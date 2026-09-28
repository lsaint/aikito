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
from .workspace_core import Change, PathPolicy, WorkspaceCoreError, apply, version_at
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
    fingerprint: str
    name: str = ""
    source_path: Path | None = None
    before: str | None = None

    @property
    def id(self) -> str:
        return f"{self.kind}:{self.name}"


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


def _missing_ids(resource: Resource, resources: dict[str, Resource]) -> tuple[str, ...]:
    return tuple(
        reference
        for reference in resource.references
        if reference not in resources
        and not (
            reference.startswith("skill:")
            and reference.removeprefix("skill:") in BUNDLED_SKILL_NAMES
        )
    )


def missing_references(resources: dict[str, Resource]) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                f"{resource.id} references missing {reference}"
                for resource in resources.values()
                for reference in _missing_ids(resource, resources)
            }
        )
    )


def reference_conflicts(
    source: dict[str, Resource], target: dict[str, Resource], changes: set[str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """Skip invalid changes and their dependents until the result is valid."""
    changes = set(changes)
    rejected = {}
    while True:
        result = dict(target)
        result.update((key, source[key]) for key in changes)
        invalid = {}
        for key in changes:
            own = tuple(
                f"{key} references missing {reference}"
                for reference in _missing_ids(source[key], result)
            )
            if own:
                invalid[key] = "; ".join(own)
        if not invalid:
            return rejected, missing_references(result)
        rejected.update(invalid)
        changes.difference_update(invalid)


def _require_valid(snapshot: WorkspaceSnapshot, *, references: bool = False) -> None:
    findings = [f"{f.resource}: {f.message}" for f in snapshot.findings]
    if references:
        findings.extend(missing_references(snapshot.resources))
    if findings:
        raise WorkspaceCoreError("Invalid workspace resources: " + "; ".join(findings))


def apply_resource_writes(
    source_snapshot: WorkspaceSnapshot,
    target_snapshot: WorkspaceSnapshot,
    writes: tuple[ResourceWrite, ...],
    *,
    policy: PathPolicy,
) -> None:
    """Apply writes from fresh snapshots supplied under the caller's lock.

    File versions are checked before installation; a final snapshot verifies
    logical content. Reusing planning snapshots avoids another preflight scan.
    """
    left, right = source_snapshot, target_snapshot
    source, target = left.root, right.root
    _require_valid(left)
    _require_valid(right)
    _, merges = partition_writes(writes)
    expected = dict(right.resources)
    by_path: dict[Path, list[ResourceWrite]] = {}
    for write in writes:
        remote, local = left.resources.get(write.id), right.resources.get(write.id)
        if remote is None or remote.fingerprint != write.fingerprint:
            raise WorkspaceCoreError(f"Source changed before writing: {write.id}")
        source_path = write.source_path or write.relative_path
        if source_path != Path(remote.parts[0].path):
            raise WorkspaceCoreError(f"Unexpected source path: {write.id}")
        if write.kind != "inbox" and write.relative_path != source_path:
            raise WorkspaceCoreError(f"Unexpected destination path: {write.id}")
        if (local.fingerprint if local else None) != write.before:
            raise WorkspaceCoreError(f"Target changed before writing: {write.id}")
        expected[write.id] = remote
        by_path.setdefault(write.relative_path, []).append(write)
    findings = missing_references(expected)
    if findings:
        raise WorkspaceCoreError("Invalid resulting references: " + "; ".join(findings))
    expected_fingerprints = {key: r.fingerprint for key, r in expected.items()}

    def verify() -> None:
        actual = snapshot_workspace(target)
        _require_valid(actual, references=True)
        fingerprints = {key: r.fingerprint for key, r in actual.resources.items()}
        if fingerprints != expected_fingerprints:
            changed = sorted(
                key
                for key in fingerprints.keys() | expected_fingerprints.keys()
                if fingerprints.get(key) != expected_fingerprints.get(key)
            )
            raise WorkspaceCoreError(
                "Resource verification failed: " + ", ".join(changed)
            )

    # Capture whole-file versions before rendering, including target comments.
    storage = {}
    for relative, grouped in by_path.items():
        kinds = {physical_kind(w.kind) for w in grouped}
        if len(kinds) != 1:
            raise WorkspaceCoreError(f"Conflicting storage kinds: {relative}")
        kind = kinds.pop()
        storage[relative] = (
            kind,
            version_at(target, relative.as_posix(), kind, policy),
        )

    with tempfile.TemporaryDirectory(prefix="aikito-resource-write-") as staging:
        rendered = render_merged_files(source, target, merges)
        changes = []
        for relative, grouped in sorted(by_path.items()):
            kind, current = storage[relative]
            if relative in rendered:
                content = Path(staging) / relative
                content.parent.mkdir(parents=True, exist_ok=True)
                content.write_text(rendered[relative], encoding="utf-8")
            else:
                paths = {w.source_path or relative for w in grouped}
                if len(paths) != 1:
                    raise WorkspaceCoreError(f"Conflicting source paths: {relative}")
                content = source / paths.pop()
            changes.append(
                Change(
                    0,
                    relative.as_posix(),
                    kind,
                    content,
                    current.fingerprint if current else None,
                    fingerprint_resource(content, kind),
                )
            )
        if changes:
            apply((target,), tuple(changes), verify=verify, policy=policy)
