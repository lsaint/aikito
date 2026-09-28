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

from .config import get_inbox_path
from .init import is_recognized_workspace
from .skill_state import WorkspaceWriterLock
from .workspace_core import (
    StateUpdate,
    WorkspaceCoreError,
    apply,
    entry_type,
    has_pending,
    recover,
    validate_resource_path,
)
from .workspace_merge import compare
from .workspace_remote import (
    FilesystemRemote,
    RemoteSnapshot,
    RECONCILE_POLICY,
    REPLICA_STATE,
    SYNC_KINDS,
    decode_resources,
    encode_resources,
    LOCAL_CONFIG,
    state_path,
    resource_for_id,
    valid_identity,
)
from .workspace_resource_write import (
    ResourceContent,
    ResourceWrite,
    prepare_resource_writes,
    credential_resources,
    reference_conflicts,
    toml_conflicts,
    verify_resource_snapshot,
)
from .workspace_resources import (
    Resource,
    WorkspaceSnapshot,
    WorkspaceResourceError,
    physical_kind,
    snapshot_workspace,
)
from .workspace_templates import template_fingerprints


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
    remote: Path
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
        not valid_identity(raw.get("sync_id"))
        or not valid_identity(raw.get("replica_id"))
        or type(raw.get("generation")) is not int
        or not 0 <= raw["generation"] <= remote.generation
    ):
        raise WorkspaceReconcileError("Invalid replica state")
    if raw["sync_id"] != remote.sync_id:
        raise WorkspaceReconcileError("Replica belongs to a different resource center")
    return ReplicaState(
        raw["sync_id"],
        raw["replica_id"],
        raw["generation"],
        decode_resources(raw.get("base")),
    ), text


def _roots(local: Path, remote: FilesystemRemote) -> Path:
    local = local.expanduser().resolve()
    if not is_recognized_workspace(local):
        raise WorkspaceReconcileError("Local path must be an Aikito workspace")
    if (
        local == remote.root
        or local in remote.root.parents
        or remote.root in local.parents
    ):
        raise WorkspaceReconcileError("Replica and resource center must be separate")
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


def build_reconcile_plan(
    local: Path,
    remote: FilesystemRemote,
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
        policy = _local_policy(local)
        b = center.resources
        base = state.base if state else {}
        identities = base.keys() | a.keys() | b.keys()
        choices = dict(resolutions or {})
        for identity, side in choices.items():
            if identity not in identities or side not in {"local", "remote"}:
                raise WorkspaceReconcileError(
                    f"Invalid reconciliation resolution: {identity}={side}"
                )
        findings = [
            f"Local {finding.resource}: {finding.message}"
            for finding in snapshot.findings
        ]
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
            if action == "CONFLICT" and identity in choices:
                side = choices[identity]
                target = "remote" if side == "local" else "local"
                before = remote_fp if target == "remote" else local_fp
                after = local_fp if target == "remote" else remote_fp
                action = (
                    "DELETE"
                    if after is None
                    else "CREATE"
                    if before is None
                    else "UPDATE"
                )
                reason = f"Conflict resolved using {side}"
            before = (local_fp if target == "local" else remote_fp) if target else None
            after = (remote_fp if target == "local" else local_fp) if target else None
            if target and action in {"CREATE", "UPDATE", "DELETE"}:
                resource = left or right or ancestor
                resource_for_id(identity, resource.fingerprint)
                if resource.kind == "inbox" and not policy.inbox_prefix:
                    action, target, reason = (
                        "BLOCKED",
                        None,
                        "Inbox is outside the workspace; reconciliation is not permitted",
                    )
                else:
                    path = (
                        f"{policy.inbox_prefix}/{resource.name}"
                        if resource.kind == "inbox" and target == "local"
                        else f"inbox/{resource.name}"
                        if resource.kind == "inbox"
                        else resource.parts[0].path
                    )
                    validate_resource_path(
                        path,
                        physical_kind(resource.kind),
                        policy if target == "local" else RECONCILE_POLICY,
                    )
            items.append(ReconcileItem(identity, action, target, before, after, reason))
        # Secrets block only the upload that contains them; unrelated work remains safe.
        local_content = (
            ResourceContent.from_workspace(snapshot) if not snapshot.findings else None
        )
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
        # Include preserved local-only project/selection resources when checking deletion.
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
                item.id
                for item in items
                if item.target == side and item.action == "DELETE"
            }
            if local_content is not None:
                rejected_fields = toml_conflicts(
                    remote.content(center) if side == "local" else local_content,
                    local_content if side == "local" else remote.content(center),
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
        return ReconcilePlan(
            local,
            remote.root,
            snapshot,
            center,
            state,
            state_text,
            tuple(items),
            tuple(sorted(set(findings))),
            tuple(sorted(choices.items())),
        )
    except (WorkspaceCoreError, WorkspaceResourceError) as exc:
        if isinstance(exc, WorkspaceReconcileError):
            raise
        raise WorkspaceReconcileError(str(exc)) from exc


def recover_reconciliation(local: Path, remote: FilesystemRemote) -> bool:
    """Recover each pending batch, preserving externally changed resources."""
    try:
        local = _roots(local, remote)
        with remote.lock():
            center = remote.recover()
            replica = recover((local,), policy=RECONCILE_POLICY)
            return center or replica
    except (WorkspaceCoreError, WorkspaceResourceError) as exc:
        raise WorkspaceReconcileError(str(exc)) from exc


def apply_reconcile_plan(
    plan: ReconcilePlan, home: Path, *, remote: FilesystemRemote | None = None
) -> None:
    """Conditionally commit the safe subset, then confirm each converged resource."""
    remote = remote or FilesystemRemote(plan.remote)
    if remote.root != plan.remote:
        raise WorkspaceReconcileError("Plan belongs to a different resource center")
    try:
        with WorkspaceWriterLock(home), remote.lock():
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
                    else plan.remote_snapshot.resources,
                    "inbox",
                )
                for item in plan.changes
                if item.target == "remote"
            )
            downloads = tuple(
                _write(
                    item,
                    plan.remote_snapshot.resources
                    if item.after is not None
                    else plan.local_snapshot.resources,
                    _local_policy(plan.local).inbox_prefix,
                )
                for item in plan.changes
                if item.target == "local"
            )
            with tempfile.TemporaryDirectory(prefix="aikito-reconcile-") as staging:
                local_changes, expected = prepare_resource_writes(
                    remote.content(plan.remote_snapshot),
                    plan.local_snapshot,
                    downloads,
                    Path(staging),
                    policy=_local_policy(plan.local),
                    sync=True,
                )
                center = remote.commit(
                    plan.remote_snapshot,
                    ResourceContent.from_workspace(plan.local_snapshot),
                    uploads,
                )
                base = dict(plan.base)
                for item in plan.items:
                    if item.action in {"CONFLICT", "BLOCKED"}:
                        continue
                    left, right = expected.get(item.id), center.resources.get(item.id)
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
    except (WorkspaceCoreError, WorkspaceResourceError) as exc:
        if isinstance(exc, WorkspaceReconcileError):
            raise
        raise WorkspaceReconcileError(str(exc)) from exc


def run_reconciliation(
    local: Path,
    remote: FilesystemRemote,
    home: Path,
    *,
    dry_run: bool,
    resolutions: Mapping[str, str] | None = None,
) -> ReconcilePlan:
    """Preview or apply safe work; conflicts and credential blocks remain visible."""
    if not dry_run:
        with WorkspaceWriterLock(home), remote.lock():
            if recover_reconciliation(local, remote):
                raise WorkspaceReconcileError(
                    "Recovered an interrupted round; run again"
                )
    plan = build_reconcile_plan(local, remote, resolutions=resolutions)
    if not dry_run and not plan.blocked:
        apply_reconcile_plan(plan, home, remote=remote)
    return plan
