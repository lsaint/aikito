"""Reconcile a workspace replica with a conditionally committed resource center.

Each replica keeps its own per-resource base. Unresolved or blocked resources
retain that base while the safe subset advances. Center and replica commits
are separate atomic transactions: if the replica fails after an upload, its
old base makes the next round safely recognize or repeat the accepted work.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import get_inbox_path
from ..init import is_recognized_workspace
from ..skill_state import WorkspaceWriterLock
from .transactions import (
    StateUpdate,
    WorkspaceCoreError,
    apply,
    entry_type,
    has_pending,
    recover,
    validate_resource_path,
)
from .merge import compare
from .resource_state import (
    REPLICA_POLICY as RECONCILE_POLICY,
    REPLICA_STATE,
    SYNC_KINDS,
    decode_resources,
    encode_resources,
    LOCAL_CONFIG,
    state_path,
    local_resource_for_id,
    valid_identity,
    validate_skill_fingerprint_scheme,
)
from .resource_write import (
    ResourceContent,
    ResourceWrite,
    credential_resources,
    reference_conflicts,
    toml_conflicts,
    verify_resource_snapshot,
)
from .resources import (
    Resource,
    WorkspaceSnapshot,
    WorkspaceResourceError,
    physical_kind,
    snapshot_workspace,
    SKILL_FINGERPRINT_SCHEME,
)
from .remote_store import RemoteStore, RemoteSnapshot, StoreError
from .payload import (
    ResourcePayload,
    TomlPayload,
    PayloadError,
    credential_payload,
    payload_hash,
    validate_payload,
)
from .payload_io import capture_mutations, prepare_payload_writes
from .templates import template_fingerprints


class WorkspaceReconcileError(WorkspaceCoreError):
    """A reconciliation round cannot be handled safely."""


@dataclass(frozen=True)
class ReplicaState:
    sync_id: str
    replica_id: str
    generation: int
    base: dict[str, Resource]

    def encode(self) -> str:
        return json.dumps(
            {
                "version": 2,
                "skill_fingerprint": SKILL_FINGERPRINT_SCHEME,
                "sync_id": self.sync_id,
                "replica_id": self.replica_id,
                "generation": self.generation,
                "base": encode_resources(self.base),
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class ReconcileItem:
    id: str
    action: str
    target: str | None
    before: str | None
    after: str | None
    reason: str


@dataclass(frozen=True)
class ReconcilePlan:
    local: Path
    local_snapshot: WorkspaceSnapshot
    remote_snapshot: RemoteSnapshot
    state: ReplicaState | None
    state_text: str | None
    items: tuple[ReconcileItem, ...]
    findings: tuple[str, ...] = ()
    resolutions: tuple[tuple[str, str], ...] = ()

    @property
    def generation(self) -> int:
        return self.remote_snapshot.generation

    @property
    def base(self) -> dict[str, Resource]:
        return self.state.base if self.state else {}

    @property
    def conflicts(self) -> tuple[ReconcileItem, ...]:
        return tuple(item for item in self.items if item.action == "CONFLICT")

    @property
    def changes(self) -> tuple[ReconcileItem, ...]:
        return tuple(
            item for item in self.items if item.action in {"CREATE", "UPDATE", "DELETE"}
        )

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


def _center_resources(center: RemoteSnapshot) -> dict[str, Resource]:
    """Decode remote logical descriptions only at the client's local boundary."""
    result = {}
    for identity, descriptor in center.resources.items():
        canonical = local_resource_for_id(identity, descriptor.fingerprint)
        if (
            canonical.kind not in {"mcp", "subagent"}
            and descriptor.references != canonical.references
        ):
            raise PayloadError("Invalid remote resource references")
        result[identity] = replace(canonical, references=descriptor.references)
    return result


def _fetch_validated(
    remote: RemoteStore, center: RemoteSnapshot, resources: Mapping[str, Resource]
) -> Mapping[str, ResourcePayload]:
    payloads = remote.fetch(center, tuple(resources))
    if set(payloads) != set(resources):
        raise PayloadError("Incomplete remote payload batch")
    for identity, resource in resources.items():
        if payload_hash(payloads[identity]) != center.resources[identity].content_hash:
            raise PayloadError("Downloaded payload transport hash mismatch")
        validate_payload(resource, payloads[identity])
    return payloads


def _read_state(
    local: Path, remote: RemoteSnapshot
) -> tuple[ReplicaState | None, str | None]:
    legacy = state_path(local, ".local/state/aikito/workspace-reconcile/baseline.json")
    if entry_type(legacy) != "missing":
        raise WorkspaceReconcileError(
            "Unsupported baseline format; remove the old internal baseline and pair again"
        )
    path = state_path(local, REPLICA_STATE)
    if entry_type(path) == "missing":
        return None, None
    text = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(text)
    except ValueError as exc:
        raise WorkspaceReconcileError("Invalid replica state") from exc
    if not isinstance(raw, dict) or raw.get("version") != 2:
        raise WorkspaceReconcileError("Unsupported replica state version")
    if (
        not isinstance(raw.get("sync_id"), str)
        or not raw.get("sync_id")
        or not valid_identity(raw.get("replica_id"))
        or type(raw.get("generation")) is not int
        or not 0 <= raw["generation"] <= remote.generation
    ):
        raise WorkspaceReconcileError("Invalid replica state")
    if raw["sync_id"] != remote.sync_id:
        raise WorkspaceReconcileError("Replica belongs to a different resource center")
    base = decode_resources(raw.get("base"))
    validate_skill_fingerprint_scheme(raw.get("skill_fingerprint"), base)
    return ReplicaState(
        raw["sync_id"],
        raw["replica_id"],
        raw["generation"],
        base,
    ), text


def _roots(local: Path, remote: RemoteStore) -> Path:
    local = local.expanduser().resolve()
    if not is_recognized_workspace(local):
        raise WorkspaceReconcileError("Local path must be an Aikito workspace")
    remote.validate_replica(local)
    return local


def _local_policy(local: Path):
    try:
        prefix = get_inbox_path(local).relative_to(local).as_posix()
    except ValueError:
        prefix = ""
    return replace(RECONCILE_POLICY, inbox_prefix=prefix)


def _write(
    item: ReconcileItem, resources: dict[str, Resource], inbox_prefix: str
) -> ResourceWrite:
    resource = resources[item.id]
    return ResourceWrite(
        Path(
            f"{inbox_prefix}/{resource.name}"
            if resource.kind == "inbox" and item.after is not None
            else resource.parts[0].path
        ),
        resource.kind,
        item.after,
        resource.name,
        Path(resource.parts[0].path),
        item.before,
    )


def _check_items(
    proposed: list[ReconcileItem],
    snapshot: WorkspaceSnapshot,
    center: dict[str, Resource],
    local_content: ResourceContent | None,
    remote_content: ResourceContent,
    remote_payloads: Mapping[str, ResourcePayload],
) -> tuple[list[ReconcileItem], list[str]]:
    """Check candidate changes after comparison or conflict choices."""
    items = list(proposed)
    a, b = snapshot.resources, center
    policy = _local_policy(snapshot.root)
    findings = [
        f"Local {finding.resource}: {finding.message}" for finding in snapshot.findings
    ]
    for index, item in enumerate(items):
        if item.action not in {"CREATE", "UPDATE", "DELETE"}:
            continue
        resource = a.get(item.id) or b.get(item.id)
        local_resource_for_id(item.id, resource.fingerprint)
        if resource.kind == "inbox" and not policy.inbox_prefix:
            items[index] = replace(
                item,
                action="BLOCKED",
                target=None,
                reason="Inbox is outside the workspace; reconciliation is not permitted",
            )
            continue
        path = (
            f"{policy.inbox_prefix}/{resource.name}"
            if resource.kind == "inbox" and item.target == "local"
            else f"inbox/{resource.name}"
            if resource.kind == "inbox"
            else resource.parts[0].path
        )
        validate_resource_path(
            path,
            physical_kind(resource.kind),
            policy if item.target == "local" else RECONCILE_POLICY,
        )
    # Secrets block only the upload that contains them; unrelated work remains safe.
    secrets = (
        frozenset()
        if snapshot.findings
        else credential_resources(
            local_content,
            {
                item.id
                for item in items
                if item.target == "remote" and item.after is not None
            },
        )
    )
    items = [
        replace(
            item,
            action="BLOCKED",
            target=None,
            reason="Possible plaintext credential; resource is not uploaded",
        )
        if item.id in secrets
        else item
        for item in items
    ]
    items = [
        replace(
            item,
            action="BLOCKED",
            target=None,
            reason="Possible plaintext credential; resource is not downloaded",
        )
        if item.target == "local"
        and item.after is not None
        and credential_payload(remote_payloads[item.id])
        else item
        for item in items
    ]
    # Reference checks include every preserved resource on each side.
    for side, source, target_resources in (
        ("local", b, snapshot.resources),
        ("remote", a, b),
    ):
        changes = {
            item.id
            for item in items
            if item.target == side and item.action in {"CREATE", "UPDATE"}
        }
        deletions = {
            item.id for item in items if item.target == side and item.action == "DELETE"
        }
        if local_content is not None:
            rejected_fields = toml_conflicts(
                remote_content if side == "local" else local_content,
                local_content if side == "local" else remote_content,
                changes,
                deletions,
            )
            items = [
                replace(
                    item,
                    action="CONFLICT",
                    target=None,
                    before=None,
                    after=None,
                    reason=rejected_fields[item.id],
                )
                if item.id in rejected_fields and item.target == side
                else item
                for item in items
            ]
            changes.difference_update(rejected_fields)
        rejected, residual = reference_conflicts(
            source,
            target_resources,
            changes,
            deletions=deletions,
        )
        items = [
            replace(
                item,
                action="CONFLICT",
                target=None,
                before=None,
                after=None,
                reason=rejected[item.id],
            )
            if item.id in rejected and item.target == side
            else item
            for item in items
        ]
        findings.extend(f"{side}: {finding}" for finding in residual)
    return items, findings


def _select_version(
    identity: str, side: str, local: dict[str, Resource], remote: dict[str, Resource]
) -> ReconcileItem:
    source, destination = (local, remote) if side == "local" else (remote, local)
    before = destination[identity].fingerprint if identity in destination else None
    after = source[identity].fingerprint if identity in source else None
    action = (
        "NOOP"
        if before == after
        else "DELETE"
        if after is None
        else "CREATE"
        if before is None
        else "UPDATE"
    )
    target = None if action == "NOOP" else "remote" if side == "local" else "local"
    return ReconcileItem(
        identity, action, target, before, after, f"Conflict resolved using {side}"
    )


def _result_resources(
    items: dict[str, ReconcileItem],
    resources: dict[str, Resource],
    source: dict[str, Resource],
    side: str,
) -> dict[str, Resource]:
    result = dict(resources)
    for item in items.values():
        if item.target != side:
            continue
        if item.action == "DELETE":
            result.pop(item.id, None)
        elif item.action in {"CREATE", "UPDATE"}:
            result[item.id] = source[item.id]
    return result


def _resolve_items(
    proposed: list[ReconcileItem],
    choices: dict[str, str],
    local: dict[str, Resource],
    remote: dict[str, Resource],
) -> list[ReconcileItem]:
    """Choose versions, restoring providers or pruning dependent set members.

    A dependency choice never overrides another explicit choice. Only set
    members are pruned automatically; standalone dependent content still needs
    its own choice. All inferred changes go through ordinary safety checks.
    """
    items = {item.id: item for item in proposed}
    pending = list(sorted(choices.items()))
    for identity, side in pending:
        items[identity] = _select_version(identity, side, local, remote)
    visited = set()
    while pending:
        identity, side = pending.pop()
        if (identity, side) in visited:
            continue
        visited.add((identity, side))
        item = items[identity]
        source, destination = (local, remote) if side == "local" else (remote, local)
        target = "remote" if side == "local" else "local"
        result = _result_resources(items, destination, source, target)
        if item.action in {"CREATE", "UPDATE"}:
            for reference in source[identity].references:
                if reference in result or reference not in source:
                    continue
                if reference in choices and choices[reference] != side:
                    continue
                items[reference] = replace(
                    _select_version(reference, side, local, remote),
                    reason=f"Restore {reference} required by resolved {identity}",
                )
                pending.append((reference, side))
        elif item.action == "DELETE":
            for key, resource in sorted(result.items()):
                if identity not in resource.references or resource.kind not in {
                    "skill-selection",
                    "project-skill",
                    "project-path",
                }:
                    continue
                if key in choices and choices[key] != side:
                    continue
                if key in source:
                    continue
                items[key] = replace(
                    _select_version(key, side, local, remote),
                    reason=f"Remove membership referencing deleted {identity}; resolved using {side}",
                )
                pending.append((key, side))
    return [items[key] for key in sorted(items)]


def build_reconcile_plan(
    local: Path,
    remote: RemoteStore,
    *,
    resolutions: Mapping[str, str] | None = None,
) -> ReconcilePlan:
    """Preview a round without creating state, lock files, or resource files."""
    try:
        local = _roots(local, remote)
        if has_pending((local,), policy=RECONCILE_POLICY):
            raise WorkspaceReconcileError("Pending round needs recovery before preview")
        center = remote.read()
        state, state_text = _read_state(local, center)
        snapshot = snapshot_workspace(local)
        a = {
            key: resource
            for key, resource in snapshot.resources.items()
            if resource.kind in SYNC_KINDS
            and not (resource.kind == "config" and resource.name in LOCAL_CONFIG)
        }
        b = _center_resources(center)
        base = state.base if state else {}
        identities = base.keys() | a.keys() | b.keys()
        choices = dict(resolutions or {})
        for identity, side in choices.items():
            if identity not in identities or side not in {"local", "remote"}:
                raise WorkspaceReconcileError(
                    f"Invalid reconciliation resolution: {identity}={side}"
                )
        items = []
        for identity in sorted(identities):
            left, right, ancestor = a.get(identity), b.get(identity), base.get(identity)
            local_fp, remote_fp = (
                left.fingerprint if left else None,
                right.fingerprint if right else None,
            )
            reference = (
                frozenset({ancestor.fingerprint})
                if ancestor
                else template_fingerprints(identity)
                if state is None
                else frozenset()
            )
            outcome = compare(reference, local_fp, remote_fp)
            action, target, reason = outcome.action, outcome.target, outcome.reason
            if state is None and action == "DELETE":
                action = "CREATE"
                target = "remote" if target == "local" else "local"
                reason = "First pairing preserves existing resources"
            before = (local_fp if target == "local" else remote_fp) if target else None
            after = (remote_fp if target == "local" else local_fp) if target else None
            items.append(ReconcileItem(identity, action, target, before, after, reason))
        local_content = (
            ResourceContent.from_workspace(snapshot) if not snapshot.findings else None
        )
        remote_payloads = _fetch_validated(remote, center, b)
        remote_content = ResourceContent(
            b,
            {},
            values={
                key: payload.field()
                for key, payload in remote_payloads.items()
                if isinstance(payload, TomlPayload)
            },
        )
        checked, findings = _check_items(
            items, snapshot, b, local_content, remote_content, remote_payloads
        )
        conflicts = {item.id for item in checked if item.action == "CONFLICT"}
        for identity in choices:
            if identity not in conflicts:
                raise WorkspaceReconcileError(
                    f"Resolution requires a conflicting resource: {identity}"
                )
        if choices:
            proposed = _resolve_items(items, choices, a, b)
            checked, findings = _check_items(
                proposed, snapshot, b, local_content, remote_content, remote_payloads
            )
        return ReconcilePlan(
            local,
            snapshot,
            center,
            state,
            state_text,
            tuple(checked),
            tuple(sorted(set(findings))),
            tuple(sorted(choices.items())),
        )
    except (
        WorkspaceCoreError,
        WorkspaceResourceError,
        StoreError,
        PayloadError,
    ) as exc:
        if isinstance(exc, WorkspaceReconcileError):
            raise
        raise WorkspaceReconcileError(str(exc)) from exc


def recover_reconciliation(local: Path, remote: RemoteStore) -> bool:
    """Recover each pending batch, preserving externally changed resources."""
    try:
        local = _roots(local, remote)
        center = remote.recover()
        replica = recover((local,), policy=RECONCILE_POLICY)
        return center or replica
    except (
        WorkspaceCoreError,
        WorkspaceResourceError,
        StoreError,
        PayloadError,
    ) as exc:
        raise WorkspaceReconcileError(str(exc)) from exc


def apply_reconcile_plan(
    plan: ReconcilePlan, home: Path, *, remote: RemoteStore
) -> None:
    """Conditionally commit the safe subset, then confirm each converged resource."""
    try:
        with WorkspaceWriterLock(home):
            if recover_reconciliation(plan.local, remote):
                raise WorkspaceReconcileError(
                    "Recovered an interrupted round; run again"
                )
            fresh = build_reconcile_plan(
                plan.local, remote, resolutions=dict(plan.resolutions)
            )
            if fresh != plan:
                raise WorkspaceReconcileError(
                    "Resources or generation changed after planning; run again"
                )
            if plan.blocked:
                raise WorkspaceReconcileError(
                    "Reconciliation has blocking findings; no resources changed"
                )
            uploads = tuple(
                _write(
                    item,
                    plan.local_snapshot.resources
                    if item.after is not None
                    else _center_resources(plan.remote_snapshot),
                    "inbox",
                )
                for item in plan.changes
                if item.target == "remote"
            )
            downloads = tuple(
                _write(
                    item,
                    _center_resources(plan.remote_snapshot)
                    if item.after is not None
                    else plan.local_snapshot.resources,
                    _local_policy(plan.local).inbox_prefix,
                )
                for item in plan.changes
                if item.target == "local"
            )
            with tempfile.TemporaryDirectory(prefix="aikito-reconcile-") as staging:
                remote_resources = _center_resources(plan.remote_snapshot)
                download_resources = {
                    write.id: remote_resources[write.id]
                    for write in downloads
                    if write.fingerprint is not None
                }
                payloads = _fetch_validated(
                    remote, plan.remote_snapshot, download_resources
                )
                local_changes, expected = prepare_payload_writes(
                    download_resources,
                    payloads,
                    plan.local_snapshot,
                    downloads,
                    Path(staging),
                    policy=_local_policy(plan.local),
                )
                mutations = capture_mutations(
                    ResourceContent.from_workspace(plan.local_snapshot),
                    uploads,
                    plan.remote_snapshot.resources,
                )
                center = remote.commit(plan.remote_snapshot, mutations)
                center_resources = _center_resources(center)
                base = dict(plan.base)
                for item in plan.items:
                    if item.action in {"CONFLICT", "BLOCKED"}:
                        continue
                    left, right = expected.get(item.id), center_resources.get(item.id)
                    if left is None and right is None:
                        base.pop(item.id, None)
                    elif left and right and left.fingerprint == right.fingerprint:
                        base[item.id] = right
                state = ReplicaState(
                    center.sync_id,
                    plan.state.replica_id if plan.state else uuid.uuid4().hex,
                    center.generation,
                    base,
                )
                if not local_changes and state == plan.state:
                    return
                state_path(plan.local, REPLICA_STATE, create=True)
                apply(
                    (plan.local,),
                    local_changes,
                    states=(
                        StateUpdate(0, REPLICA_STATE, plan.state_text, state.encode()),
                    ),
                    verify=lambda: verify_resource_snapshot(
                        snapshot_workspace(plan.local), expected
                    ),
                    policy=_local_policy(plan.local),
                )
    except (
        WorkspaceCoreError,
        WorkspaceResourceError,
        StoreError,
        PayloadError,
    ) as exc:
        if isinstance(exc, WorkspaceReconcileError):
            raise
        raise WorkspaceReconcileError(str(exc)) from exc


def run_reconciliation(
    local: Path,
    remote: RemoteStore,
    home: Path,
    *,
    dry_run: bool,
    resolutions: Mapping[str, str] | None = None,
) -> ReconcilePlan:
    """Preview or apply safe work; conflicts and credential blocks remain visible."""
    if not dry_run:
        with WorkspaceWriterLock(home):
            if recover_reconciliation(local, remote):
                raise WorkspaceReconcileError(
                    "Recovered an interrupted round; run again"
                )
    plan = build_reconcile_plan(local, remote, resolutions=resolutions)
    if not dry_run and not plan.blocked:
        apply_reconcile_plan(plan, home, remote=remote)
    return plan
