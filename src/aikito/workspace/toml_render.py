"""Render merged workspace configuration without performing writes."""

from __future__ import annotations

import re
import tomllib
from datetime import date, time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..add import format_toml_key, format_toml_value, update_skills_in_toml
from ..project_config import add_candidate_path_to_content

if TYPE_CHECKING:
    from .resource_write import ResourceWrite


@dataclass(frozen=True, eq=False)
class TomlValue:
    """A field's actual key components and typed value, independent of files."""

    path: tuple[str, ...]
    value: object

    def __eq__(self, other: object) -> bool:
        return isinstance(other, TomlValue) and self.encode() == other.encode()

    def encode(self) -> dict:
        return {"path": list(self.path), "toml": f"value = {_toml_value(self.value)}\n"}

    @classmethod
    def decode(cls, raw: object) -> TomlValue:
        if (
            not isinstance(raw, dict)
            or not isinstance(raw.get("path"), list)
            or not raw["path"]
            or any(not isinstance(key, str) for key in raw["path"])
            or not isinstance(raw.get("toml"), str)
        ):
            raise ValueError("Invalid TOML resource value")
        parsed = tomllib.loads(raw["toml"])
        if set(parsed) != {"value"}:
            raise ValueError("Invalid TOML resource value")
        return cls(tuple(raw["path"]), parsed["value"])


def document_values(
    document: dict, prefix: tuple[str, ...] = ()
) -> dict[str, TomlValue]:
    result = {}
    for key, value in document.items():
        path = (*prefix, key)
        if isinstance(value, dict):
            result.update(document_values(value, path))
        else:
            result[".".join(path)] = TomlValue(path, value)
    return result


def _remove_value(document: dict, path: tuple[str, ...]) -> None:
    node = document
    parents = []
    for key in path[:-1]:
        child = node.get(key)
        if not isinstance(child, dict):
            return
        parents.append((node, key))
        node = child
    node.pop(path[-1], None)
    for parent, key in reversed(parents):
        if not parent[key]:
            parent.pop(key)
        else:
            break


def render_sync_file(
    text: str, items: list[ResourceWrite], values: dict[str, TomlValue], expected: dict
) -> str | None:
    """Compose a shared TOML result from selected changes and preserved values.

    Sync can delete fields and set members. Rebuilding the typed document keeps
    unrelated values, including literal dotted keys and multiline values; the
    legacy Import renderer continues to preserve its existing formatting.
    """
    document = tomllib.loads(text)
    for item in sorted(items, key=lambda item: item.fingerprint is not None):
        if item.kind not in {"config", "project-field"}:
            continue
        current = (
            document_values(document).get(item.name)
            if item.kind == "config"
            else TomlValue(
                (item.name.partition("/")[2],),
                document.get(item.name.partition("/")[2]),
            )
        )
        field = values.get(item.id) if item.fingerprint is not None else current
        if field is None:
            if item.fingerprint is None:
                continue
            raise ValueError(f"Missing TOML value: {item.id}")
        if current is not None and field.path != current.path:
            _remove_value(document, current.path)
        node = document
        parents = []
        for key in field.path[:-1]:
            child = node.setdefault(key, {})
            if not isinstance(child, dict):
                raise ValueError(f"Conflicting TOML field: {item.id}")
            parents.append((node, key))
            node = child
        key = field.path[-1]
        if item.fingerprint is None:
            node.pop(key, None)
            for parent, part in reversed(parents):
                if not parent[part]:
                    parent.pop(part)
                else:
                    break
        else:
            node[key] = field.value
    relative = items[0].relative_path.as_posix()
    resources = [
        resource for resource in expected.values() if resource.parts[0].path == relative
    ]
    if relative == "skills.toml":
        document["skills"] = sorted(
            resource.name
            for resource in resources
            if resource.kind == "skill-selection"
        )
    elif relative.startswith("projects/"):
        if not any(resource.kind == "project" for resource in resources):
            return None
        kinds = {item.kind for item in items}
        if "project-skill" in kinds:
            document["skills"] = sorted(
                resource.name.partition("/")[2]
                for resource in resources
                if resource.kind == "project-skill"
            )
        if "project-path" in kinds:
            document.pop("path", None)
            document.pop("paths", None)
            paths = sorted(
                resource.name.partition("/")[2]
                for resource in resources
                if resource.kind == "project-path"
            )
            if paths:
                document["paths"] = paths
    comments = "".join(
        line + "\n" for line in text.splitlines() if line.lstrip().startswith("#")
    )
    return comments + "".join(
        f"{format_toml_key(key)} = {_toml_value(value)}\n"
        for key, value in document.items()
    )


def _list_field(root: Path, relative: Path, key: str) -> list[str]:
    if not (root / relative).exists():
        return []
    with (root / relative).open("rb") as stream:
        value = tomllib.load(stream).get(key, [])
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
        raise ValueError(f"Invalid {key} in {root / relative}")
    return value


def _adopt_field(text: str, sections: list[str], key: str, value: object) -> str:
    """Set one TOML field, keeping other fields, comments, and formatting."""
    header = f"[{'.'.join(sections)}]" if sections else ""
    rendered = f"{format_toml_key(key)} = {_toml_value(value)}"
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


def _toml_value(value: object) -> str:
    if isinstance(value, (date, time)):
        return value.isoformat()
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        return (
            "{ "
            + ", ".join(
                f"{format_toml_key(key)} = {_toml_value(item)}"
                for key, item in value.items()
            )
            + " }"
        )
    return format_toml_value(value)


def _new_file_text(document: dict, items: list[ResourceWrite], original: str) -> str:
    """Project a new shared file when some source fields were skipped."""
    kinds = {item.kind for item in items}
    if "config" in kinds:
        selected = {item.name for item in items}

        def project(value: dict, prefix: str = "") -> dict:
            result = {}
            for key, child in value.items():
                name = f"{prefix}.{key}" if prefix else key
                if isinstance(child, dict):
                    nested = project(child, name)
                    if nested:
                        result[key] = nested
                elif name in selected:
                    result[key] = child
            return result

        filtered = project(document)
        if filtered == document:
            return original
    elif "project" in kinds:
        selected = {
            item.name.partition("/")[2]
            for item in items
            if item.kind == "project-field"
        }
        fields = {key for key in document if key not in {"path", "paths", "skills"}}
        paths = {
            item.name.partition("/")[2] for item in items if item.kind == "project-path"
        }
        raw_paths = document.get("paths", [])
        source_paths = set(
            raw_paths.values() if isinstance(raw_paths, dict) else raw_paths
        )
        if document.get("path"):
            source_paths.add(document["path"])
        if fields <= selected and source_paths <= paths:
            return original
        filtered = {key: value for key, value in document.items() if key in selected}
        if paths:
            filtered["paths"] = sorted(paths)
    else:
        return original
    return "".join(
        f"{format_toml_key(key)} = {_toml_value(value)}\n"
        for key, value in filtered.items()
    )


def render_merged_files(
    source: Path, target: Path, changes: tuple[ResourceWrite, ...]
) -> dict[Path, str]:
    """Render only selected logical changes, preserving existing target fields."""
    groups: dict[Path, list[ResourceWrite]] = {}
    for item in changes:
        groups.setdefault(item.relative_path, []).append(item)
    result = {}
    for relative, items in groups.items():
        existing = (target / relative).exists()
        text = ((target if existing else source) / relative).read_text(encoding="utf-8")
        with (source / relative).open("rb") as stream:
            source_document = tomllib.load(stream)
        if not existing:
            text = _new_file_text(source_document, items, text)
        kinds = {item.kind for item in items}
        for item in items:
            if not existing:
                # New files already carry their source scalar fields verbatim.
                continue
            name = item.name
            if item.kind == "config":
                *sections, key = name.split(".")
                value = _nested_value(source_document, name)
                text = _adopt_field(text, sections, key, value)
            elif item.kind == "project-field":
                key = name.partition("/")[2]
                text = _adopt_field(text, [], key, source_document[key])
        if kinds & {"skill-selection", "project-skill"} or (
            not existing and "project" in kinds and "skills" in source_document
        ):
            values = sorted(
                {
                    item.name
                    if item.kind == "skill-selection"
                    else item.name.partition("/")[2]
                    for item in items
                    if item.kind in ("skill-selection", "project-skill")
                }
                | set(_list_field(target, relative, "skills"))
            )
            text = update_skills_in_toml(text, values)
        if "project-path" in kinds:
            for value in sorted(
                item.name.partition("/")[2]
                for item in items
                if item.kind == "project-path"
            ):
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
