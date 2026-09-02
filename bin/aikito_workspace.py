"""Resolve and persist the active Aikito workspace."""

import os
from pathlib import Path

from aikito_platform import get_workspace_config_dir


def get_workspace_pointer_path(home: Path) -> Path:
    """Return the user-level file that stores the default workspace path."""
    return get_workspace_config_dir(home) / "workspace"


def resolve_workspace_with_source(home: Path) -> tuple[Path, str]:
    """Resolve the workspace and identify whether it came from env, config, or default."""
    env_dir = os.environ.get("AIKITO_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve(), "AIKITO_DIR"

    pointer_path = get_workspace_pointer_path(home)
    try:
        configured_dir = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        configured_dir = ""
    if configured_dir:
        return Path(configured_dir).expanduser().resolve(), "configured"

    return (home / "aikito").resolve(), "default"


def resolve_workspace(home: Path) -> Path:
    """Resolve the workspace using environment, persisted choice, then default."""
    return resolve_workspace_with_source(home)[0]


def persist_workspace(workspace: Path, home: Path) -> Path:
    """Persist the workspace selected by a successful explicit initialization."""
    pointer_path = get_workspace_pointer_path(home)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(f"{workspace.expanduser().resolve()}\n", encoding="utf-8")
    return pointer_path
