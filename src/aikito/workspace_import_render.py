"""Render merged workspace configuration without performing writes."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from .add import format_toml_key, format_toml_value, update_skills_in_toml
from .project_config import (
    DEFAULT_PROJECT_SYNC_MODE,
    add_candidate_path_to_content,
    get_project_candidate_paths,
)
from .workspace_import_decisions import ABSENT, nested_value, project_field_changes

if TYPE_CHECKING:
    from .workspace_import import ImportPlan


def _list_field(root: Path, relative: Path, key: str) -> list[str]:
    with (root / relative).open("rb") as stream:
        value = tomllib.load(stream).get(key, [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f"Invalid {key} in {root / relative}")
    return value


def _project_paths(root: Path, relative: Path) -> list[str]:
    with (root / relative).open("rb") as stream:
        document = tomllib.load(stream)
    return [raw for _, raw in get_project_candidate_paths(document)]


def _adopt_project_fields(text: str, updates: dict[str, object]) -> str:
    additions = []
    for key, value in updates.items():
        rendered = format_toml_value(value)
        if key == "sync_mode":
            match = re.search(
                rf"(?m)^(sync_mode\s*=\s*)(['\"]{re.escape(DEFAULT_PROJECT_SYNC_MODE)}['\"])(?=\s*(?:#.*)?$)",
                text,
            )
            if match:
                text = text[: match.start(2)] + rendered + text[match.end(2) :]
                continue
        additions.append(f"{format_toml_key(key)} = {rendered}\n")
    if additions:
        section = re.search(r"(?m)^\[", text)
        position = section.start() if section else len(text)
        prefix = text[:position]
        separator = "" if not prefix or prefix.endswith("\n") else "\n"
        text = prefix + separator + "".join(additions) + text[position:]
    return text


def _adopt_config_field(text: str, name: str, value: object) -> str:
    *sections, key = name.split(".")
    header = f"[{'.'.join(sections)}]" if sections else ""
    rendered = f"{format_toml_key(key)} = {format_toml_value(value)}"
    lines = text.splitlines(keepends=True)
    start = 0
    if header:
        for index, line in enumerate(lines):
            if line.strip() == header:
                start = index + 1
                break
        else:
            return text.rstrip("\n") + f"\n\n{header}\n{rendered}\n"
    end = next(
        (
            index
            for index in range(start, len(lines))
            if lines[index].lstrip().startswith("[")
        ),
        len(lines),
    )
    assignment = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for index in range(start, end):
        if assignment.match(lines[index]):
            comment = ""
            if "#" in lines[index]:
                comment = " #" + lines[index].partition("#")[2].rstrip("\r\n")
            lines[index] = rendered + comment + "\n"
            return "".join(lines)
    lines.insert(end, rendered + "\n")
    return "".join(lines)


def render_merged_files(plan: ImportPlan) -> dict[Path, str]:
    groups: dict[Path, set[str]] = {}
    for item in plan.items:
        if item.action == "MERGE":
            groups.setdefault(item.resource.relative_path, set()).add(
                item.resource.kind
            )
    result = {}
    for relative, kinds in groups.items():
        target_file = plan.target / relative
        text = target_file.read_text(encoding="utf-8") if target_file.exists() else ""
        if "project" in kinds:
            updates, conflicts = project_field_changes(
                plan.source / relative, plan.target / relative
            )
            if conflicts:
                raise ValueError(f"Project fields changed: {relative}")
            text = _adopt_project_fields(text, updates)
        if "skill-selection" in kinds or "project-skill" in kinds:
            values = sorted(
                set(_list_field(plan.source, relative, "skills"))
                | set(_list_field(plan.target, relative, "skills"))
            )
            text = update_skills_in_toml(text, values)
        if "project-path" in kinds:
            for value in _project_paths(plan.source, relative):
                updated = add_candidate_path_to_content(
                    text, value, Path.home(), match_resolved=False
                )
                if updated is not None:
                    text = updated
        if "config" in kinds:
            with (plan.source / relative).open("rb") as stream:
                source_config = tomllib.load(stream)
            for item in plan.items:
                if item.action == "MERGE" and item.resource.kind == "config":
                    value = nested_value(source_config, item.resource.name)
                    if value is ABSENT:
                        raise ValueError(
                            f"Missing source configuration field: {item.resource.name}"
                        )
                    text = _adopt_config_field(text, item.resource.name, value)
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid merged TOML: {relative}") from exc
        result[relative] = text
    return result
