"""Plan and apply additive imports using logical snapshots and one transaction."""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path

from .config import LEGACY_DEFAULT_INBOX_PATH, get_inbox_path
from .diagnostics import Finding
from .init import is_recognized_workspace
from .skill_state import WorkspaceWriterLock
from .templating import BUNDLED_SKILL_NAMES
from .workspace_import_decisions import decide_resource
from .workspace_import_render import render_merged_files
from .workspace_core import (
    Change,
    PathPolicy,
    WorkspaceCoreError,
    apply,
    entry_type,
    recover,
    validate_resource_path,
    validate_roots,
)
from .workspace_resources import (
    Resource,
    WorkspaceResourceError,
    fingerprint_resource,
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


# Resources stored as fields or members of a shared TOML file, by file kind.
IMPORT_SHARED_KINDS = {
    "config": "workspace-config",
    "skill-selection": "skills-config",
    "project": "project-config",
    "project-field": "project-config",
    "project-path": "project-config",
    "project-skill": "project-config",
}
# Resources stored as one standalone file or directory, by file kind.
_FILE_KINDS = {
    "memory": "memory",
    "project-memory": "memory",
    "skill": "skill",
    "inbox": "inbox",
    "subagent": "subagent",
    "mcp": "mcp",
    "agent": "agent",
    "global-instructions": "global-instructions",
    "project-instructions": "project-instructions",
}
_SUPPORTED = frozenset(IMPORT_SHARED_KINDS) | frozenset(_FILE_KINDS)
_CHANGES = ("CREATE", "UPDATE")


def _physical_kind(kind: str) -> str:
    return IMPORT_SHARED_KINDS.get(kind) or _FILE_KINDS[kind]


@dataclass(frozen=True)
class ImportResource:
    relative_path: Path
    kind: str
    fingerprint: str
    name: str = ""
    source_path: Path | None = None


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
    warnings: tuple[Finding, ...] = ()
    inbox_prefix: str = ""

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
        paths = {
            item.resource.relative_path
            for item in self.changes
            if item.resource.kind in _FILE_KINDS
            or not (self.target / item.resource.relative_path).exists()
        }
        return tuple(sorted(paths))

    @property
    def merges(self) -> tuple[ImportItem, ...]:
        """Field and member changes rendered into an existing target file."""
        return tuple(
            item
            for item in self.changes
            if item.resource.kind in IMPORT_SHARED_KINDS
            and (self.target / item.resource.relative_path).exists()
        )

    @property
    def blocked(self) -> bool:
        return bool(
            self.findings
            or any(item.action in ("CONFLICT", "BLOCKED") for item in self.items)
        )


def _inbox_prefix(root: Path) -> str | None:
    try:
        relative = get_inbox_path(root).relative_to(root)
    except ValueError:
        return None
    return relative.as_posix() if relative.parts else None


def _policy(
    root: Path, *, destination_prefix: str = "", recovery_prefix: str = ""
) -> PathPolicy:
    current = _inbox_prefix(root) or ""
    primary = destination_prefix or current
    extras = tuple(
        prefix for prefix in (current, recovery_prefix) if prefix and prefix != primary
    )
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


def _planned_inbox_prefix(
    source: Path, target: Path, left: dict[str, Resource], right: dict[str, Resource]
) -> str | None:
    source_field = left.get("config:inbox.path")
    if source_field is not None:
        action, _ = decide_resource(source_field, right)
        if action in _CHANGES:
            return _configured_inbox_prefix(source / "config.toml", target)
    return _inbox_prefix(target)


def _destination_path(resource: Resource, target: Path, inbox_prefix: str) -> Path:
    if resource.kind == "inbox":
        if inbox_prefix:
            return Path(inbox_prefix) / resource.name
    return Path(resource.parts[0].path)


def _missing_references(
    source: dict[str, Resource],
    target: dict[str, Resource],
    items: tuple[ImportItem, ...],
) -> tuple[str, ...]:
    result = dict(target)
    for item in items:
        if item.action in _CHANGES:
            resource = source[f"{item.resource.kind}:{item.resource.name}"]
            result[resource.id] = resource
    findings = set()
    for resource in result.values():
        if resource.kind not in ("mcp", "subagent", "skill-selection", "project-skill"):
            continue
        for reference in resource.references:
            if reference not in result and not (
                reference.startswith("skill:")
                and reference.removeprefix("skill:") in BUNDLED_SKILL_NAMES
            ):
                findings.add(f"{resource.id} references missing {reference}")
    return tuple(sorted(findings))


def build_import_plan(source: Path, target: Path) -> ImportPlan:
    """Build a read-only plan for the first import resource batch."""
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
    findings = [f"Source {f.resource}: {f.message}" for f in left.findings]
    findings.extend(f"Target {f.resource}: {f.message}" for f in right.findings)
    if _inbox_prefix(source) is None:
        findings.append("Source inbox is outside the workspace")
    if _inbox_prefix(target) is None:
        findings.append("Target inbox is outside the workspace")
    planned_inbox = _planned_inbox_prefix(
        source, target, left.resources, right.resources
    )
    if planned_inbox is None:
        findings.append("Imported inbox path would leave the target workspace")
    inbox_prefix = planned_inbox or ""
    items = []
    for resource in left.resources.values():
        if resource.kind not in _SUPPORTED:
            continue
        action, reason = decide_resource(resource, right.resources)
        destination = _destination_path(resource, target, inbox_prefix)
        if action in _CHANGES:
            try:
                validate_resource_path(
                    destination.as_posix(),
                    _physical_kind(resource.kind),
                    _policy(target, destination_prefix=inbox_prefix),
                )
            except WorkspaceCoreError as exc:
                action, reason = "BLOCKED", str(exc)
        items.append(
            ImportItem(
                ImportResource(
                    destination,
                    resource.kind,
                    resource.fingerprint,
                    resource.name,
                    Path(resource.parts[0].path),
                ),
                action,
                reason,
            )
        )
        if action == "BLOCKED":
            findings.append(reason)
    planned = tuple(items)
    findings.extend(_missing_references(left.resources, right.resources, planned))
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
    )
    if not plan.blocked:
        try:
            render_merged_files(plan.source, plan.target, plan.merges)
        except (WorkspaceImportError, OSError, ValueError) as exc:
            message = (
                str(exc)
                if isinstance(exc, (WorkspaceImportError, ValueError))
                else "Cannot render merged TOML; inspect source and target collection fields"
            )
            plan = replace(plan, findings=(message,))
    return plan


def recover_imports(target: Path) -> bool:
    """Recover a pending workspace import after a writer lock is acquired."""
    target = target.expanduser().resolve()
    try:
        return recover(
            (target,),
            policy=_policy(target, recovery_prefix=_staged_inbox_prefix(target)),
        )
    except WorkspaceCoreError as exc:
        raise WorkspaceImportError(str(exc)) from exc


def _staged_inbox_prefix(target: Path) -> str:
    """Recover paths planned for a config update that was not installed yet."""
    journal = target / ".local/state/aikito/workspace-transactions/pending.json"
    if entry_type(journal) != "file":
        return ""
    try:
        data = json.loads(journal.read_text(encoding="utf-8"))
        txid = data.get("txid")
        if not isinstance(txid, str) or not re.fullmatch(r"[0-9a-f]{32}", txid):
            return ""
        if not any(
            item.get("kind") == "workspace-config" and item.get("path") == "config.toml"
            for item in data.get("changes", [])
            if isinstance(item, dict)
        ):
            return ""
        staged = journal.parent / "tx" / txid / "stage/config.toml"
        if entry_type(staged) == "file":
            return _configured_inbox_prefix(staged, target) or ""
    except (OSError, ValueError, TypeError, AttributeError):
        return ""
    return ""


def apply_import_plan(plan: ImportPlan, home: Path) -> None:
    """Apply a fresh, conflict-free import through the common transaction."""
    with WorkspaceWriterLock(home):
        if recover_imports(plan.target):
            raise WorkspaceImportError("Recovered an interrupted import; run again")
        fresh = build_import_plan(plan.source, plan.target)
        if fresh != plan:
            raise WorkspaceImportError("Workspace changed after planning; run again")
        if plan.blocked:
            raise WorkspaceImportError(
                "Import has conflicts or findings; no files changed"
            )
        with tempfile.TemporaryDirectory(prefix="aikito-import-") as staging:
            staging_root = Path(staging)
            kinds = {
                item.resource.relative_path: _physical_kind(item.resource.kind)
                for item in plan.changes
            }
            sources = {
                item.resource.relative_path: plan.source
                / (item.resource.source_path or item.resource.relative_path)
                for item in plan.changes
            }
            changes = [
                _change(relative, kinds[relative], sources[relative], plan.target)
                for relative in plan.copies
            ]
            merged = render_merged_files(plan.source, plan.target, plan.merges)
            for relative, content in merged.items():
                generated = staging_root / relative
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_text(content, encoding="utf-8")
                changes.append(
                    _change(relative, kinds[relative], generated, plan.target)
                )
            if changes:
                try:
                    apply(
                        (plan.target,),
                        tuple(changes),
                        policy=_policy(
                            plan.target, destination_prefix=plan.inbox_prefix
                        ),
                    )
                except WorkspaceCoreError as exc:
                    raise WorkspaceImportError(str(exc)) from exc


def _change(relative: Path, kind: str, source: Path, target: Path) -> Change:
    current = target / relative
    return Change(
        0,
        relative.as_posix(),
        kind,
        source,
        fingerprint_resource(current, kind) if current.exists() else None,
        fingerprint_resource(source, kind),
    )


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
