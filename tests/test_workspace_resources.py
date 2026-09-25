"""Logical resource snapshots of one workspace."""

from __future__ import annotations

import io
import os
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from aikito.init import init_workspace
from aikito.workspace_resources import (
    WorkspaceResourceError,
    resource_id,
    scan_credentials,
    snapshot_workspace,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    root = tmp_path / "ws"
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, tmp_path / "home")
    return root


def _write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _codes(workspace: Path) -> list[tuple[str, str]]:
    return [(f.code, f.resource) for f in snapshot_workspace(workspace).findings]


def test_fresh_workspace_has_no_findings(workspace: Path) -> None:
    snapshot = snapshot_workspace(workspace)
    assert snapshot.findings == ()
    assert "skills/aikito" in snapshot.skipped
    assert resource_id("global-instructions", "AGENTS.md") in snapshot.resources
    assert resource_id("skill-selection", "aikito") in snapshot.resources


def test_rejects_non_workspace(tmp_path: Path) -> None:
    with pytest.raises(WorkspaceResourceError):
        snapshot_workspace(tmp_path)


def test_logical_resources_span_files_tables_and_sets(workspace: Path) -> None:
    _write(workspace / "memory/notes/a-note.md", "# A\n")
    _write(workspace / "skills/demo/SKILL.md", "---\nname: demo\n---\n")
    _write(
        workspace / "subagents/checker.md",
        '---\ndescription: "d"\nagents: ["codex"]\n---\nCheck things.\n',
    )
    _write(workspace / "mcps/docs.toml", 'transport = "remote"\nagents = ["codex"]\n')
    _write(
        workspace / "projects/app/agent.toml",
        'name = "app"\npaths = ["~/a", "/b"]\nskills = ["demo"]\n',
    )
    _write(workspace / "projects/app/memory/notes/p-note.md", "# P\n")

    resources = snapshot_workspace(workspace).resources
    subagent = resources["subagent:checker"]
    assert [part.path for part in subagent.parts] == ["subagents/checker.md"]
    assert subagent.references == ("agent:codex",)
    assert resources["mcp:docs"].references == ("agent:codex",)
    assert {"project-path:app/~/a", "project-path:app//b"} <= resources.keys()
    assert resources["project-skill:app/demo"].references == (
        "project:app",
        "skill:demo",
    )
    assert "memory:notes/a-note.md" in resources
    assert "project-memory:app/notes/p-note.md" in resources
    assert "skill:demo" in resources


def test_toml_fingerprints_ignore_comments_and_formatting(workspace: Path) -> None:
    before = snapshot_workspace(workspace).resources
    agents = workspace / "agents" / "codex.toml"
    if not agents.exists():
        _write(agents, '[agents.codex]\ndisplay_name = "Codex"\n')
        before = snapshot_workspace(workspace).resources
    agents.write_text(
        "# extra comment\n" + agents.read_text(encoding="utf-8") + "\n\n",
        encoding="utf-8",
    )
    assert snapshot_workspace(workspace).resources == before


def test_agent_tables_are_independent_resources(workspace: Path) -> None:
    _write(
        workspace / "agents/one.toml",
        '[agents.one]\ndisplay_name = "One"\n',
    )
    _write(workspace / "agents/two.toml", '[agents.two]\ndisplay_name = "Two"\n')
    before = snapshot_workspace(workspace).resources
    _write(
        workspace / "agents/two.toml",
        '[agents.two]\ndisplay_name = "2"\n',
    )
    after = snapshot_workspace(workspace).resources
    assert after["agent:one"] == before["agent:one"]
    assert after["agent:two"] != before["agent:two"]


def test_system_artifacts_are_ignored(workspace: Path) -> None:
    _write(workspace / "skills/demo/SKILL.md", "demo\n")
    before = snapshot_workspace(workspace).resources["skill:demo"]
    for path in (
        "skills/.DS_Store",
        "skills/demo/.DS_Store",
        "skills/demo/__pycache__/x.pyc",
        "memory/notes/.DS_Store",
        "memory/notes/._a-note.md",
    ):
        _write(workspace / path, "junk")
    snapshot = snapshot_workspace(workspace)
    assert snapshot.findings == ()
    assert snapshot.resources["skill:demo"] == before


def test_unmanaged_areas_are_skipped(workspace: Path) -> None:
    _write(workspace / "shell/tool.sh", "echo\n")
    _write(workspace / "AGENTS.md", "local\n")
    snapshot = snapshot_workspace(workspace)
    assert {"shell", "AGENTS.md"} <= set(snapshot.skipped)
    assert ".git" not in snapshot.skipped
    assert snapshot.findings == ()


def test_collects_all_findings_instead_of_stopping(workspace: Path) -> None:
    _write(workspace / "memory/notes/Bad Name.md", "x")
    _write(workspace / "memory/notes/other.txt", "x")
    _write(workspace / "skills/no-skill-md/readme.txt", "x")
    _write(workspace / "mcps/broken.toml", "not = [toml")
    _write(workspace / "subagents/orphan.md", "x")
    codes = _codes(workspace)
    assert ("unsupported-entry", "memory/notes/Bad Name.md") in codes
    assert ("unsupported-entry", "memory/notes/other.txt") in codes
    assert ("invalid-skill", "skills/no-skill-md") in codes
    assert ("invalid-toml", "mcps/broken.toml") in codes
    assert ("invalid-subagent", "subagents/orphan.md") in codes


def test_credentials_are_warnings_reported_by_path_only(workspace: Path) -> None:
    secret = "api_key = abcdefghijklmnopqrstuvwxyz"
    _write(workspace / "memory/notes/leak.md", secret)
    _write(workspace / "skills/demo/SKILL.md", "demo\n")
    _write(workspace / "skills/demo/ref/config.md", secret)
    _write(workspace / "shell/unmanaged.sh", secret)
    snapshot = snapshot_workspace(workspace)
    assert snapshot.findings == ()
    findings = scan_credentials(snapshot)
    assert [(f.status, f.code, f.resource) for f in findings] == [
        ("warning", "possible-credential", "memory/notes/leak.md"),
        ("warning", "possible-credential", "skills/demo/ref/config.md"),
    ]
    assert all(secret not in f.message for f in findings)


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_symlinks_are_findings_not_followed(workspace: Path, tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("secret", encoding="utf-8")
    (workspace / "memory/notes/link.md").symlink_to(outside)
    _write(workspace / "skills/demo/SKILL.md", "demo\n")
    (workspace / "skills/demo/escape").symlink_to(tmp_path)
    snapshot = snapshot_workspace(workspace)
    codes = [(f.code, f.resource) for f in snapshot.findings]
    assert ("unsafe-entry", "memory/notes/link.md") in codes
    assert ("unsafe-entry", "skills/demo/escape") in codes
    assert "skill:demo" not in snapshot.resources


@pytest.mark.skipif(os.name == "nt", reason="POSIX executable bit")
def test_skill_fingerprint_tracks_executable_bit(workspace: Path) -> None:
    script = workspace / "skills/demo/run.sh"
    _write(workspace / "skills/demo/SKILL.md", "demo\n")
    _write(script, "echo\n")
    before = snapshot_workspace(workspace).resources["skill:demo"]
    script.chmod(0o755)
    assert snapshot_workspace(workspace).resources["skill:demo"] != before


def test_inbox_follows_configured_path(workspace: Path) -> None:
    _write(workspace / "inbox/idea.md", "# Idea\n")
    _write(workspace / "inbox/sub/deep.md", "# Deep\n")
    resources = snapshot_workspace(workspace).resources
    assert {"inbox:idea.md", "inbox:sub/deep.md"} <= resources.keys()
