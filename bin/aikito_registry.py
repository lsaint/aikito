"""Schema comparison and additive migration for the Agent registry."""

import json
import re
import tomllib
from pathlib import Path
from typing import Any


def _leaf_fields(value: dict[str, Any], prefix: tuple[str, ...] = ()) -> dict:
    fields = {}
    for key, child in value.items():
        path = (*prefix, key)
        if isinstance(child, dict):
            fields.update(_leaf_fields(child, path))
        else:
            fields[path] = child
    return fields


def missing_agent_fields(
    registry_path: Path,
    template: str,
    include_agents: tuple[str, ...] = (),
) -> dict[str, dict[tuple[str, ...], Any]]:
    """Return bundled fields missing from agents already present in a registry."""
    with registry_path.open("rb") as registry_file:
        current = tomllib.load(registry_file).get("agents", {})
    bundled = tomllib.loads(template).get("agents", {})
    missing = {}
    agent_names = tuple(dict.fromkeys((*current, *include_agents)))
    for agent_name in agent_names:
        current_definition = current.get(agent_name, {})
        bundled_definition = bundled.get(agent_name)
        if not isinstance(current_definition, dict) or not isinstance(
            bundled_definition, dict
        ):
            continue
        current_fields = _leaf_fields(current_definition)
        absent = {
            path: value
            for path, value in _leaf_fields(bundled_definition).items()
            if path not in current_fields
        }
        if absent:
            missing[agent_name] = absent
    return missing


def _toml_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value(item) for item in value) + "]"
    return str(value)


def add_missing_agent_fields(
    registry_path: Path,
    template: str,
    include_agents: tuple[str, ...] = (),
) -> list[str]:
    """Add bundled defaults for missing fields without changing existing values."""
    missing = missing_agent_fields(registry_path, template, include_agents)
    if not missing:
        return []

    content = registry_path.read_text(encoding="utf-8")
    fixes = []
    grouped: dict[tuple[str, ...], list[tuple[str, Any]]] = {}
    for agent_name, fields in missing.items():
        for path, value in fields.items():
            table = ("agents", agent_name, *path[:-1])
            grouped.setdefault(table, []).append((path[-1], value))

    for table, fields in grouped.items():
        header = "[" + ".".join(table) + "]"
        additions = "".join(f"{key} = {_toml_value(value)}\n" for key, value in fields)
        match = re.search(rf"(?m)^{re.escape(header)}\s*$", content)
        if match:
            next_header = re.search(r"(?m)^\[", content[match.end() :])
            insertion = (
                match.end() + next_header.start() if next_header else len(content)
            )
            prefix = "" if content[:insertion].endswith("\n") else "\n"
            content = content[:insertion] + prefix + additions + content[insertion:]
        else:
            separator = "\n" if content.endswith("\n") else "\n\n"
            content += separator + header + "\n" + additions
        for key, _value in fields:
            fixes.append(f"Added {'.'.join((*table, key))} to agents.toml")

    registry_path.write_text(content, encoding="utf-8")
    return fixes
