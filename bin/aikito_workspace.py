"""Resolve and persist the active Aikito workspace."""

import os
from pathlib import Path


def get_workspace_pointer_path(home: Path) -> Path:
    """Return the user-level file that stores the default workspace path."""
    config_home = os.environ.get("XDG_CONFIG_HOME")
    base_dir = Path(config_home).expanduser() if config_home else home / ".config"
    return base_dir / "aikito" / "workspace"


def resolve_workspace(home: Path) -> Path:
    """Resolve the workspace using environment, persisted choice, then default."""
    env_dir = os.environ.get("AIKITO_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve()

    pointer_path = get_workspace_pointer_path(home)
    try:
        configured_dir = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        configured_dir = ""
    if configured_dir:
        return Path(configured_dir).expanduser().resolve()

    return (home / "aikito").resolve()


def persist_workspace(workspace: Path, home: Path) -> Path:
    """Persist the workspace selected by a successful explicit initialization."""
    pointer_path = get_workspace_pointer_path(home)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(f"{workspace.expanduser().resolve()}\n", encoding="utf-8")
    return pointer_path
