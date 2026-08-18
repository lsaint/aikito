"""
Aikito Configuration Management

Handles reading workspace configuration (config.toml)
and merging project-level configuration (projects/<name>/agent.toml).
"""

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

DEFAULT_STALE_MEMORY_DAYS = 30
DEFAULT_INBOX_PATH = "~/aikito/inbox"


@dataclass
class MemoryConfig:
    stale_days: int = DEFAULT_STALE_MEMORY_DAYS


@dataclass
class InboxConfig:
    path: str = DEFAULT_INBOX_PATH


@dataclass
class AikitoConfig:
    memory: MemoryConfig = field(default_factory=MemoryConfig)
    inbox: InboxConfig = field(default_factory=InboxConfig)


def get_workspace_config_path(aikito_dir: Path) -> Optional[Path]:
    """
    Return path to workspace configuration file (config.toml),
    or None if it does not exist.
    """
    config_file = aikito_dir / "config.toml"
    if config_file.is_file():
        return config_file
    return None


def load_workspace_config(aikito_dir: Path) -> AikitoConfig:
    """
    Load global configuration from config.toml in the Aikito workspace directory.
    """
    config_file = get_workspace_config_path(aikito_dir)
    if not config_file:
        return AikitoConfig()

    try:
        with open(config_file, "rb") as f:
            data = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError):
        return AikitoConfig()

    memory_config = MemoryConfig()
    memory_data = data.get("memory")
    if isinstance(memory_data, dict):
        val = memory_data.get("stale_days")
        if isinstance(val, int) and val > 0:
            memory_config.stale_days = val

    inbox_config = InboxConfig()
    inbox_data = data.get("inbox")
    if isinstance(inbox_data, dict):
        path_val = inbox_data.get("path")
        if isinstance(path_val, str):
            inbox_config.path = path_val

    return AikitoConfig(memory=memory_config, inbox=inbox_config)


def get_inbox_path(aikito_dir: Path) -> Path:
    """
    Resolve configured inbox path. Defaults to aikito_dir / "inbox"
    (or ~/aikito/inbox).
    """
    config = load_workspace_config(aikito_dir)
    raw_path = config.inbox.path.strip() if config.inbox.path else ""
    if not raw_path:
        return (aikito_dir / "inbox").resolve()

    expanded = Path(raw_path).expanduser()
    if expanded == Path(DEFAULT_INBOX_PATH).expanduser():
        return (aikito_dir / "inbox").resolve()
    if not expanded.is_absolute():
        return (aikito_dir / expanded).resolve()
    return expanded.resolve()


def get_project_memory_stale_days(
    proj_folder: Path, default_stale_days: int = DEFAULT_STALE_MEMORY_DAYS
) -> int:
    """
    Read memory.stale_days override from a project's agent.toml.
    Returns default_stale_days if not specified or invalid.
    """
    agent_toml = proj_folder / "agent.toml"
    if not agent_toml.is_file():
        return default_stale_days

    try:
        with open(agent_toml, "rb") as f:
            data = tomllib.load(f)
        memory_data = data.get("memory")
        if isinstance(memory_data, dict):
            stale_days = memory_data.get("stale_days")
            if isinstance(stale_days, int) and stale_days > 0:
                return stale_days
    except (tomllib.TOMLDecodeError, OSError):
        pass

    return default_stale_days
