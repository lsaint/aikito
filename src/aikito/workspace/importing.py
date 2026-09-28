"""Plan and apply additive imports using logical snapshots and one transaction."""

from __future__ import annotations

import os
import shlex
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from pathlib import Path

from ..config import LEGACY_DEFAULT_INBOX_PATH, get_inbox_path
from ..diagnostics import Finding
from ..init import is_recognized_workspace
from ..skill_state import WorkspaceWriterLock
from .import_decisions import decide_resource
from .toml_render import render_merged_files
from .resource_write import (
    ResourceWrite,
    apply_resource_writes,
    reference_conflicts,
    partition_writes,
)
from .transactions import (
    PathPolicy,
    WorkspaceCoreError,
    recover,
    validate_resource_path,
    validate_roots,
)
from .resources import (
    RESOURCE_STORAGE,
    Resource,
    WorkspaceSnapshot,
    is_shared_resource,
    physical_kind,
    WorkspaceResourceError,
    scan_credentials,
    snapshot_workspace,
)


class WorkspaceImportError(WorkspaceCoreError):
    """An import cannot proceed without risking an unsafe write."""


def _source_migration_command(source: Path) -> str:
    if os.name == "nt":
        quoted = str(source).replace("'", "''")
        return f"$env:AIKITO_DIR = '{quoted}'; aikito migrate workspace-resources"
    return f"AIKITO_DIR={shlex.quote(str(source))} aikito migrate workspace-resources"


_SUPPORTED = frozenset(RESOURCE_STORAGE)
_CHANGES = ("CREATE", "UPDATE")


@dataclass(frozen=True)
class ImportItem:
    resource: ResourceWrite
    action: str
    reason: str


@dataclass(frozen=True)
class ImportPlan:
    source: Path
    target: Path
    items: tuple[ImportItem, ...]
    excluded: tuple[str, ...]
    findings: tuple[str, ...] = ()
    warnings: tuple[Finding, ...] = ()
    inbox_prefix: str = ""
    resolutions: tuple[tuple[str, str], ...] = ()
    new_files: tuple[Path, ...] = ()

    @property
    def conflicts(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CONFLICT")

    @property
    def creates(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CREATE")

    @property
    def changes(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action in _CHANGES)

    @property
    def copies(self) -> tuple[Path, ...]:
        """Target paths replaced by a whole source file or skill directory."""
        return partition_writes(tuple(item.resource for item in self.changes))[0]

    @property
    def merges(self) -> tuple[ImportItem, ...]:
        """Field and member changes rendered into an existing target file."""
        return tuple(
            item for item in self.changes if is_shared_resource(item.resource.kind)
        )

    @property
    def blocked(self) -> bool:
        return bool(
            self.findings or any(item.action == "BLOCKED" for item in self.items)
        )


def _inbox_prefix(root: Path) -> str | None:
    try:
        relative = get_inbox_path(root).relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _policy(root: Path, *, destination_prefix: str = "") -> PathPolicy:
    current = _inbox_prefix(root) or ""
    primary = destination_prefix or current
    extras = tuple(prefix for prefix in (current,) if prefix and prefix != primary)
    return PathPolicy(
        create_parents=True, inbox_prefix=primary, extra_inbox_prefixes=extras
    )


def _configured_inbox_prefix(config_file: Path, target: Path) -> str | None:
    with config_file.open("rb") as stream:
        raw = tomllib.load(stream).get("inbox", {}).get("path", "inbox")
    if not isinstance(raw, str):
        return None
    candidate = Path(raw.strip() or "inbox").expanduser()
    if ".." in candidate.parts:
        return None
    if candidate == Path(LEGACY_DEFAULT_INBOX_PATH).expanduser():
        candidate = target / "inbox"
    elif not candidate.is_absolute():
        candidate = target / candidate
    try:
        relative = candidate.resolve().relative_to(target)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _destination_path(resource: Resource, target: Path, inbox_prefix: str) -> Path:
    if resource.kind == "inbox":
        if inbox_prefix:
            return Path(inbox_prefix) / resource.name
    return Path(resource.parts[0].path)


def build_import_plan(
    source: Path, target: Path, *, resolutions: Mapping[str, str] | None = None
) -> ImportPlan:
    """Build a read-only plan for an additive workspace import."""
    return _build_import_plan(source, target, resolutions=resolutions)[0]


def _build_import_plan(
    source: Path, target: Path, *, resolutions: Mapping[str, str] | None = None
) -> tuple[ImportPlan, WorkspaceSnapshot, WorkspaceSnapshot]:
    """Retain the validated snapshots for execution under the writer lock."""
    source, target = source.expanduser().resolve(), target.expanduser().resolve()
    if not is_recognized_workspace(source):
        raise WorkspaceImportError(f"Source is not an Aikito workspace: {source}")
    if not is_recognized_workspace(target):
        raise WorkspaceImportError(f"Target is not an Aikito workspace: {target}")
    try:
        source, target = validate_roots(source, target)
        left = snapshot_workspace(source)
    except WorkspaceResourceError as exc:
        message = str(exc).replace(
            "aikito migrate workspace-resources", _source_migration_command(source)
        )
        raise WorkspaceImportError(f"Source {message}") from exc
    except WorkspaceCoreError as exc:
        raise WorkspaceImportError(str(exc)) from exc
    try:
        right = snapshot_workspace(target)
    except WorkspaceResourceError as exc:
        raise WorkspaceImportError(f"Target {exc}") from exc
    choices = dict(resolutions or {})
    for identity, side in choices.items():
        if (
            identity not in left.resources
            or left.resources[identity].kind not in _SUPPORTED
        ):
            raise WorkspaceImportError(f"Unknown import resource ID: {identity}")
        if side not in ("target", "source"):
            raise WorkspaceImportError(f"Invalid resolution for {identity}: {side}")
    findings = [f"Source {f.resource}: {f.message}" for f in left.findings]
    findings.extend(f"Target {f.resource}: {f.message}" for f in right.findings)
    if _inbox_prefix(source) is None:
        findings.append("Source inbox is outside the workspace")
    current_inbox = _inbox_prefix(target)
    if current_inbox is None:
        findings.append("Target inbox is outside the workspace")
    inbox_field = left.resources.get("config:inbox.path")
    inbox_action = "NOOP"
    if inbox_field is not None:
        inbox_action, _ = decide_resource(inbox_field, right.resources)
        if choices.get(inbox_field.id) == "target":
            inbox_action = "NOOP"
        elif inbox_action == "CONFLICT":
            inbox_action = {"target": "NOOP", "source": "UPDATE"}.get(
                choices.get(inbox_field.id), inbox_action
            )
    planned_inbox = (
        _configured_inbox_prefix(source / "config.toml", target)
        if inbox_action in _CHANGES
        else current_inbox
    )
    if planned_inbox is None:
        findings.append("Imported inbox path would leave the target workspace")
    inbox_prefix = planned_inbox or ""
    target_notes = sum(r.kind == "inbox" for r in right.resources.values())
    items = []
    for resource in left.resources.values():
        if resource.kind not in _SUPPORTED:
            continue
        action, reason = decide_resource(resource, right.resources)
        if choices.get(resource.id) == "target":
            action, reason = "NOOP", "Skipped by --keep-target; target kept unchanged"
        elif action == "CONFLICT" and choices.get(resource.id) == "source":
            action, reason = "UPDATE", "Conflict resolved using source"
        destination = _destination_path(resource, target, inbox_prefix)
        if (
            resource.id == "config:inbox.path"
            and action in _CHANGES
            and current_inbox is not None
            and planned_inbox is not None
            and planned_inbox != current_inbox
            and target_notes
        ):
            action = "BLOCKED"
            noun = "note" if target_notes == 1 else "notes"
            reason = (
                f"Target inbox contains {target_notes} {noun} under {current_inbox}/; "
                f"changing inbox.path to {planned_inbox}/ would leave them unmanaged. "
                "Move or remove the existing notes before changing the inbox path"
            )
        if action in _CHANGES:
            try:
                validate_resource_path(
                    destination.as_posix(),
                    physical_kind(resource.kind),
                    _policy(target, destination_prefix=inbox_prefix),
                )
            except WorkspaceCoreError as exc:
                action, reason = "BLOCKED", str(exc)
        items.append(
            ImportItem(
                ResourceWrite(
                    destination,
                    resource.kind,
                    resource.fingerprint,
                    resource.name,
                    Path(resource.parts[0].path),
                    right.resources[resource.id].fingerprint
                    if resource.id in right.resources
                    else None,
                ),
                action,
                reason,
            )
        )
        if action == "BLOCKED":
            findings.append(reason)
    rejected, reference_findings = reference_conflicts(
        left.resources,
        right.resources,
        {item.resource.id for item in items if item.action in _CHANGES},
    )
    planned = tuple(
        replace(
            item,
            action="CONFLICT",
            reason=rejected[item.resource.id],
        )
        if item.resource.id in rejected
        else item
        for item in items
    )
    findings.extend(reference_findings)
    imported_paths = {
        item.resource.source_path.as_posix()
        for item in planned
        if item.action in _CHANGES and item.resource.source_path is not None
    }
    warnings = tuple(
        finding
        for finding in scan_credentials(left)
        if any(
            finding.resource == path or finding.resource.startswith(f"{path}/")
            for path in imported_paths
        )
    )
    skipped = tuple(
        sorted(
            {f"SKIPPED Source {path}" for path in left.skipped}
            | {
                f"SKIPPED Source {resource.parts[0].path} ({resource.kind})"
                for resource in left.resources.values()
                if resource.kind not in _SUPPORTED
            }
        )
    )
    plan = ImportPlan(
        source,
        target,
        planned,
        skipped,
        tuple(sorted(set(findings))),
        warnings,
        inbox_prefix,
        tuple(sorted(choices.items())),
        tuple(
            sorted(
                {
                    item.resource.relative_path
                    for item in planned
                    if item.action in _CHANGES
                    and not (target / item.resource.relative_path).exists()
                }
            )
        ),
    )
    if not plan.blocked:
        try:
            render_merged_files(
                plan.source, plan.target, tuple(item.resource for item in plan.merges)
            )
        except (WorkspaceImportError, OSError, ValueError) as exc:
            message = (
                str(exc)
                if isinstance(exc, (WorkspaceImportError, ValueError))
                else "Cannot render merged TOML; inspect source and target collection fields"
            )
            plan = replace(plan, findings=(message,))
    return plan, left, right


def recover_imports(target: Path) -> bool:
    """Recover a pending workspace import after a writer lock is acquired."""
    target = target.expanduser().resolve()
    try:
        return recover(
            (target,),
            policy=_policy(target),
        )
    except WorkspaceCoreError as exc:
        raise WorkspaceImportError(str(exc)) from exc


def apply_import_plan(plan: ImportPlan, home: Path) -> None:
    """Apply the safe subset of a fresh import through the common transaction."""
    with WorkspaceWriterLock(home):
        if recover_imports(plan.target):
            raise WorkspaceImportError("Recovered an interrupted import; run again")
        fresh, left, right = _build_import_plan(
            plan.source, plan.target, resolutions=dict(plan.resolutions)
        )
        if fresh != plan:
            raise WorkspaceImportError("Workspace changed after planning; run again")
        if plan.blocked:
            raise WorkspaceImportError("Import has blocking findings; no files changed")
        try:
            apply_resource_writes(
                left,
                right,
                tuple(item.resource for item in plan.changes),
                policy=_policy(plan.target, destination_prefix=plan.inbox_prefix),
            )
        except (WorkspaceCoreError, WorkspaceResourceError) as exc:
            raise WorkspaceImportError(str(exc)) from exc


def run_workspace_import(
    source: Path,
    target: Path,
    home: Path,
    *,
    dry_run: bool,
    resolutions: Mapping[str, str] | None = None,
) -> ImportPlan:
    """Preview or apply an additive workspace import."""
    if dry_run:
        return build_import_plan(source, target, resolutions=resolutions)
    with WorkspaceWriterLock(home):
        if recover_imports(target):
            raise WorkspaceImportError("Recovered an interrupted import; run again")
        plan = build_import_plan(source, target, resolutions=resolutions)
        if not plan.blocked:
            apply_import_plan(plan, home)
        return plan
