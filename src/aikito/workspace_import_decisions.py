"""Reusable import decisions and template baseline comparisons."""

from __future__ import annotations

import tomllib
from pathlib import Path

from .project_config import DEFAULT_PROJECT_SYNC_MODE
from .templating import TemplateError, load_template, render_project_files
from .workspace_resources import Resource, value_fingerprint

PROJECT_KINDS = frozenset(
    {
        "project",
        "project-path",
        "project-skill",
        "project-instructions",
        "project-memory",
    }
)
SET_KINDS = frozenset({"skill-selection", "project-path", "project-skill"})


def _project_name(resource: Resource) -> str:
    return resource.name.split("/", 1)[0]


def decide_resource(
    resource: Resource,
    target_resources: dict[str, Resource],
    source: Path,
    target: Path,
) -> tuple[str, str]:
    current = target_resources.get(resource.id)
    if resource.kind == "config":
        if current is None:
            return "MERGE", "Add workspace configuration field"
        if current.fingerprint == resource.fingerprint:
            return "NOOP", "Configuration field matches"
        baseline = _config_template_value(resource.name)
        baseline_fp = value_fingerprint(baseline) if baseline is not ABSENT else None
        if resource.fingerprint == baseline_fp:
            return "NOOP", "Target configuration field is customized"
        if current.fingerprint == baseline_fp:
            return "MERGE", "Adopt source configuration field over template"
        return "CONFLICT", "Both workspace configuration fields changed"
    if resource.kind == "agent":
        baseline = _agent_template_fingerprint(resource.name)
        return _baseline_decision(resource, current, baseline)
    if resource.kind == "global-instructions":
        if current is None:
            return "CREATE", "Global instructions are absent from target"
        source_text = (source / resource.parts[0].path).read_text(encoding="utf-8")
        target_text = (target / current.parts[0].path).read_text(encoding="utf-8")
        template = load_template("global/AGENTS.md")
        if source_text == target_text or source_text == template:
            return "NOOP", "Global instructions agree or source is the template"
        if target_text == template:
            return "UPDATE", "Replace unmodified global instructions template"
        return "CONFLICT", "Both global instructions differ from the template"
    if resource.kind in PROJECT_KINDS:
        project = _project_name(resource)
        if f"project:{project}" not in target_resources:
            if resource.kind in SET_KINDS:
                return "INCLUDED", "Included in the new project configuration"
            return "CREATE", "Create project resource in the target workspace"
    if resource.kind == "project":
        updates, conflicts = project_field_changes(
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
    if resource.kind in SET_KINDS:
        return ("NOOP", "Selection exists") if current else ("MERGE", "Add selection")
    if current is None:
        return "CREATE", "Resource is absent from target"
    if current.fingerprint == resource.fingerprint:
        return "NOOP", "Contents match"
    return "CONFLICT", "Contents differ"


ABSENT = object()


def nested_value(document: dict, name: str) -> object:
    current: object = document
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            return ABSENT
        current = current[part]
    return current


def _config_template_value(name: str) -> object:
    return nested_value(tomllib.loads(load_template("config.toml")), name)


def _agent_template_fingerprint(name: str) -> str | None:
    try:
        document = tomllib.loads(load_template(f"agents/{name}.toml"))
    except TemplateError:
        return None
    table = document.get("agents", {}).get(name)
    return value_fingerprint(table) if isinstance(table, dict) else None


def _baseline_decision(
    resource: Resource, current: Resource | None, baseline: str | None
) -> tuple[str, str]:
    if current is None:
        return "CREATE", "Resource is absent from target"
    if current.fingerprint == resource.fingerprint:
        return "NOOP", "Contents match"
    if baseline is not None and resource.fingerprint == baseline:
        return "NOOP", "Target is customized; source matches the template"
    if baseline is not None and current.fingerprint == baseline:
        return "UPDATE", "Replace unmodified template with source"
    return "CONFLICT", "Both sides differ from the template"


def project_field_changes(
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
            key == "sync_mode" and target_fields[key] == DEFAULT_PROJECT_SYNC_MODE
        ):
            updates[key] = value
        else:
            conflicts.append(key)
    return updates, tuple(sorted(conflicts))
