"""Execution and state management for MCP synchronization."""

import json
from pathlib import Path
from typing import Any

from .model import STATE_FILE, STATE_VERSION, MCPConfigError


def _load_state(home: Path) -> dict[str, Any]:
    path = home / STATE_FILE
    if not path.exists():
        return {"version": STATE_VERSION, "entries": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MCPConfigError(f"Cannot read MCP state file {path}: {exc}") from exc
    if state.get("version") != STATE_VERSION or not isinstance(
        state.get("entries"), dict
    ):
        raise MCPConfigError(f"Unsupported MCP state file: {path}")
    return state
