"""TOML MCP config adapter (Codex, Zed)."""

import json
import re
import tomllib
from typing import Any

from ..model import MCPConfigError


def get_toml_server(text: str, server_name: str) -> dict[str, Any] | None:
    from .jsonc import _load_document

    document = _load_document("toml", text)
    server = document.get("mcp_servers", {}).get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(f"Codex MCP server '{server_name}' must be a table")
    return server


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        entries = ", ".join(
            f"{json.dumps(str(key))} = {_toml_value(item)}"
            for key, item in value.items()
        )
        return f"{{ {entries} }}"
    raise MCPConfigError(f"Unsupported managed TOML value: {value!r}")


def update_toml_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    header = f"[mcp_servers.{server_name}]"
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in desired.items())
    section = f"{header}\n{body}\n"
    header_pattern = re.compile(rf"(?m)^[ \t]*{re.escape(header)}[ \t]*(?:#.*)?$")
    match = header_pattern.search(text)
    if match is None:
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        if text and not text.endswith("\n\n"):
            separator += "\n"
        return text + separator + section

    next_header = re.search(
        r"(?m)^[ \t]*\[[^\]]+\][ \t]*(?:#.*)?$", text[match.end() :]
    )
    end = len(text) if next_header is None else match.end() + next_header.start()
    return text[: match.start()] + section + text[end:]


def remove_toml_server(text: str, server_name: str) -> str:
    header = f"[mcp_servers.{server_name}]"
    header_pattern = re.compile(rf"(?m)^[ \t]*{re.escape(header)}[ \t]*(?:#.*)?$")
    match = header_pattern.search(text)
    if match is None:
        return text

    next_header = re.search(
        r"(?m)^[ \t]*\[[^\]]+\][ \t]*(?:#.*)?$", text[match.end() :]
    )
    end = len(text) if next_header is None else match.end() + next_header.start()
    new_text = text[: match.start()] + text[end:]
    cleaned = re.sub(r"\n{3,}", "\n\n", new_text)
    try:
        tomllib.loads(cleaned)
    except Exception as exc:
        raise MCPConfigError(
            f"Failed to verify Codex TOML after removing server '{server_name}': {exc}"
        ) from exc
    return cleaned
