"""Required workspace resource migration and layout gate."""

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.workspace_layout import (
    WorkspaceLayoutError,
    apply_migration,
    build_migration_plan,
    load_agent_document,
    parse_subagent_file,
    require_current_layout,
    migration_path_policy,
)
from aikito.workspace_core import WorkspaceCoreError, validate_resource_path


def legacy_workspace(root: Path) -> None:
    (root / "subagents").mkdir(parents=True)
    (root / "agents.toml").write_text(
        "# Registry header\n[agents]\n\n# Codex description\n[agents.codex]\n"
        'display_name = "Codex"\n\n[agents.codex.subagents]\n'
        'config_path = ".codex/config.toml"\n',
        encoding="utf-8",
    )
    (root / "subagents.toml").write_text(
        '[subagents.review]\ndescription = "Review changes"\n'
        'agents = ["codex"]\n\n[subagents.review.codex]\n'
        'model = "o3"\n',
        encoding="utf-8",
    )
    (root / "subagents" / "review.md").write_text(
        "# Review\n\nKeep this body exactly.\n", encoding="utf-8"
    )


def test_migration_paths_are_registered_by_the_caller() -> None:
    with pytest.raises(WorkspaceCoreError, match="Unsupported resource path"):
        validate_resource_path("layout.toml", "layout")
    assert validate_resource_path(
        "layout.toml", "layout", migration_path_policy()
    ) == Path("layout.toml")


def test_migration_preview_apply_and_idempotency(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    original_body = (root / "subagents" / "review.md").read_bytes().decode("utf-8")
    before = {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    with pytest.raises(WorkspaceLayoutError, match="migrate workspace-resources"):
        require_current_layout(root)

    plan = build_migration_plan(root)
    assert not plan.blocked
    assert [path for path, _ in plan.creates] == ["agents/codex.toml"]
    assert [path for path, _ in plan.updates] == ["subagents/review.md"]
    assert {p.name: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before

    apply_migration(plan, tmp_path)
    require_current_layout(root)
    assert load_agent_document(root)["agents"]["codex"]["display_name"] == "Codex"
    metadata, body = parse_subagent_file(root / "subagents" / "review.md")
    assert metadata == {
        "description": "Review changes",
        "agents": ["codex"],
        "codex": {"model": "o3"},
    }
    assert body == original_body
    assert (root / "layout.toml").read_text(encoding="utf-8") == "version = 2\n"
    assert not (root / "agents.toml").exists()
    assert not (root / "subagents.toml").exists()
    assert not build_migration_plan(root).blocked
    assert not build_migration_plan(root).creates


def test_migration_collision_does_not_write(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    (root / "agents").mkdir()
    (root / "agents" / "codex.toml").write_text("custom = true\n", encoding="utf-8")
    before = {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    }
    plan = build_migration_plan(root)
    assert plan.blocked
    assert any("agents/codex.toml" in finding for finding in plan.findings)
    with pytest.raises(WorkspaceLayoutError, match="blockers"):
        apply_migration(plan, tmp_path)
    assert {
        str(p.relative_to(root)): p.read_bytes() for p in root.rglob("*") if p.is_file()
    } == before


def test_migration_restores_legacy_files_after_interrupted_apply(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    old_agents = (root / "agents.toml").read_bytes()
    old_subagents = (root / "subagents.toml").read_bytes()
    old_body = (root / "subagents" / "review.md").read_bytes()
    original_replace = os.replace

    def fail_marker(
        source: str | os.PathLike[str], destination: str | os.PathLike[str]
    ) -> None:
        if Path(destination) == root / "layout.toml":
            raise OSError("simulated interruption before layout marker")
        original_replace(source, destination)

    with patch("aikito.workspace_core.os.replace", side_effect=fail_marker):
        with pytest.raises(OSError, match="simulated interruption"):
            apply_migration(build_migration_plan(root), tmp_path)

    assert (root / "agents.toml").read_bytes() == old_agents
    assert (root / "subagents.toml").read_bytes() == old_subagents
    assert (root / "subagents" / "review.md").read_bytes() == old_body
    assert not (root / "layout.toml").exists()
    assert not (root / "agents" / "codex.toml").exists()


def test_migration_preserves_crlf_instruction_body(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    body = b"# Review\r\n\r\nKeep CRLF.\r\n"
    path = root / "subagents" / "review.md"
    path.write_bytes(body)

    apply_migration(build_migration_plan(root), tmp_path)

    assert path.read_bytes().endswith(body)
    assert parse_subagent_file(path)[1].encode("utf-8") == body


def test_migration_keeps_comments_with_the_following_resource(tmp_path: Path) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    (root / "agents.toml").write_text(
        "# Agent registry header\n"
        "# General guidance for all agents\n\n"
        "# Codex CLI\n"
        "[agents.codex]\n"
        'display_name = "Codex"\n\n'
        "# Claude Code CLI\n"
        "[agents.claude-code]\n"
        'display_name = "Claude Code"\n',
        encoding="utf-8",
    )
    (root / "subagents.toml").write_text(
        "# Verifier section\n"
        "# Verifier must run with isolated context and not inherit parent conversation history.\n"
        "[subagents.review]\n"
        'description = "Review changes"\n'
        'agents = ["codex"]\n\n'
        "[subagents.review.codex]\n"
        'model = "o3"\n\n'
        "# Jira section\n"
        "[subagents.jira]\n"
        'description = "Track issues"\n'
        'agents = ["codex"]\n',
        encoding="utf-8",
    )
    (root / "subagents" / "jira.md").write_text("# Jira\n", encoding="utf-8")

    plan = build_migration_plan(root)
    assert not plan.blocked
    apply_migration(plan, tmp_path)

    codex = (root / "agents" / "codex.toml").read_text(encoding="utf-8")
    claude = (root / "agents" / "claude-code.toml").read_text(encoding="utf-8")
    assert codex.startswith("# Codex CLI\n")
    assert "# Agent registry header" not in codex
    assert "# Claude Code CLI" not in codex
    assert claude.startswith("# Claude Code CLI\n")

    review = (root / "subagents" / "review.md").read_text(encoding="utf-8")
    jira = (root / "subagents" / "jira.md").read_text(encoding="utf-8")
    assert "# Verifier must run with isolated context" in review.split("---", 2)[1]
    assert "# Jira section" not in review
    assert "# Jira section" in jira.split("---", 2)[1]
    assert (
        parse_subagent_file(root / "subagents" / "review.md")[0]["description"]
        == "Review changes"
    )


def test_empty_subagent_registry_keeps_comments_in_layout_marker(
    tmp_path: Path,
) -> None:
    root = tmp_path / "ws"
    root.mkdir()
    legacy_workspace(root)
    (root / "subagents" / "review.md").unlink()
    (root / "subagents.toml").write_text(
        "# Keep this registry note\n[subagents]\n", encoding="utf-8"
    )

    plan = build_migration_plan(root)
    assert not plan.blocked
    apply_migration(plan, tmp_path)

    assert "# Keep this registry note" in (root / "layout.toml").read_text(
        encoding="utf-8"
    )
    require_current_layout(root)
