"""Exercise definition-driven detection and native adapters through the real CLI."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import tomllib
from pathlib import Path

from aikito.templating import load_template


def exercise(root: Path) -> None:
    home, workspace, binaries = (root / name for name in ("home", "workspace", "bin"))
    home.mkdir(parents=True)
    binaries.mkdir()
    environment = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local/state"),
        APPDATA=str(home / "AppData/Roaming"),
        LOCALAPPDATA=str(home / "AppData/Local"),
        AIKITO_DIR=str(workspace),
        AIKITO_NO_UPDATE_NOTIFIER="1",
        XDG_CACHE_HOME=str(home / ".cache"),
        PATH=str(binaries) + os.pathsep + os.environ.get("PATH", ""),
    )

    def cli(*arguments: str, allowed=(0,)) -> str:
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *arguments],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode in allowed, (arguments, result.stdout, result.stderr)
        return result.stdout + result.stderr

    cli("init", "workspace", str(workspace))
    for path in (workspace / "agents").glob("*.toml"):
        path.unlink()
    (workspace / "agents/example.toml").write_text(
        """[agents.example]
display_name = "Example"
instruction_path = ".example/AGENTS.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"
[agents.example.detect]
commands = ["aikito-example-smoke"]
paths = [".example"]
[agents.example.mcp]
config_path = ".example/mcp.jsonc"
config_format = "jsonc"
name_style = "verbatim"
[agents.example.subagents]
config_path = ".example/agents"
config_format = "claude_markdown"
""",
        encoding="utf-8",
    )
    executable = binaries / (
        "aikito-example-smoke.cmd" if os.name == "nt" else "aikito-example-smoke"
    )
    executable.write_text(
        "@echo off\nexit /b 0\n" if os.name == "nt" else "#!/bin/sh\nexit 0\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    assert not (home / ".example").exists()
    assert "aikito-example-smoke" in cli(
        "status"
    )  # Binary-only detection (including Windows PATHEXT).
    assert "aikito-example-smoke CLI found" in cli("doctor", allowed=(0, 1))
    executable.unlink()
    assert "aikito-example-smoke" not in cli("status")  # Neither binary nor marker.
    (home / ".example").mkdir()
    status = cli("status")
    assert "aikito-example-smoke" in status  # Marker-only detection.

    # A pre-adapter Grok definition must keep Grok header semantics.
    legacy_grok = "\n".join(
        line
        for line in load_template("agents/grok.toml").splitlines()
        if not line.startswith("adapter")
    )
    (workspace / "agents/grok.toml").write_text(legacy_grok + "\n", encoding="utf-8")
    legacy_mcp = tomllib.loads(legacy_grok)["agents"]["grok"]["mcp"]
    assert legacy_mcp["config_format"] == "toml" and "adapter" not in legacy_mcp
    (home / ".grok").mkdir()
    (workspace / "mcps/docs.toml").write_text(
        """transport = "remote"
url = "https://example.com/mcp"
agents = ["example", "grok"]
[authentication]
method = "basic_api_token"
account_email = "smoke@example.com"
token_env = "AIKITO_SMOKE_TOKEN"
authorization_env = "AIKITO_SMOKE_AUTH"
""",
        encoding="utf-8",
    )
    cli("sync", "mcp")
    json_path, grok_path = home / ".example/mcp.jsonc", home / ".grok/config.toml"
    assert json_path.is_file() and grok_path.is_file()
    assert (
        json.loads(json_path.read_text())["mcp"]["docs"]["url"]
        == "https://example.com/mcp"
    )
    grok_entry = tomllib.loads(grok_path.read_text())["mcp_servers"]["docs"]
    assert grok_entry["headers"]["Authorization"] == "${AIKITO_SMOKE_AUTH}"
    assert "env_http_headers" not in grok_entry
    (workspace / "subagents/review.md").write_text(
        """---
description: "Review changes"
agents: ["example"]
example: {"model":"example", "effort":"high"}
---
Review changes carefully.
""",
        encoding="utf-8",
    )
    cli("sync", "subagents")
    assert (home / ".example/agents/review.md").is_file()
    assert 'model: "example"' in (home / ".example/agents/review.md").read_text()
    assert "aikito-example-smoke" in cli("status")
    doctor = cli("doctor", allowed=(0, 1))
    assert "Example config: valid JSONC" in doctor
    assert "Grok Build config: valid TOML" in doctor
    assert "Plan is stale" not in doctor
    imported = root / "imported.md"
    imported.write_text(
        "---\ndescription:\n  Use when: reviewing code\nmetadata:\n  author: me\nexample:\n  model: example\n---\nReview imported code.\n",
        encoding="utf-8",
    )
    cli("add", "subagent", "imported", "--from", str(imported), "--agents", "example")
    assert (workspace / "subagents/imported.md").is_file()
    assert (
        'description: "Use when: reviewing code"'
        in (workspace / "subagents/imported.md").read_text()
    )
    review = workspace / "subagents/review.md"
    review.write_text(
        review.read_text().replace(
            'example: {"model":"example", "effort":"high"}',
            'example: {"model":"example", "effort":"high"}\ncodex: {"model":"foreign"}',
        )
    )
    cli("sync", "subagents")
    assert "platform 'codex'" in cli("doctor", allowed=(0, 1))
    assert (home / ".example/agents/review.md").is_file()
    print("Agent detection and adapter CLI smoke passed")


if __name__ == "__main__":
    with tempfile.TemporaryDirectory(prefix="aikito-adapter-smoke-") as directory:
        exercise(Path(directory))
