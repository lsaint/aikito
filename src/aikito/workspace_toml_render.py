"""Render merged workspace configuration without performing writes."""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING

from .add import format_toml_key, format_toml_value, update_skills_in_toml
from .project_config import add_candidate_path_to_content, get_project_candidate_paths

if TYPE_CHECKING:
    from .workspace_resource_write import ResourceWrite


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


def _adopt_field(text: str, sections: list[str], key: str, value: object) -> str:
    """Set one TOML field, keeping other fields, comments, and formatting."""
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
    if end and not lines[end - 1].endswith("\n"):
        lines[end - 1] += "\n"
    lines.insert(end, rendered + "\n")
    return "".join(lines)


def _nested_value(document: dict, name: str) -> object:
    current: object = document
    for part in name.split("."):
        if not isinstance(current, dict) or part not in current:
            raise ValueError(f"Missing source configuration field: {name}")
        current = current[part]
    return current


def render_merged_files(
    source: Path, target: Path, changes: tuple[ResourceWrite, ...]
) -> dict[Path, str]:
    """Render existing shared TOML files that receive field or member changes."""
    groups: dict[Path, list[ResourceWrite]] = {}
    for item in changes:
        groups.setdefault(item.relative_path, []).append(item)
    result = {}
    for relative, items in groups.items():
        text = (target / relative).read_text(encoding="utf-8")
        with (source / relative).open("rb") as stream:
            source_document = tomllib.load(stream)
        kinds = {item.kind for item in items}
        for item in items:
            name = item.name
            if item.kind == "config":
                *sections, key = name.split(".")
                value = _nested_value(source_document, name)
                text = _adopt_field(text, sections, key, value)
            elif item.kind == "project-field":
                key = name.partition("/")[2]
                text = _adopt_field(text, [], key, source_document[key])
        if kinds & {"skill-selection", "project-skill"}:
            values = sorted(
                set(_list_field(source, relative, "skills"))
                | set(_list_field(target, relative, "skills"))
            )
            text = update_skills_in_toml(text, values)
        if "project-path" in kinds:
            for value in _project_paths(source, relative):
                updated = add_candidate_path_to_content(
                    text, value, Path.home(), match_resolved=False
                )
                if updated is not None:
                    text = updated
        try:
            tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError(f"Invalid merged TOML: {relative}") from exc
        result[relative] = text
    return result
