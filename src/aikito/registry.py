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
    current = {}
    for path in sorted(registry_path.glob("*.toml")):
        document = tomllib.loads(path.read_text(encoding="utf-8"))
        table = document.get("agents", {})
        if set(document) != {"agents"} or set(table) != {path.stem}:
            raise ValueError(f"Invalid Agent definition: {path}")
        current[path.stem] = table[path.stem]
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

    fixes = []
    bundled = tomllib.loads(template).get("agents", {})
    for agent_name, fields in missing.items():
        path = registry_path / f"{agent_name}.toml"
        if not path.is_file():
            if agent_name not in bundled:
                continue
            content = _agent_template_fragment(template, agent_name)
            path.write_text(content, encoding="utf-8")
            fixes.append(f"Added agents/{agent_name}.toml")
            continue
        content = path.read_text(encoding="utf-8")
        grouped: dict[tuple[str, ...], list[tuple[str, Any]]] = {}
        for field, value in fields.items():
            table = ("agents", agent_name, *field[:-1])
            grouped.setdefault(table, []).append((field[-1], value))
        for table, values in grouped.items():
            header = "[" + ".".join(table) + "]"
            additions = "".join(
                f"{key} = {_toml_value(value)}\n" for key, value in values
            )
            match = re.search(rf"(?m)^{re.escape(header)}\s*$", content)
            if match:
                next_header = re.search(r"(?m)^\[", content[match.end() :])
                insertion = (
                    match.end() + next_header.start() if next_header else len(content)
                )
                prefix = "" if content[:insertion].endswith("\n") else "\n"
                content = content[:insertion] + prefix + additions + content[insertion:]
            else:
                content += "\n" + header + "\n" + additions
            for key, _value in values:
                fixes.append(
                    f"Added {'.'.join((*table, key))} to agents/{agent_name}.toml"
                )
        tomllib.loads(content)
        path.write_text(content, encoding="utf-8")
    return fixes


def _agent_template_fragment(template: str, agent_name: str) -> str:
    header = f"[agents.{agent_name}]"
    start = re.search(rf"(?m)^{re.escape(header)}\s*$", template)
    if start is None:
        raise ValueError(f"Missing bundled Agent: {agent_name}")
    next_agent = re.search(r"(?m)^\[agents\.[^.\]]+\]\s*$", template[start.end() :])
    end = start.end() + next_agent.start() if next_agent else len(template)
    return template[start.start() : end].strip() + "\n"


def remove_agent_section(registry_path: Path, agent_name: str) -> None:
    """Remove one Agent file from the canonical registry directory."""
    (registry_path / f"{agent_name}.toml").unlink(missing_ok=True)
