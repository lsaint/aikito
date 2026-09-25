"""Build v2 workspace fixtures from legacy Agent table text in tests."""

import tomllib
import re
from pathlib import Path

from aikito.workspace_layout import _split_agent_text
from aikito.workspace_layout import (
    WorkspaceLayoutError,
    parse_subagent_file,
    render_subagent_text,
)
from aikito.add import _render_subagent_block


def write_agents(root: Path, content: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    directory = root / "agents"
    directory.mkdir(exist_ok=True)
    (root / "subagents").mkdir(exist_ok=True)
    for path in directory.glob("*.toml"):
        path.unlink()
    try:
        document = tomllib.loads(content)
        agents = document.get("agents", {})
        if not isinstance(agents, dict):
            raise ValueError("Invalid Agent table")
        normalized = content
        for name in agents:
            if re.search(rf"(?m)^\[agents\.{re.escape(name)}\]\s*$", normalized):
                continue
            nested = re.search(
                rf"(?m)^\[agents\.{re.escape(name)}\.[^\]]+\]", normalized
            )
            if nested:
                normalized = (
                    normalized[: nested.start()]
                    + f"[agents.{name}]\n"
                    + normalized[nested.start() :]
                )
        fragments = _split_agent_text(normalized, agents)
    except (tomllib.TOMLDecodeError, ValueError):
        (directory / "invalid.toml").write_text(content, encoding="utf-8")
        return
    for name, fragment in fragments.items():
        (directory / f"{name}.toml").write_text(fragment, encoding="utf-8")


def write_subagents(root: Path, content: str) -> None:
    """Move fixture metadata into each existing prompt without changing its body."""
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    directory = root / "subagents"
    directory.mkdir(exist_ok=True)
    (root / "agents").mkdir(exist_ok=True)
    document = tomllib.loads(content)
    table = document.get("subagents", {})
    if not isinstance(table, dict):
        raise ValueError("Invalid subagent table")
    for name, metadata in table.items():
        path = directory / f"{name}.md"
        if path.is_file():
            try:
                _, body = parse_subagent_file(path)
            except WorkspaceLayoutError:
                body = path.read_text(encoding="utf-8")
        else:
            body = f"# {name}\n"
        path.write_text(render_subagent_text(metadata, body), encoding="utf-8")


def read_agents(root: Path) -> str:
    return "[agents]\n\n" + "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((root / "agents").glob("*.toml"))
    )


def read_subagents(root: Path) -> str:
    content = "[subagents]\n"
    for path in sorted((root / "subagents").glob("*.md")):
        metadata, _ = parse_subagent_file(path)
        content += _render_subagent_block(
            path.stem,
            metadata["description"],
            metadata["agents"],
            {
                key: value
                for key, value in metadata.items()
                if key not in ("description", "agents")
            },
        )
    return content


def replace_subagent_body(root: Path, name: str, body: str) -> None:
    path = root / "subagents" / f"{name}.md"
    metadata, _ = parse_subagent_file(path)
    path.write_text(render_subagent_text(metadata, body), encoding="utf-8")
