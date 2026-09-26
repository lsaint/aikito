"""Plan and apply additive imports using logical snapshots and one transaction."""

from __future__ import annotations

import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .add import _format_toml_key, _format_toml_value, _update_skills_in_toml
from .config import get_inbox_path
from .diagnostics import Finding
from .project_config import add_candidate_path_to_content, get_project_candidate_paths
from .skill_state import WorkspaceWriterLock
from .templating import BUNDLED_SKILL_NAMES, render_project_files
from .workspace_core import (
    Change,
    PathPolicy,
    WorkspaceCoreError,
    apply,
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


_SUPPORTED = frozenset(
    {
        "memory",
        "project-memory",
        "skill",
        "inbox",
        "skill-selection",
        "subagent",
        "mcp",
        "project",
        "project-path",
        "project-skill",
        "project-instructions",
    }
)
_SETS = frozenset({"skill-selection", "project-path", "project-skill"})
_PROJECT_KINDS = frozenset(
    {
        "project",
        "project-path",
        "project-skill",
        "project-instructions",
        "project-memory",
    }
)
_FILE_KINDS = {"project-memory": "memory", "project": "project-config"}


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

    @property
    def conflicts(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CONFLICT")

    @property
    def creates(self) -> tuple[ImportItem, ...]:
        return tuple(item for item in self.items if item.action == "CREATE")

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


def _policy(root: Path) -> PathPolicy:
    return PathPolicy(create_parents=True, inbox_prefix=_inbox_prefix(root) or "")


def _project_name(resource: Resource) -> str:
    return resource.name.split("/", 1)[0]


def _destination_path(resource: Resource, target: Path) -> Path:
    if resource.kind == "inbox":
        prefix = _inbox_prefix(target)
        if prefix is not None:
            return Path(prefix) / resource.name
    return Path(resource.parts[0].path)


def _decision(
    resource: Resource,
    target_resources: dict[str, Resource],
    source: Path,
    target: Path,
) -> tuple[str, str]:
    current = target_resources.get(resource.id)
    if resource.kind in _PROJECT_KINDS:
        project = _project_name(resource)
        if f"project:{project}" not in target_resources:
            if resource.kind in _SETS:
                return "INCLUDED", "Included in the new project configuration"
            return "CREATE", "Create project resource in the target workspace"
    if resource.kind == "project":
        updates, conflicts = _project_changes(
            source / resource.parts[0].path, target / resource.parts[0].path
        )
        if conflicts:
            return "CONFLICT", f"Project fields differ: {', '.join(conflicts)}"
        return (
            ("MERGE", "Adopt source project fields")
            if updates
            else ("NOOP", "Project fields match or target defaults remain")
        )
    if resource.kind == "project-instructions" and current is not None:
        template = render_project_files(target / "projects" / resource.name)[0][1]
        if (target / current.parts[0].path).read_text(encoding="utf-8") == template:
            if current.fingerprint != resource.fingerprint:
                return "UPDATE", "Replace unmodified project instructions template"
    if resource.kind in _SETS:
        return ("NOOP", "Selection exists") if current else ("MERGE", "Add selection")
    if current is None:
        return "CREATE", "Resource is absent from target"
    if current.fingerprint == resource.fingerprint:
        return "NOOP", "Contents match"
    return "CONFLICT", "Contents differ"


def _project_changes(
    source: Path, target: Path
) -> tuple[dict[str, object], tuple[str, ...]]:
    with source.open("rb") as stream:
        source_fields = tomllib.load(stream)
    with target.open("rb") as stream:
        target_fields = tomllib.load(stream)
    updates: dict[str, object] = {}
    conflicts = []
    for key, value in source_fields.items():
        if key in ("path", "paths", "skills") or target_fields.get(key) == value:
            continue
        if key not in target_fields or (
            key == "sync_mode" and target_fields[key] == "link"
        ):
            updates[key] = value
        else:
            conflicts.append(key)
    return updates, tuple(sorted(conflicts))


def _missing_references(
    source: dict[str, Resource],
    target: dict[str, Resource],
    items: tuple[ImportItem, ...],
) -> tuple[str, ...]:
    result = dict(target)
    for item in items:
        if item.action in ("CREATE", "MERGE", "UPDATE", "INCLUDED"):
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
    try:
        source, target = validate_roots(source, target)
        left, right = snapshot_workspace(source), snapshot_workspace(target)
    except (WorkspaceCoreError, WorkspaceResourceError) as exc:
        raise WorkspaceImportError(str(exc)) from exc
    findings = [f"Source {f.resource}: {f.message}" for f in left.findings]
    findings.extend(f"Target {f.resource}: {f.message}" for f in right.findings)
    if _inbox_prefix(source) is None:
        findings.append("Source inbox is outside the workspace")
    if _inbox_prefix(target) is None:
        findings.append("Target inbox is outside the workspace")
    items = []
    for resource in left.resources.values():
        if resource.kind not in _SUPPORTED:
            continue
        action, reason = _decision(resource, right.resources, source, target)
        destination = _destination_path(resource, target)
        if action in ("CREATE", "MERGE", "UPDATE"):
            kind = _FILE_KINDS.get(resource.kind, resource.kind)
            if resource.kind in _SETS:
                kind = (
                    "skills-config"
                    if resource.kind == "skill-selection"
                    else "project-config"
                )
            try:
                validate_resource_path(destination.as_posix(), kind, _policy(target))
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
        if item.action in ("CREATE", "MERGE", "UPDATE", "INCLUDED")
        and item.resource.source_path is not None
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
            | {f"SKIPPED Target {path}" for path in right.skipped}
            | {
                f"SKIPPED Source {resource.parts[0].path} ({resource.kind})"
                for resource in left.resources.values()
                if resource.kind not in _SUPPORTED
            }
        )
    )
    return ImportPlan(
        source, target, planned, skipped, tuple(sorted(set(findings))), warnings
    )


def _list_field(root: Path, relative: Path, key: str) -> list[str]:
    with (root / relative).open("rb") as stream:
        value = tomllib.load(stream).get(key, [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise WorkspaceImportError(f"Invalid {key} in {root / relative}")
    return value


def _project_paths(root: Path, relative: Path) -> list[str]:
    with (root / relative).open("rb") as stream:
        document = tomllib.load(stream)
    return [raw for _, raw in get_project_candidate_paths(document)]


def _adopt_project_fields(text: str, updates: dict[str, object]) -> str:
    additions = []
    for key, value in updates.items():
        rendered = _format_toml_value(value)
        if key == "sync_mode":
            match = re.search(
                r"(?m)^(sync_mode\s*=\s*)(['\"]link['\"])(?=\s*(?:#.*)?$)", text
            )
            if match:
                text = text[: match.start(2)] + rendered + text[match.end(2) :]
                continue
        additions.append(f"{_format_toml_key(key)} = {rendered}\n")
    if additions:
        section = re.search(r"(?m)^\[", text)
        position = section.start() if section else len(text)
        prefix = text[:position]
        separator = "" if not prefix or prefix.endswith("\n") else "\n"
        text = prefix + separator + "".join(additions) + text[position:]
    return text


def _merged_files(plan: ImportPlan) -> dict[Path, str]:
    groups: dict[Path, set[str]] = {}
    for item in plan.items:
        if item.action == "MERGE":
            groups.setdefault(item.resource.relative_path, set()).add(
                item.resource.kind
            )
    result = {}
    for relative, kinds in groups.items():
        text = (plan.target / relative).read_text(encoding="utf-8")
        if "project" in kinds:
            updates, conflicts = _project_changes(
                plan.source / relative, plan.target / relative
            )
            if conflicts:
                raise WorkspaceImportError(f"Project fields changed: {relative}")
            text = _adopt_project_fields(text, updates)
        if "skill-selection" in kinds or "project-skill" in kinds:
            values = sorted(
                set(_list_field(plan.source, relative, "skills"))
                | set(_list_field(plan.target, relative, "skills"))
            )
            text = _update_skills_in_toml(text, values)
        if "project-path" in kinds:
            for value in _project_paths(plan.source, relative):
                updated = add_candidate_path_to_content(
                    text, value, Path.home(), match_resolved=False
                )
                if updated is not None:
                    text = updated
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise WorkspaceImportError(f"Invalid merged TOML: {relative}") from exc
        result[relative] = text
    return result


def recover_imports(target: Path) -> bool:
    """Recover a pending workspace import after a writer lock is acquired."""
    target = target.expanduser().resolve()
    try:
        return recover((target,), policy=_policy(target))
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
            raise WorkspaceImportError(
                "Import has conflicts or findings; no files changed"
            )
        with tempfile.TemporaryDirectory(prefix="aikito-import-") as staging:
            staging_root = Path(staging)
            changes = []
            for item in plan.creates:
                resource = item.resource
                source = plan.source / (resource.source_path or resource.relative_path)
                kind = _FILE_KINDS.get(resource.kind, resource.kind)
                changes.append(
                    Change(
                        0,
                        resource.relative_path.as_posix(),
                        kind,
                        source,
                        None,
                        fingerprint_resource(source, kind),
                    )
                )
            for item in plan.items:
                if item.action != "UPDATE":
                    continue
                resource = item.resource
                source = plan.source / (resource.source_path or resource.relative_path)
                target = plan.target / resource.relative_path
                kind = _FILE_KINDS.get(resource.kind, resource.kind)
                changes.append(
                    Change(
                        0,
                        resource.relative_path.as_posix(),
                        kind,
                        source,
                        fingerprint_resource(target, kind),
                        fingerprint_resource(source, kind),
                    )
                )
            for relative, content in _merged_files(plan).items():
                generated = staging_root / relative
                generated.parent.mkdir(parents=True, exist_ok=True)
                generated.write_text(content, encoding="utf-8")
                kind = (
                    "skills-config"
                    if relative == Path("skills.toml")
                    else "project-config"
                )
                changes.append(
                    Change(
                        0,
                        relative.as_posix(),
                        kind,
                        generated,
                        fingerprint_resource(plan.target / relative, kind),
                        fingerprint_resource(generated, kind),
                    )
                )
            if changes:
                try:
                    apply((plan.target,), tuple(changes), policy=_policy(plan.target))
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
