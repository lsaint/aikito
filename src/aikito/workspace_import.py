"""Add supported resources from a source workspace without changing it."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from .skill_state import WorkspaceWriterLock
from .workspace_core import (
    Change,
    WorkspaceCoreError,
    apply,
    compare_import,
    entry_type,
    recover,
    read_supported_snapshot,
    validate_roots,
)
from .workspace_resources import is_ignored_name


class WorkspaceImportError(WorkspaceCoreError):
    """An import cannot proceed without risking an unsafe write."""


@dataclass(frozen=True)
class ImportResource:
    relative_path: Path
    kind: str
    fingerprint: str


@dataclass(frozen=True)
class ImportItem:
    resource: ImportResource
    action: str
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    source: Path
    target: Path
    items: tuple[ImportItem, ...]
    excluded: tuple[str, ...]
    findings: tuple[str, ...] = ()

    @property
    def conflicts(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CONFLICT")

    @property
    def creates(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CREATE")

    @property
    def blocked(self) -> bool:
        return bool(self.conflicts or self.findings)


_SECRET_PATTERN = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
    rb"\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"
)


def _check_credentials(source: Path, resources: tuple[ImportItem, ...]) -> None:
    for item in resources:
        path = source / item.resource.relative_path
        files = [path] if item.resource.kind == "memory" else path.rglob("*")
        for file in files:
            if (
                not any(is_ignored_name(part) for part in file.relative_to(path).parts)
                and entry_type(file) == "file"
                and _SECRET_PATTERN.search(file.read_bytes())
            ):
                raise WorkspaceImportError(
                    f"Possible plaintext credential in source file: {file}"
                )


def build_import_plan(source: Path, target: Path) -> ImportPlan:
    """Build a read-only additive plan from independent workspace snapshots."""
    try:
        source, target = validate_roots(source, target)
        source_versions, _, source_findings = read_supported_snapshot(source)
        target_versions, _, target_findings = read_supported_snapshot(target)
    except WorkspaceCoreError as exc:
        raise WorkspaceImportError(str(exc)) from exc
    items = []
    findings = [f"Source {message}" for message in source_findings]
    findings.extend(f"Target {message}" for message in target_findings)
    missing_projects = set()
    for decision in compare_import(source_versions, target_versions):
        version = source_versions[decision.path]
        resource = ImportResource(
            Path(decision.path), version.kind, version.fingerprint
        )
        if decision.path.startswith("projects/") and decision.action == "CREATE":
            project = Path(decision.path).parts[1]
            if entry_type(target / "projects" / project / "agent.toml") != "file":
                missing_projects.add(project)
                decision = decision.__class__(
                    decision.path,
                    decision.kind,
                    "BLOCKED",
                    None,
                    "Target project must exist before importing its memory",
                )
        items.append(ImportItem(resource, decision.action, decision.reason))
    for project in sorted(missing_projects):
        findings.append(
            f"Target project must exist before importing its memory: {project}. "
            f"Run aikito init project {project} first"
        )
    result = ImportPlan(
        source,
        target,
        tuple(items),
        ("Workspace configuration and unsupported resource kinds are excluded",),
        tuple(findings),
    )
    _check_credentials(source, result.items)
    return result


def recover_imports(target: Path) -> bool:
    """Recover a pending workspace import after a writer lock is acquired."""
    target = target.expanduser().resolve()
    legacy = target / ".local/state/aikito/imports"
    if entry_type(legacy) == "directory" and any(legacy.iterdir()):
        raise WorkspaceImportError(
            f"Legacy import transaction needs review before another write: {legacy}"
        )
    if entry_type(legacy) not in ("directory", "missing"):
        raise WorkspaceImportError(f"Unsafe legacy import state: {legacy}")
    try:
        return recover((target,))
    except WorkspaceCoreError as exc:
        raise WorkspaceImportError(str(exc)) from exc


def apply_import_plan(plan: ImportPlan, home: Path) -> None:
    """Apply a fresh, conflict-free import through the common transaction."""
    with WorkspaceWriterLock(home):
        if recover_imports(plan.target):
            raise WorkspaceImportError("Recovered an interrupted import; run again")
        fresh = build_import_plan(plan.source, plan.target)
        if fresh != plan:
            raise WorkspaceImportError("Workspace changed after planning; run again")
        if plan.blocked:
            raise WorkspaceImportError("Import has conflicts; no files changed")
        changes = tuple(
            Change(
                0,
                item.resource.relative_path.as_posix(),
                item.resource.kind,
                plan.source / item.resource.relative_path,
                None,
                item.resource.fingerprint,
            )
            for item in plan.creates
        )
        if changes:
            try:
                apply((plan.target,), changes)
            except WorkspaceCoreError as exc:
                raise WorkspaceImportError(str(exc)) from exc


def run_workspace_import(
    source: Path, target: Path, home: Path, *, dry_run: bool
) -> ImportPlan:
    """Preview or apply an additive workspace import."""
    if dry_run:
        return build_import_plan(source, target)
    with WorkspaceWriterLock(home):
        if recover_imports(target):
            raise WorkspaceImportError("Recovered an interrupted import; run again")
        plan = build_import_plan(source, target)
        if not plan.blocked:
            apply_import_plan(plan, home)
        return plan
