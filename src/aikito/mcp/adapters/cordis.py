"""DSH Cordis (DeepSeek) YAML patch MCP config adapter."""

import json
import re
from typing import Any


def _split_cordis_patch_items(text: str) -> list[tuple[int, int, str]]:
    """Split YAML list into (start_idx, end_idx, item_text) tuples for top-level list items."""
    item_starts: list[int] = []
    for match in re.finditer(r"(?m)^-[ \t]+", text):
        item_starts.append(match.start())

    if not item_starts:
        stripped = text.lstrip()
        if stripped.startswith("-"):
            item_starts.append(text.find("-"))
        else:
            return []

    items: list[tuple[int, int, str]] = []
    for i, start in enumerate(item_starts):
        end = item_starts[i + 1] if i + 1 < len(item_starts) else len(text)
        items.append((start, end, text[start:end]))
    return items


def _parse_cordis_plugin_item(item_text: str) -> dict[str, Any]:
    """Parse key fields from a single Cordis plugin item in YAML."""
    result: dict[str, Any] = {}
    config: dict[str, Any] = {}
    current_section = None
    current_key = None

    lines = item_text.splitlines()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        norm_line = line
        if norm_line.lstrip().startswith("- "):
            dash_idx = norm_line.find("- ")
            norm_line = norm_line[:dash_idx] + "  " + norm_line[dash_idx + 2 :]

        indent = len(norm_line) - len(norm_line.lstrip())

        if indent <= 2:
            current_section = None
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k = k.strip().lstrip("- ").strip()
                v = v.strip()
                if k == "config":
                    current_section = "config"
                else:
                    result[k] = v.strip("'\"")
        elif current_section == "config" and indent == 4:
            if ":" in stripped:
                k, v = stripped.split(":", 1)
                k = k.strip()
                v = v.strip()
                if not v:
                    if k in ("args", "headers", "env"):
                        current_key = k
                        if k == "args":
                            config[k] = []
                        else:
                            config[k] = {}
                else:
                    current_key = None
                    if v.isdigit():
                        config[k] = int(v)
                    elif v in ("true", "false"):
                        config[k] = v == "true"
                    else:
                        if not v.startswith("!!js"):
                            v = v.strip("'\"")
                        config[k] = v
        elif current_section == "config" and indent >= 6 and current_key:
            if current_key == "args" and stripped.startswith("- "):
                val = stripped[2:].strip().strip("'\"")
                config["args"].append(val)
            elif current_key in ("headers", "env") and ":" in stripped:
                hk, hv = stripped.split(":", 1)
                hk = hk.strip()
                hv = hv.strip()
                if not hv.startswith("!!js"):
                    hv = hv.strip("'\"")
                config[current_key][hk] = hv

    if config:
        result["config"] = config
    return result


def _parse_dsh_cordis_entries(text: str) -> dict[str, dict[str, Any]]:
    """Extract all @deepseek-ai/dsh-mcp-client configs from cordis.patch.yml text."""
    if not text.strip():
        return {}

    entries: dict[str, dict[str, Any]] = {}
    items = _split_cordis_patch_items(text)
    for _, _, item_text in items:
        plugin = _parse_cordis_plugin_item(item_text)
        name = plugin.get("name", "")
        if isinstance(name, str):
            name = name.strip("'\"")
        if name == "@deepseek-ai/dsh-mcp-client":
            config = plugin.get("config", {})
            if isinstance(config, dict) and "serverName" in config:
                entries[config["serverName"]] = config
    return entries


def _format_dsh_cordis_entry(server_name: str, desired: dict[str, Any]) -> str:
    lines = [
        f"- id: aikito-mcp-{server_name}",
        "  name: '@deepseek-ai/dsh-mcp-client'",
        "  config:",
        f"    serverName: {server_name}",
    ]
    for key, val in desired.items():
        if key == "serverName":
            continue
        if key == "args" and isinstance(val, list):
            lines.append("    args:")
            for arg in val:
                lines.append(f"      - {json.dumps(str(arg), ensure_ascii=False)}")
        elif key in ("headers", "env") and isinstance(val, dict):
            lines.append(f"    {key}:")
            for k, v in sorted(val.items()):
                if isinstance(v, str) and (v.startswith("!!js") or v.startswith("`")):
                    lines.append(f"      {k}: {v}")
                else:
                    lines.append(f"      {k}: {json.dumps(str(v), ensure_ascii=False)}")
        elif isinstance(val, bool):
            lines.append(f"    {key}: {'true' if val else 'false'}")
        elif isinstance(val, (int, float)):
            lines.append(f"    {key}: {val}")
        else:
            lines.append(f"    {key}: {val}")
    return "\n".join(lines)


def get_dsh_cordis_server(text: str, server_name: str) -> dict[str, Any] | None:
    entries = _parse_dsh_cordis_entries(text)
    return entries.get(server_name)


def update_dsh_cordis_server(
    text: str, server_name: str, desired: dict[str, Any]
) -> str:
    formatted = _format_dsh_cordis_entry(server_name, desired)
    if not text.strip():
        return formatted + "\n"

    items = _split_cordis_patch_items(text)
    for start, end, item_text in items:
        plugin = _parse_cordis_plugin_item(item_text)
        name = plugin.get("name", "")
        if isinstance(name, str):
            name = name.strip("'\"")
        plugin_id = plugin.get("id", "")
        cfg = plugin.get("config", {})
        is_target = plugin_id == f"aikito-mcp-{server_name}" or (
            name == "@deepseek-ai/dsh-mcp-client"
            and cfg.get("serverName") == server_name
        )
        if is_target:
            trailing = "" if item_text.endswith("\n") else "\n"
            return text[:start] + formatted + trailing + text[end:]

    separator = (
        "\n" if text.endswith("\n\n") else ("\n\n" if text.endswith("\n") else "\n\n")
    )
    return text.rstrip() + separator + formatted + "\n"


def remove_dsh_cordis_server(text: str, server_name: str) -> str:
    if not text.strip():
        return ""

    items = _split_cordis_patch_items(text)
    for start, end, item_text in items:
        plugin = _parse_cordis_plugin_item(item_text)
        name = plugin.get("name", "")
        if isinstance(name, str):
            name = name.strip("'\"")
        plugin_id = plugin.get("id", "")
        cfg = plugin.get("config", {})
        is_target = plugin_id == f"aikito-mcp-{server_name}" or (
            name == "@deepseek-ai/dsh-mcp-client"
            and cfg.get("serverName") == server_name
        )
        if is_target:
            new_text = (text[:start] + text[end:]).strip()
            return (new_text + "\n") if new_text else ""
    return text
