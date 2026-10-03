"""Reconcile a workspace replica with a conditionally committed resource center.

Each replica keeps its own per-resource base. Unresolved or blocked resources
retain that base while the safe subset advances. Center and replica commits
are separate atomic transactions: if the replica fails after an upload, its
durable request and receipt confirm uploaded Base before the next plan.
"""

from __future__ import annotations

import hashlib
import tempfile
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path

from ..config import get_inbox_path
from ..init import is_recognized_workspace
from ..skill_state import WorkspaceWriterLock
from . import remote_limits
from .reconcile_budget import (
    CHANGES,
    check_manifest,
    defer_dependents,
    needed_downloads,
    operation_dependencies,
    select_round,
)
from .remote_protocol import encode_commit_request as encode_protocol_commit_request
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
    PENDING_COMMIT_STATE,
    SYNC_KINDS,
    LOCAL_CONFIG,
    state_path,
    local_resource_for_id,
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
)
from .remote_store import RemoteStore, RemoteSnapshot, SnapshotExpired, StoreError
from .remote_wire import (
    build_commit_request,
    committed_snapshot,
    validate_commit_result,
)
from .pending_commit import PendingCommitStore
from .commit_recovery import recover_pending
from .payload import (
    ResourceDescriptor,
    ResourcePayload,
    TreePayload,
    tree_mode_fingerprint,
    TomlPayload,
    PayloadError,
    credential_payload,
    encode_payload,
    payload_hash,
    validate_payload,
)
from .payload_io import capture_mutations, capture_resources, prepare_payload_writes
from .replica_state import ReplicaState, load_replica_state
from .templates import template_fingerprints


class WorkspaceReconcileError(WorkspaceCoreError):
    """A reconciliation round cannot be handled safely."""


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
    payload_cache: tuple[tuple[str, str, ResourcePayload], ...] = field(
        default=(), compare=False, repr=False
    )
    applied_items: tuple[ReconcileItem, ...] = ()
    rounds: int = 0
    stop_reason: str | None = None

    @property
    def revision(self) -> int:
        return self.remote_snapshot.revision

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
    def deferred(self) -> tuple[ReconcileItem, ...]:
        return tuple(item for item in self.items if item.action == "DEFERRED")

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
        result[identity] = replace(
            canonical,
            references=descriptor.references,
            mode_fingerprint=descriptor.mode_fingerprint,
        )
    return result


def _resource_version(resource: Resource | None) -> str | None:
    if resource is None:
        return None
    if resource.kind == "skill" and resource.mode_fingerprint is not None:
        return f"{resource.fingerprint}:{resource.mode_fingerprint}"
    return resource.fingerprint


def _fetch_validated(
    remote: RemoteStore, center: RemoteSnapshot, resources: Mapping[str, Resource]
) -> Mapping[str, ResourcePayload]:
    if not resources:
        return {}
    payloads = remote.fetch(center, tuple(resources))
    if set(payloads) != set(resources):
        raise PayloadError("Incomplete remote payload batch")
    for identity, resource in resources.items():
        if (
            payload_hash(payloads[identity]) != center.resources[identity].content_hash
            or len(encode_payload(payloads[identity]))
            != center.resources[identity].size
        ):
            raise PayloadError("Downloaded payload transport hash mismatch")
        validate_payload(resource, payloads[identity])
    return payloads


def _plan_payloads(
    remote: RemoteStore,
    center: RemoteSnapshot,
    resources: Mapping[str, Resource],
    items: list[ReconcileItem],
    captured: Mapping[str, ResourcePayload] | None = None,
) -> dict[str, ResourcePayload]:
    """Fetch candidate downloads and fields needed to merge shared config."""
    needed = needed_downloads(items, resources)
    result = dict(captured or {})
    missing = {key: resources[key] for key in sorted(needed - result.keys())}
    result.update(_fetch_validated(remote, center, missing))
    return result


def _remote_plan_content(
    resources: dict[str, Resource], payloads: Mapping[str, ResourcePayload]
) -> ResourceContent:
    return ResourceContent(
        resources,
        {},
        values={
            key: payload.field()
            for key, payload in payloads.items()
            if isinstance(payload, TomlPayload)
        },
    )


def _read_state(
    local: Path, remote: RemoteSnapshot
) -> tuple[ReplicaState | None, str | None]:
    return load_replica_state(
        local, sync_id=remote.sync_id, max_revision=remote.revision
    )


def _roots(local: Path, remote: RemoteStore, *, allow_pending: bool = False) -> Path:
    local = local.expanduser().resolve()
    if (
        not allow_pending
        and entry_type(state_path(local, PENDING_COMMIT_STATE)) != "missing"
    ):
        raise WorkspaceReconcileError(
            "Pending remote commit needs recovery before preview"
        )
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
                if item.action in CHANGES
                and item.target == "remote"
                and item.after is not None
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
        if item.action in CHANGES
        and item.target == "local"
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
        if (
            before == after
            and (source[identity].mode_fingerprint if identity in source else None)
            == (
                destination[identity].mode_fingerprint
                if identity in destination
                else None
            )
        )
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


def _budget_uploads(proposed, local_content, resources):
    """Capture exact upload sizes before selection, blocking unfit resources."""
    descriptors = {}
    checked = []
    for item in proposed:
        if (
            item.action not in CHANGES
            or item.target != "remote"
            or local_content is None
        ):
            checked.append(item)
            continue
        if item.after is None:
            descriptors[item.id] = None
        else:
            payload = capture_resources(
                local_content, [item.id], check_credentials=False
            )[item.id]
            encoded = encode_payload(payload)
            descriptor = ResourceDescriptor(
                resources[item.id].fingerprint,
                hashlib.sha256(encoded).hexdigest(),
                len(encoded),
                resources[item.id].references,
                tree_mode_fingerprint(payload)
                if isinstance(payload, TreePayload)
                else None,
            )
            # The next capture must never overlap with this resource's bytes.
            del payload, encoded
            if descriptor.size > remote_limits.MAX_RESOURCE_PAYLOAD_BYTES:
                item = replace(
                    item,
                    action="BLOCKED",
                    target=None,
                    reason="Resource exceeds the sync size limit; resource is not uploaded",
                )
            else:
                descriptors[item.id] = descriptor
        checked.append(item)
    return checked, descriptors


def build_reconcile_plan(
    local: Path,
    remote: RemoteStore,
    *,
    resolutions: Mapping[str, str] | None = None,
    _cached: tuple[tuple[str, str, ResourcePayload], ...] = (),
    _expected: ReconcilePlan | None = None,
    _inspect_payloads: bool = True,
) -> ReconcilePlan:
    """Preview a round without creating state, lock files, or resource files."""
    try:
        local = _roots(local, remote)
        if has_pending((local,), policy=RECONCILE_POLICY):
            raise WorkspaceReconcileError("Pending round needs recovery before preview")
        center = remote.read()
        state, state_text = _read_state(local, center)
        snapshot = snapshot_workspace(local)
        check_manifest(center)
        if _expected is not None and center != _expected.remote_snapshot:
            raise SnapshotExpired(
                "Remote identity or revision changed after planning; replan"
            )
        if _expected is not None and (
            snapshot != _expected.local_snapshot or state_text != _expected.state_text
        ):
            raise WorkspaceReconcileError(
                "Resources or revision changed after planning; run again"
            )
        a = {
            key: resource
            for key, resource in snapshot.resources.items()
            if resource.kind in SYNC_KINDS
            and not (resource.kind == "config" and resource.name in LOCAL_CONFIG)
        }
        b = _center_resources(center)
        base = state.base if state else {}
        identities = (
            base.keys()
            | a.keys()
            | b.keys()
            | (state.unpaired_ids if state else frozenset())
        )
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
            ambiguous_mode = False
            if (
                ancestor
                and ancestor.kind == "skill"
                and ancestor.mode_fingerprint is None
            ):
                # An old Base has no executable ancestor. Equal live modes can
                # seed it; disagreement must be resolved explicitly.
                modes = {
                    resource.mode_fingerprint
                    for resource in (left, right)
                    if resource is not None
                }
                if len(modes) == 1:
                    ancestor = replace(ancestor, mode_fingerprint=modes.pop())
                else:
                    ambiguous_mode = True
            first_pairing = state is None or identity in state.unpaired_ids
            reference = (
                frozenset()
                if ambiguous_mode
                else frozenset({_resource_version(ancestor)})
                if ancestor
                else template_fingerprints(identity)
                if first_pairing
                else frozenset()
            )
            outcome = compare(
                reference, _resource_version(left), _resource_version(right)
            )
            action, target, reason = outcome.action, outcome.target, outcome.reason
            if first_pairing and (left is None) != (right is None):
                # Absence during initial pairing carries no deletion history,
                # including when the existing resource has edited a template.
                action = "CREATE"
                target = "local" if left is None else "remote"
                reason = "First pairing preserves existing resources"
            before = (local_fp if target == "local" else remote_fp) if target else None
            after = (remote_fp if target == "local" else local_fp) if target else None
            items.append(ReconcileItem(identity, action, target, before, after, reason))
        local_content = (
            ResourceContent.from_workspace(snapshot) if not snapshot.findings else None
        )
        # Explicit choices are expanded at descriptor level, before any fetch.
        # Payload-only conflicts are discovered later and may require a second
        # budgeted selection, but can never download outside the selected scope.
        declared_conflicts = {item.id for item in items if item.action == "CONFLICT"}
        reference_rejections = {}
        for side, source, target in (("local", b, a), ("remote", a, b)):
            rejected, _ = reference_conflicts(
                source,
                target,
                {
                    item.id
                    for item in items
                    if item.target == side and item.action in {"CREATE", "UPDATE"}
                },
                deletions={
                    item.id
                    for item in items
                    if item.target == side and item.action == "DELETE"
                },
            )
            declared_conflicts.update(rejected)
            reference_rejections.update(rejected)
        for identity in choices:
            if identity not in declared_conflicts and not identity.startswith(
                "config:"
            ):
                raise WorkspaceReconcileError(
                    f"Resolution requires a conflicting resource: {identity}"
                )
        compared = [
            replace(
                item,
                action="CONFLICT",
                target=None,
                before=None,
                after=None,
                reason=reference_rejections[item.id],
            )
            if item.id in reference_rejections
            else item
            for item in items
        ]
        proposed = _resolve_items(items, choices, a, b) if choices else compared
        dependencies = operation_dependencies(proposed, a, b)
        proposed, upload_descriptors = _budget_uploads(proposed, local_content, a)
        selected = select_round(
            proposed,
            dependencies,
            center,
            upload_descriptors,
            state.receipt_cursor if state else None,
        )
        if not _inspect_payloads:
            return ReconcilePlan(
                local,
                snapshot,
                center,
                state,
                state_text,
                tuple(
                    replace(item, action="DEFERRED", reason="Round limit reached")
                    if item.action in CHANGES
                    else item
                    for item in selected
                ),
                resolutions=tuple(sorted(choices.items())),
            )
        needed = needed_downloads(selected, b)
        captured = {
            key: payload
            for key, digest, payload in _cached
            if key in needed and center.resources[key].content_hash == digest
        }
        for key, payload in captured.items():
            if (
                payload_hash(payload) != center.resources[key].content_hash
                or len(encode_payload(payload)) != center.resources[key].size
            ):
                raise PayloadError("Cached payload transport hash or size mismatch")
            validate_payload(b[key], payload)
        remote_payloads = _plan_payloads(remote, center, b, selected, captured)
        remote_content = _remote_plan_content(b, remote_payloads)
        checked, findings = _check_items(
            selected, snapshot, b, local_content, remote_content, remote_payloads
        )
        for identity in choices.keys() - declared_conflicts:
            chosen = next(item for item in checked if item.id == identity)
            if chosen.action in {"DEFERRED", "BLOCKED"}:
                continue
            original = [
                item
                if item.id.startswith("config:")
                else replace(item, action="NOOP", target=None)
                for item in items
            ]
            original_checked, _ = _check_items(
                original, snapshot, b, local_content, remote_content, remote_payloads
            )
            if (
                next(item for item in original_checked if item.id == identity).action
                != "CONFLICT"
            ):
                raise WorkspaceReconcileError(
                    f"Resolution requires a conflicting resource: {identity}"
                )
        checked = defer_dependents(checked, dependencies)
        return ReconcilePlan(
            local,
            snapshot,
            center,
            state,
            state_text,
            tuple(checked),
            tuple(sorted(set(findings))),
            tuple(sorted(choices.items())),
            tuple(
                (key, center.resources[key].content_hash, payload)
                for key, payload in sorted(remote_payloads.items())
            ),
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


def recover_reconciliation(local: Path, remote: RemoteStore, home: Path) -> bool:
    """Recover pending identity first; standalone journals invalidate old plans."""
    try:
        with WorkspaceWriterLock(home):
            local = _roots(local, remote, allow_pending=True)
            replica = recover((local,), policy=_local_policy(local))
            if recover_pending(local, remote, home):
                return False
            center = remote.recover()
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
            if recover_reconciliation(plan.local, remote, home):
                raise WorkspaceReconcileError(
                    "Recovered an interrupted round; run again"
                )
            fresh = build_reconcile_plan(
                plan.local,
                remote,
                resolutions=dict(plan.resolutions),
                _cached=plan.payload_cache,
                _expected=plan,
            )
            if fresh != plan:
                raise WorkspaceReconcileError(
                    "Resources or revision changed after planning; run again"
                )
            if plan.blocked:
                raise WorkspaceReconcileError(
                    "Reconciliation has blocking findings; no resources changed"
                )
            remote_resources = _center_resources(plan.remote_snapshot)
            uploads = tuple(
                _write(
                    item,
                    plan.local_snapshot.resources
                    if item.after is not None
                    else remote_resources,
                    "inbox",
                )
                for item in plan.changes
                if item.target == "remote"
            )
            downloads = tuple(
                _write(
                    item,
                    remote_resources
                    if item.after is not None
                    else plan.local_snapshot.resources,
                    _local_policy(plan.local).inbox_prefix,
                )
                for item in plan.changes
                if item.target == "local"
            )
            with tempfile.TemporaryDirectory(prefix="aikito-reconcile-") as staging:
                download_resources = {
                    write.id: remote_resources[write.id]
                    for write in downloads
                    if write.fingerprint is not None
                }
                payloads = {
                    key: payload
                    for key, digest, payload in fresh.payload_cache
                    if key in download_resources
                }
                for key, payload in payloads.items():
                    validate_payload(download_resources[key], payload)
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
                if snapshot_workspace(plan.local) != plan.local_snapshot:
                    raise WorkspaceReconcileError(
                        "Local resources changed before sending"
                    )
                pending = result = None
                replica_id = plan.state.replica_id if plan.state else uuid.uuid4().hex
                if mutations:
                    safe = tuple(
                        item.id
                        for item in plan.items
                        if item.action not in {"CONFLICT", "BLOCKED", "DEFERRED"}
                    )
                    excluded = tuple(
                        item.id
                        for item in plan.items
                        if item.action in {"CONFLICT", "BLOCKED", "DEFERRED"}
                    )
                    request = build_commit_request(
                        replica_id,
                        uuid.uuid4().hex,
                        plan.remote_snapshot,
                        mutations,
                        previous_receipt=plan.state.receipt_cursor
                        if plan.state
                        else None,
                    )
                    # Reject before persisting: recovery can only resend this exact
                    # request, so an oversized pending request could never progress.
                    if (
                        len(encode_protocol_commit_request(request))
                        > remote_limits.MAX_REMOTE_REQUEST_BYTES
                    ):
                        raise WorkspaceReconcileError(
                            "Commit request exceeds the remote request size limit "
                            f"of {remote_limits.MAX_REMOTE_REQUEST_BYTES} bytes"
                        )
                    pending_store = PendingCommitStore(plan.local, home)
                    pending = pending_store.persist(
                        request,
                        safe_resource_ids=safe,
                        excluded_resource_ids=excluded,
                        unpaired_ids=frozenset(item.id for item in plan.items)
                        if plan.state is None
                        else plan.state.unpaired_ids,
                    )
                    try:
                        result = remote.commit(request)
                    except SnapshotExpired as exc:
                        pending_store.clear(pending, rejected=exc)
                        raise
                    validate_commit_result(request, result)
                    center = committed_snapshot(request)
                else:
                    center = plan.remote_snapshot
                center_resources = _center_resources(center)
                base = dict(plan.base)
                unpaired = (
                    set(plan.state.unpaired_ids)
                    if plan.state
                    else {item.id for item in plan.items}
                )
                for item in plan.items:
                    if item.action in {"CONFLICT", "BLOCKED", "DEFERRED"}:
                        continue
                    left, right = expected.get(item.id), center_resources.get(item.id)
                    if left is None and right is None:
                        base.pop(item.id, None)
                        unpaired.discard(item.id)
                    elif (
                        left
                        and right
                        and (left.fingerprint, left.mode_fingerprint)
                        == (right.fingerprint, right.mode_fingerprint)
                    ):
                        base[item.id] = right
                        unpaired.discard(item.id)
                state = ReplicaState(
                    center.sync_id,
                    replica_id,
                    center.revision,
                    base,
                    plan.state.receipt_cursor if plan.state else None,
                    plan.state.completion_marker if plan.state else None,
                    frozenset(unpaired),
                )
                if not local_changes and state == plan.state:
                    return
                if pending is not None:
                    pending_store.complete(
                        pending,
                        result,
                        state,
                        changes=local_changes,
                        verify=lambda: verify_resource_snapshot(
                            snapshot_workspace(plan.local), expected
                        ),
                    )
                    pending_store.clear(pending)
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


MAX_RECONCILIATION_ROUNDS = 1024


def run_reconciliation(
    local: Path,
    remote: RemoteStore,
    home: Path,
    *,
    dry_run: bool,
    resolutions: Mapping[str, str] | None = None,
) -> ReconcilePlan:
    """Apply bounded rounds and report accumulated work plus remaining items.

    Conflicts and blocked groups are partial success. A round or retry limit
    stops with progress intact, so another invocation can continue safely.
    """
    if dry_run:
        return build_reconcile_plan(local, remote, resolutions=resolutions)
    choices = dict(resolutions or {})
    applied = []
    rounds = 0
    with WorkspaceWriterLock(home):
        if recover_reconciliation(local, remote, home):
            raise WorkspaceReconcileError("Recovered an interrupted round; run again")
        while True:
            plan = None
            for attempt in range(2):
                try:
                    plan = build_reconcile_plan(
                        local,
                        remote,
                        resolutions=choices,
                        _inspect_payloads=rounds < MAX_RECONCILIATION_ROUNDS,
                    )
                    if plan.blocked or rounds >= MAX_RECONCILIATION_ROUNDS:
                        return replace(
                            plan,
                            applied_items=tuple(applied),
                            rounds=rounds,
                            stop_reason="Blocking findings"
                            if plan.blocked
                            else "Reconciliation round limit reached",
                        )
                    apply_reconcile_plan(plan, home, remote=remote)
                    break
                except WorkspaceReconcileError as exc:
                    if not isinstance(exc.__cause__, SnapshotExpired):
                        raise
                    if attempt:
                        if plan is None:
                            plan = build_reconcile_plan(
                                local,
                                remote,
                                resolutions=choices,
                                _inspect_payloads=False,
                            )
                        return replace(
                            plan,
                            items=tuple(
                                replace(
                                    item,
                                    action="DEFERRED",
                                    reason="Snapshot retry limit reached",
                                )
                                if item.action in CHANGES
                                else item
                                for item in plan.items
                            ),
                            applied_items=tuple(applied),
                            rounds=rounds,
                            stop_reason="Snapshot retry limit reached",
                        )
            rounds += 1
            applied.extend(plan.changes)
            for item in plan.items:
                if item.action not in {"CONFLICT", "BLOCKED", "DEFERRED"}:
                    choices.pop(item.id, None)
            result = replace(plan, applied_items=tuple(applied), rounds=rounds)
            if not plan.deferred:
                return result
            if not plan.changes:
                return replace(
                    result,
                    stop_reason="No reconciliation progress; deferred prerequisites remain unresolved",
                )
