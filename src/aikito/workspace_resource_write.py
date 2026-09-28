"""Translate logical resource writes into one verified filesystem transaction.

Shared TOML fields are composed before staging. The transaction commits only
after a fresh snapshot matches the intended resources and keeps references
valid; verification failures use the same rollback as filesystem failures.
Callers own the writer lock and choose which logical changes are allowed.
"""

from __future__ import annotations

import tempfile
from dataclasses import dataclass
from pathlib import Path

from .templating import BUNDLED_SKILL_NAMES
from .workspace_core import (
    Change,
    PathPolicy,
    WorkspaceCoreError,
    apply,
    entry_type,
    version_at,
)
from .workspace_toml_render import render_merged_files
from .workspace_resources import (
    Resource,
    WorkspaceSnapshot,
    fingerprint_resource,
    is_shared_resource,
    physical_kind,
    snapshot_workspace,
)


@dataclass(frozen=True)
class ResourceWrite:
    relative_path: Path
    kind: str
    fingerprint: str | None
    name: str = ""
    source_path: Path | None = None
    before: str | None = None

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"


@dataclass(frozen=True)
class ResourceContent:
    """Validated resource metadata and content locations, independent of layout."""

    resources: dict[str, Resource]
    paths: dict[str, Path]
    workspace_root: Path | None = None

    @classmethod
    def from_workspace(cls, snapshot: WorkspaceSnapshot) -> ResourceContent:
        return cls(
            snapshot.resources,
            {
                key: snapshot.root / resource.parts[0].path
                for key, resource in snapshot.resources.items()
            },
            snapshot.root,
        )


def partition_writes(
    writes: tuple[ResourceWrite, ...],
) -> tuple[tuple[Path, ...], tuple[ResourceWrite, ...]]:
    """Group whole-file replacements and changes to existing shared files."""
    copies = set()
    merges = []
    for write in writes:
        if is_shared_resource(write.kind):
            merges.append(write)
        else:
            copies.add(write.relative_path)
    return tuple(sorted(copies)), tuple(merges)


def _missing_ids(
    resource: Resource,
    resources: dict[str, Resource],
    external: frozenset[str] = frozenset(),
) -> tuple[str, ...]:
    return tuple(
        reference
        for reference in resource.references
        if reference not in resources
        and reference not in external
        and not (
            reference.startswith("skill:")
            and reference.removeprefix("skill:") in BUNDLED_SKILL_NAMES
        )
    )


def missing_references(
    resources: dict[str, Resource], *, external: frozenset[str] = frozenset()
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                f"{resource.id} references missing {reference}"
                for resource in resources.values()
                for reference in _missing_ids(resource, resources, external)
            }
        )
    )


def reference_conflicts(
    source: dict[str, Resource],
    target: dict[str, Resource],
    changes: set[str],
    *,
    deletions: set[str] | None = None,
    external: frozenset[str] = frozenset(),
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Skip invalid changes and their dependents until the result is valid."""
    changes = set(changes)
    deletions = set(deletions or ())
    rejected = {}
    while True:
        result = dict(target)
        result.update((key, source[key]) for key in changes)
        for key in deletions:
            result.pop(key, None)
        invalid = {}
        for key, resource in sorted(result.items()):
            own = tuple(
                f"{key} references missing {reference}"
                for reference in _missing_ids(resource, result, external)
            )
            if own and key in changes:
                invalid[key] = "; ".join(own)
            elif own:
                for reference in _missing_ids(resource, result, external):
                    if reference in deletions:
                        invalid[reference] = (
                            f"Deleting {reference} would invalidate {key}"
                        )
        if not invalid:
            return rejected, missing_references(result, external=external)
        rejected.update(invalid)
        changes.difference_update(invalid)
        deletions.difference_update(invalid)


def _require_valid(snapshot: WorkspaceSnapshot, *, references: bool = False) -> None:
    findings = [f"{f.resource}: {f.message}" for f in snapshot.findings]
    if references:
        findings.extend(missing_references(snapshot.resources))
    if findings:
        raise WorkspaceCoreError("Invalid workspace resources: " + "; ".join(findings))


def verify_resource_snapshot(
    actual: WorkspaceSnapshot,
    expected: dict[str, Resource],
    *,
    external: frozenset[str] = frozenset(),
) -> None:
    """Verify all managed resources, including preserved target-only content."""
    _require_valid(actual)
    findings = missing_references(actual.resources, external=external)
    if findings:
        raise WorkspaceCoreError("Invalid resulting references: " + "; ".join(findings))
    fingerprints = {key: r.fingerprint for key, r in actual.resources.items()}
    intended = {key: r.fingerprint for key, r in expected.items()}
    if fingerprints != intended:
        changed = sorted(
            key
            for key in fingerprints.keys() | intended.keys()
            if fingerprints.get(key) != intended.get(key)
        )
        raise WorkspaceCoreError("Resource verification failed: " + ", ".join(changed))


def prepare_resource_writes(
    content: ResourceContent,
    target_snapshot: WorkspaceSnapshot,
    writes: tuple[ResourceWrite, ...],
    staging: Path,
    *,
    policy: PathPolicy,
    external: frozenset[str] = frozenset(),
) -> tuple[tuple[Change, ...], dict[str, Resource]]:
    """Compose logical writes or deletions before the caller's atomic commit.

    The content provider supplies files by ID; only shared TOML rendering needs
    a workspace source. Callers keep staging alive until the transaction ends.
    """
    right = target_snapshot
    target = right.root
    _require_valid(right)
    expected = dict(right.resources)
    by_path: dict[Path, list[ResourceWrite]] = {}
    for write in writes:
        remote, local = content.resources.get(write.id), right.resources.get(write.id)
        if (local.fingerprint if local else None) != write.before:
            raise WorkspaceCoreError(f"Target changed before writing: {write.id}")
        if write.fingerprint is None:
            if local is None or is_shared_resource(write.kind):
                raise WorkspaceCoreError(f"Unsupported resource deletion: {write.id}")
            if write.relative_path != Path(local.parts[0].path):
                raise WorkspaceCoreError(f"Unexpected destination path: {write.id}")
            expected.pop(write.id)
        else:
            if remote is None or remote.fingerprint != write.fingerprint:
                raise WorkspaceCoreError(f"Source changed before writing: {write.id}")
            source_path = write.source_path or Path(remote.parts[0].path)
            if source_path != Path(remote.parts[0].path):
                raise WorkspaceCoreError(f"Unexpected source path: {write.id}")
            if write.kind != "inbox" and write.relative_path != source_path:
                raise WorkspaceCoreError(f"Unexpected destination path: {write.id}")
            expected[write.id] = remote
        by_path.setdefault(write.relative_path, []).append(write)
    findings = missing_references(expected, external=external)
    if findings:
        raise WorkspaceCoreError("Invalid resulting references: " + "; ".join(findings))
    storage = {}
    for relative, grouped in by_path.items():
        kinds = {physical_kind(w.kind) for w in grouped}
        if len(kinds) != 1:
            raise WorkspaceCoreError(f"Conflicting storage kinds: {relative}")
        kind = kinds.pop()
        current = version_at(target, relative.as_posix(), kind, policy)
        if (
            grouped[0].kind in {"memory", "project-memory", "skill"}
            and (current.fingerprint if current else None) != grouped[0].before
        ):
            raise WorkspaceCoreError(
                f"Unmanaged or changed target resource: {relative}"
            )
        storage[relative] = (kind, current)
    _, merges = partition_writes(writes)
    if merges and content.workspace_root is None:
        raise WorkspaceCoreError("Shared TOML writes require workspace content")
    rendered = (
        render_merged_files(content.workspace_root, target, merges) if merges else {}
    )
    changes = []
    for relative, grouped in sorted(by_path.items()):
        kind, current = storage[relative]
        if all(write.fingerprint is None for write in grouped):
            source = None
            after = None
        elif any(write.fingerprint is None for write in grouped):
            raise WorkspaceCoreError(f"Conflicting deletion and update: {relative}")
        else:
            if relative in rendered:
                source = staging / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text(rendered[relative], encoding="utf-8")
            else:
                paths = {content.paths.get(write.id) for write in grouped}
                if len(paths) != 1 or None in paths:
                    raise WorkspaceCoreError(
                        f"Missing or conflicting content: {relative}"
                    )
                source = paths.pop()
                if kind == "skill" and entry_type(source / "SKILL.md") != "file":
                    raise WorkspaceCoreError(
                        f"Skill content lacks a regular SKILL.md: {grouped[0].id}"
                    )
            after = fingerprint_resource(source, kind)
            if (
                grouped[0].kind in {"memory", "project-memory", "skill"}
                and after != grouped[0].fingerprint
            ):
                raise WorkspaceCoreError(
                    f"Source changed before writing: {grouped[0].id}"
                )
        changes.append(
            Change(
                0,
                relative.as_posix(),
                kind,
                source,
                current.fingerprint if current else None,
                after,
            )
        )
    return tuple(changes), expected


def apply_resource_writes(
    source_snapshot: WorkspaceSnapshot,
    target_snapshot: WorkspaceSnapshot,
    writes: tuple[ResourceWrite, ...],
    *,
    policy: PathPolicy,
) -> None:
    """Apply the validated resource batch under the caller's writer lock."""
    _require_valid(source_snapshot)
    with tempfile.TemporaryDirectory(prefix="aikito-resource-write-") as staging:
        changes, expected = prepare_resource_writes(
            ResourceContent.from_workspace(source_snapshot),
            target_snapshot,
            writes,
            Path(staging),
            policy=policy,
        )
        if changes:
            apply(
                (target_snapshot.root,),
                changes,
                verify=lambda: verify_resource_snapshot(
                    snapshot_workspace(target_snapshot.root), expected
                ),
                policy=policy,
            )
