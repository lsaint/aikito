"""Import behavior across isolated workspace directories."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.cli_parser import build_parser
from aikito.workspace_import import (
    WorkspaceImportError,
    apply_import_plan,
    build_import_plan,
    recover_imports,
    run_workspace_import,
)


def test_partial_import_is_not_a_public_command() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["import", "workspace", "/tmp/source"])


def _workspace(root: Path) -> Path:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects",
        "skills",
        "global",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def test_import_creates_memory_then_becomes_noop(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    home = tmp_path / "home"
    note = Path("memory/notes/example.md")
    (source / note).write_text("# Example\n", encoding="utf-8")

    preview = run_workspace_import(source, target, home, dry_run=True)
    assert [(item.action, item.resource.relative_path) for item in preview.items] == [
        ("CREATE", note)
    ]
    assert not (target / note).exists()
    assert not home.exists()
    assert not (target / ".local").exists()

    run_workspace_import(source, target, home, dry_run=False)
    assert (target / note).read_text(encoding="utf-8") == "# Example\n"
    again = build_import_plan(source, target)
    assert [item.action for item in again.items] == ["NOOP"]


def test_conflict_blocks_all_creates(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for name in ("a", "b"):
        (source / "memory/notes" / f"{name}.md").write_text(name, encoding="utf-8")
    (target / "memory/notes/a.md").write_text("other", encoding="utf-8")

    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert [item.action for item in plan.items] == ["CONFLICT", "CREATE"]
    assert not (target / "memory/notes/b.md").exists()
    assert (target / "memory/notes/a.md").read_text(encoding="utf-8") == "other"


def test_imports_user_skill_without_replacing_system_skill(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    skill = source / "skills" / "example"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    bundled = source / "skills" / "aikito"
    bundled.mkdir()
    (bundled / "SKILL.md").write_text("source version", encoding="utf-8")
    (target / "skills" / "aikito").mkdir()
    (target / "skills" / "aikito" / "SKILL.md").write_text(
        "target version", encoding="utf-8"
    )

    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "skills/example/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Example\n"
    assert (target / "skills/aikito/SKILL.md").read_text(
        encoding="utf-8"
    ) == "target version"


def test_import_skips_system_artifacts_in_skill(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    skill = source / "skills/example"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    (skill / ".DS_Store").write_text("junk", encoding="utf-8")

    run_workspace_import(source, target, tmp_path / "home", dry_run=False)

    assert (target / "skills/example/SKILL.md").is_file()
    assert not (target / "skills/example/.DS_Store").exists()


def test_project_memory_requires_existing_target_project(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    notes = source / "projects/example/memory/notes"
    notes.mkdir(parents=True)
    (source / "projects/example/agent.toml").write_text("", encoding="utf-8")
    (notes / "decision.md").write_text("# Decision\n", encoding="utf-8")
    plan = build_import_plan(source, target)
    assert [item.action for item in plan.items] == ["BLOCKED"]
    assert "Run aikito init project example first" in plan.findings[0]


def test_plan_reports_every_missing_target_project(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for name in ("alpha", "beta"):
        project = source / "projects" / name
        (project / "memory/notes").mkdir(parents=True)
        (project / "agent.toml").write_text("", encoding="utf-8")
        (project / "memory/notes/decision.md").write_text(
            "# Decision\n", encoding="utf-8"
        )

    plan = build_import_plan(source, target)

    assert [item.action for item in plan.items] == ["BLOCKED", "BLOCKED"]
    assert len(plan.findings) == 2
    assert "init project alpha" in plan.findings[0]
    assert "init project beta" in plan.findings[1]


def test_source_symlink_is_rejected(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    secret = tmp_path / "outside.md"
    secret.write_text("outside", encoding="utf-8")
    (source / "memory/notes/foreign.md").symlink_to(secret)
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert any("regular file" in finding for finding in plan.findings)


def test_plaintext_credential_warns_without_blocking_import(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "memory/notes/private.md").write_text(
        "api_key = abcdefghijklmnop123456\n", encoding="utf-8"
    )
    preview = run_workspace_import(source, target, tmp_path / "home", dry_run=True)
    assert not preview.blocked
    assert [
        (finding.status, finding.code, finding.resource) for finding in preview.warnings
    ] == [("warning", "possible-credential", "memory/notes/private.md")]
    assert not (target / "memory/notes/private.md").exists()
    applied = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert applied.warnings == preview.warnings
    assert (target / "memory/notes/private.md").read_text(encoding="utf-8") == (
        "api_key = abcdefghijklmnop123456\n"
    )


def test_plan_is_invalidated_by_target_change(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    note = Path("memory/notes/example.md")
    (source / note).write_text("source", encoding="utf-8")
    plan = build_import_plan(source, target)
    (target / note).write_text("new target content", encoding="utf-8")
    with pytest.raises(WorkspaceImportError, match="changed after planning"):
        apply_import_plan(plan, tmp_path / "home")
    assert (target / note).read_text(encoding="utf-8") == "new target content"


def test_existing_project_name_may_contain_underscore(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    source_notes = source / "projects/My_Project/memory/notes"
    target_notes = target / "projects/My_Project/memory/notes"
    source_notes.mkdir(parents=True)
    target_notes.mkdir(parents=True)
    (source / "projects/My_Project/agent.toml").write_text("", encoding="utf-8")
    (target / "projects/My_Project/agent.toml").write_text("", encoding="utf-8")
    (source_notes / "decision.md").write_text("# Decision\n", encoding="utf-8")
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target_notes / "decision.md").is_file()


def test_mid_apply_failure_rolls_back_created_notes(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for name in ("a", "b"):
        (source / "memory/notes" / f"{name}.md").write_text(name, encoding="utf-8")

    original_replace = os.replace

    def fail_second(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == target / "memory/notes/b.md":
            raise OSError("simulated failure")
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=fail_second):
        with pytest.raises(OSError, match="simulated failure"):
            run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not (target / "memory/notes/a.md").exists()
    assert not (target / "memory/notes/b.md").exists()


def test_recovers_pending_journal_before_next_write(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    note = Path("memory/notes/example.md")
    (source / note).write_text("pending", encoding="utf-8")
    original_replace = os.replace

    def interrupt(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == target / note:
            raise KeyboardInterrupt
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / ".local/state/aikito/workspace-transactions/pending.json").exists()
    assert recover_imports(target)
    assert not (target / note).exists()
    assert not (
        target / ".local/state/aikito/workspace-transactions/pending.json"
    ).exists()
