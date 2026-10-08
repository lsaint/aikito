"""Exercise definition-driven adoption and Agent registration with the real CLI."""

from __future__ import annotations

import os
import json
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from aikito.templating import load_template


def exercise(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    home, workspace = root / "home", root / "workspace"
    home.mkdir()
    environment = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        AIKITO_DIR=str(workspace),
        AIKITO_NO_UPDATE_NOTIFIER="1",
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local/state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
    )

    def cli(*arguments: str, expected=0) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *arguments],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == expected, (arguments, result.stdout, result.stderr)
        return result.stdout + result.stderr

    cli("init", "workspace", str(workspace))
    for path in (workspace / "agents").glob("*.toml"):
        path.unlink()
    (workspace / "agents/example.toml").write_text(
        '[agents.example]\ndisplay_name = "Example"\n'
        '[agents.example.mcp]\nconfig_path = ".example/config.toml"\n'
        'config_format = "toml"\n',
        encoding="utf-8",
    )
    for name in ("codex", "example"):
        directory = home / f".{name}"
        directory.mkdir()
        (directory / "config.toml").write_text(
            f'[mcp_servers.{name}-docs]\nurl = "https://{name}.example.com/mcp"\n',
            encoding="utf-8",
        )
    copilot = home / ".copilot/agents"
    copilot.mkdir(parents=True)
    (copilot / "review.agent.md").write_text(
        '---\ndescription: Review changes\ntools: ["read"]\n---\nReview code.\n',
        encoding="utf-8",
    )

    claude_source = home / ".claude.json"
    claude_content = json.dumps(
        {
            "mcpServers": {
                "private-api": {
                    "url": "https://private.example.com/mcp",
                    "headers": {
                        "Authorization": "Bearer dummy-test-secret",
                        "Accept": "application/json",
                    },
                }
            }
        }
    )
    claude_source.write_text(claude_content, encoding="utf-8")

    preview = cli("adopt", "--dry-run", "--verbose")
    assert "[DRY-RUN AGENT] Would register Codex" in preview
    assert "dummy-test-secret" not in preview
    assert not (workspace / "agents/codex.toml").exists()
    assert not (workspace / "mcps/codex-docs.toml").exists()
    assert not (workspace / "mcps/private-api.toml").exists()
    assert not (workspace / "subagents/review.md").exists()

    cli("adopt", "--skip", "agent/codex", expected=1)
    assert not (workspace / "agents/github-copilot.toml").exists()
    assert not (workspace / "mcps/example-docs.toml").exists()
    cli("adopt", "--dry-run", "--skip", "agent/codex", "--skip", "mcp/codex-docs")
    assert "dummy-test-secret" not in cli("adopt")
    for name in ("claude-code", "codex", "github-copilot"):
        path = workspace / f"agents/{name}.toml"
        assert path.is_file()
        assert path.read_text(encoding="utf-8") == load_template(f"agents/{name}.toml")
    for name in ("codex", "example"):
        path = workspace / f"mcps/{name}-docs.toml"
        assert path.is_file()
        assert tomllib.loads(path.read_text(encoding="utf-8"))["agents"] == [name]
    private_path = workspace / "mcps/private-api.toml"
    assert private_path.is_file()
    private_content = private_path.read_text(encoding="utf-8")
    assert "dummy-test-secret" not in private_content
    assert tomllib.loads(private_content)["headers"] == {
        "Authorization": "${AIKITO_PRIVATE_API_AUTHORIZATION}",
        "Accept": "application/json",
    }
    assert claude_source.read_text(encoding="utf-8") == claude_content
    assert (workspace / "subagents/review.md").is_file()
    assert "github-copilot" in (workspace / "subagents/review.md").read_text(
        encoding="utf-8"
    )
    assert "No adoptable Agent configuration found" in cli("adopt", "--dry-run")
    assert (
        (home / ".codex/config.toml")
        .read_text(encoding="utf-8")
        .startswith("[mcp_servers.codex-docs]")
    )
    print("Definition-driven adoption CLI smoke passed")


if __name__ == "__main__":
    configured_root = os.environ.get("AIKITO_ADOPT_SMOKE_ROOT")
    if configured_root:
        exercise(Path(configured_root))
    else:
        with tempfile.TemporaryDirectory(prefix="aikito-adopt-smoke-") as directory:
            exercise(Path(directory))
