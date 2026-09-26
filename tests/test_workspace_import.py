"""Import behavior across isolated workspace directories."""

from __future__ import annotations

import json
import io
import os
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.cli_parser import build_parser
from aikito.init import init_project, init_workspace
from aikito.templating import load_template
from aikito.workspace_core import PathPolicy, WorkspaceCoreError, validate_resource_path
from aikito.workspace_import import (
    WorkspaceImportError,
    apply_import_plan,
    build_import_plan,
    recover_imports,
    run_workspace_import,
)


def test_import_workspace_is_a_public_command() -> None:
    args = build_parser().parse_args(
        ["import", "workspace", "/tmp/source", "--dry-run", "--verbose"]
    )
    assert args.source == Path("/tmp/source")
    assert args.dry_run
    assert args.verbose


@pytest.mark.parametrize(
    "path",
    (
        "/tmp/memory/index.md",
        "memory/../index.md",
        "memory/.git/index.md",
        "memory/.local/index.md",
    ),
)
def test_core_rejects_unsafe_paths_even_with_caller_policy(path: str) -> None:
    with pytest.raises(WorkspaceCoreError, match="Unsafe resource path"):
        validate_resource_path(
            path, "memory", PathPolicy(resources=(("memory", path),))
        )


def _workspace(root: Path) -> Path:
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, root.parent / "home")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def test_public_import_dry_run_reports_conflict_without_writing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    relative = Path("memory/notes/example.md")
    (source / relative).write_text("source\n", encoding="utf-8")
    (target / relative).write_text("target\n", encoding="utf-8")
    args = build_parser().parse_args(["import", "workspace", str(source), "--dry-run"])
    with patch("aikito.cli.get_aikito_dir", return_value=target):
        with pytest.raises(SystemExit) as result:
            args.func(args)
    assert result.value.code == 1
    output = capsys.readouterr()
    assert (
        "[CONFLICT] memory/notes/example.md (memory:notes/example.md): Contents differ"
        in output.out
    )
    assert "[SUMMARY]" in output.out
    assert "Make the conflicting source or target resources agree" in output.err
    assert (target / relative).read_text(encoding="utf-8") == "target\n"


def test_public_import_groups_merges_and_hides_noise(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for name in ("first", "second"):
        skill = source / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(f"# {name}\n", encoding="utf-8")
    (source / "skills.toml").write_text(
        'skills = ["first", "second"]\n', encoding="utf-8"
    )
    (source / "docs").mkdir()
    (target / "docs").mkdir()
    (source / ".pytest_cache").mkdir()
    (source / ".ruff_cache").mkdir()
    with patch("aikito.cli.get_aikito_dir", return_value=target):
        args = build_parser().parse_args(
            ["import", "workspace", str(source), "--dry-run"]
        )
        args.func(args)
        compact = capsys.readouterr().out
        assert compact.count("[MERGE] skills.toml") == 1
        assert "[NOOP]" not in compact
        assert "[SKIPPED" not in compact
        excluded = build_import_plan(source, target).excluded
        assert all(".pytest_cache" not in entry for entry in excluded)
        assert all(".ruff_cache" not in entry for entry in excluded)
        assert all("SKIPPED Target" not in entry for entry in excluded)
        skipped_count = len(excluded)
        assert skipped_count >= 1
        assert f"SKIPPED {skipped_count}" in compact

        verbose_args = build_parser().parse_args(
            ["import", "workspace", str(source), "--dry-run", "--verbose"]
        )
        verbose_args.func(verbose_args)
    verbose = capsys.readouterr().out
    assert verbose.count("[MERGE] skills.toml") == 1
    assert "[SKIPPED Source docs]" in verbose
    assert "SKIPPED Target" not in verbose
    assert ".pytest_cache" not in verbose
    assert ".ruff_cache" not in verbose


def test_public_import_suggests_binding_new_offline_project(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    project = source / "projects" / "blog"
    project.mkdir()
    (project / "agent.toml").write_text(
        'name = "blog"\npath = "~/not-cloned"\nskills = []\n', encoding="utf-8"
    )
    home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    args = build_parser().parse_args(["import", "workspace", str(source)])
    with patch("aikito.cli.get_aikito_dir", return_value=target):
        args.func(args)
    output = capsys.readouterr().out
    assert "[NEXT] Run aikito sync --dry-run" in output
    assert "aikito sync project blog <path>" in output
    assert "aikito git status" in output


def test_import_reports_missing_source_and_scoped_migration(tmp_path: Path) -> None:
    target = _workspace(tmp_path / "target")
    missing = tmp_path / "missing"
    with pytest.raises(WorkspaceImportError, match="Source is not an Aikito workspace"):
        build_import_plan(missing, target)

    source = _workspace(tmp_path / "source")
    (source / "layout.toml").unlink()
    (source / "agents.toml").write_text("[agents]\n", encoding="utf-8")
    (source / "subagents.toml").write_text("[subagents]\n", encoding="utf-8")
    with pytest.raises(WorkspaceImportError) as error:
        build_import_plan(source, target)
    assert str(source) in str(error.value)
    if os.name == "nt":
        assert "$env:AIKITO_DIR" in str(error.value)
    else:
        assert f"AIKITO_DIR={source}" in str(error.value)


def test_import_creates_memory_then_becomes_noop(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    home = tmp_path / "home"
    note = Path("memory/notes/example.md")
    (source / note).write_text("# Example\n", encoding="utf-8")

    preview = run_workspace_import(source, target, home, dry_run=True)
    assert [
        (item.action, item.resource.relative_path)
        for item in preview.items
        if item.resource.kind == "memory"
    ] == [("CREATE", note)]
    assert not (target / note).exists()
    assert not home.exists()
    assert not (target / ".local").exists()

    run_workspace_import(source, target, home, dry_run=False)
    assert (target / note).read_text(encoding="utf-8") == "# Example\n"
    again = build_import_plan(source, target)
    assert [item.action for item in again.items if item.resource.kind == "memory"] == [
        "NOOP"
    ]


def test_conflict_blocks_all_creates(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for name in ("a", "b"):
        (source / "memory/notes" / f"{name}.md").write_text(name, encoding="utf-8")
    (target / "memory/notes/a.md").write_text("other", encoding="utf-8")

    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert [item.action for item in plan.items if item.resource.kind == "memory"] == [
        "CONFLICT",
        "CREATE",
    ]
    assert not (target / "memory/notes/b.md").exists()
    assert (target / "memory/notes/a.md").read_text(encoding="utf-8") == "other"


def test_imports_user_skill_without_replacing_system_skill(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    skill = source / "skills" / "example"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    bundled = source / "skills" / "aikito"
    (bundled / "SKILL.md").write_text("source version", encoding="utf-8")
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


def test_import_creates_missing_project_and_memory_indexes(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "memory/index.md").write_text("# Global index\n", encoding="utf-8")
    notes = source / "projects/example/memory/notes"
    notes.mkdir(parents=True)
    (source / "projects/example/agent.toml").write_text(
        'name = "example"\npaths = ["~/not-cloned"]\nskills = []\n', encoding="utf-8"
    )
    (source / "projects/example/memory/index.md").write_text(
        "# Project index\n", encoding="utf-8"
    )
    (source / "projects/example/memory/README.md").write_text(
        "# Project memory\n", encoding="utf-8"
    )
    (notes / "decision.md").write_text("# Decision\n", encoding="utf-8")
    plan = build_import_plan(source, target)
    assert not plan.blocked
    assert not (target / "projects/example").exists()
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    for path in (
        "memory/index.md",
        "projects/example/agent.toml",
        "projects/example/memory/index.md",
        "projects/example/memory/README.md",
        "projects/example/memory/notes/decision.md",
    ):
        assert (target / path).is_file()
    assert not (tmp_path / "not-cloned").exists()
    assert not build_import_plan(source, target).blocked


def test_import_creates_multiple_missing_projects(tmp_path: Path) -> None:
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
    assert not plan.blocked
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "projects/alpha/memory/notes/decision.md").is_file()
    assert (target / "projects/beta/memory/notes/decision.md").is_file()


def test_project_defaults_and_instruction_template_adopt_source(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    with redirect_stdout(io.StringIO()):
        assert init_project(target, checkout, "demo", home=tmp_path) == "demo"
    project = source / "projects/demo"
    project.mkdir()
    (project / "agent.toml").write_text(
        'name = "demo"\npath = "~/checkout"\nsync_mode = "copy"\n'
        'description = "Source description"\nskills = []\n',
        encoding="utf-8",
    )
    (project / "AGENTS.md").write_text("# Source instructions\n", encoding="utf-8")
    preview = build_import_plan(source, target)
    assert not preview.blocked
    assert {item.action for item in preview.items} >= {"MERGE", "UPDATE"}
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    with (target / "projects/demo/agent.toml").open("rb") as stream:
        config = tomllib.load(stream)
    assert config["sync_mode"] == "copy"
    assert config["description"] == "Source description"
    assert (target / "projects/demo/AGENTS.md").read_text(
        encoding="utf-8"
    ) == "# Source instructions\n"
    assert not build_import_plan(source, target).blocked


def test_changed_project_values_still_conflict(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root, mode, instructions in (
        (source, "copy", "# Source\n"),
        (target, "manual", "# Target\n"),
    ):
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\nsync_mode = "{mode}"\n', encoding="utf-8"
        )
        (project / "AGENTS.md").write_text(instructions, encoding="utf-8")
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert sum(item.action == "CONFLICT" for item in plan.items) == 2


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


def test_imports_inbox_subagent_and_mcp_together(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root in (source, target):
        (root / "agents/codex.toml").write_text(
            '[agents.codex]\ndisplay_name = "Codex"\n', encoding="utf-8"
        )
    note = source / "inbox/ideas/first.md"
    note.parent.mkdir(parents=True)
    note.write_text("# First\n", encoding="utf-8")
    (source / "subagents/checker.md").write_text(
        '---\ndescription: "Check"\nagents: ["codex"]\n---\nCheck.\n',
        encoding="utf-8",
    )
    (source / "mcps/docs.toml").write_text(
        'transport = "remote"\nagents = ["codex"]\n', encoding="utf-8"
    )

    preview = build_import_plan(source, target)
    assert not preview.blocked
    assert {(item.resource.kind, item.action) for item in preview.items} >= {
        ("inbox", "CREATE"),
        ("subagent", "CREATE"),
        ("mcp", "CREATE"),
    }
    assert not (target / "inbox").exists()
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "inbox/ideas/first.md").read_text(encoding="utf-8") == "# First\n"
    assert (target / "subagents/checker.md").is_file()
    assert (target / "mcps/docs.toml").is_file()
    assert all(
        item.action == "NOOP" for item in build_import_plan(source, target).items
    )


def test_merge_selection_and_project_sets_preserves_other_fields(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root, name in ((source, "from-source"), (target, "at-target")):
        skill = root / "skills" / name
        skill.mkdir()
        (skill / "SKILL.md").write_text(name, encoding="utf-8")
        (root / "skills.toml").write_text(
            f'# keep {name}\nskills = ["{name}"]\n', encoding="utf-8"
        )
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\npath = "~/{name}"\nskills = ["{name}"]\n'
            'description = "keep me"\n',
            encoding="utf-8",
        )
    preview = build_import_plan(source, target)
    assert not preview.blocked
    assert sum(item.action == "MERGE" for item in preview.items) == 3
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    with (target / "skills.toml").open("rb") as stream:
        assert tomllib.load(stream)["skills"] == ["at-target", "from-source"]
    project_text = (target / "projects/demo/agent.toml").read_text(encoding="utf-8")
    assert 'description = "keep me"' in project_text
    with (target / "projects/demo/agent.toml").open("rb") as stream:
        merged = tomllib.load(stream)
    assert merged["paths"] == ["~/at-target", "~/from-source"]
    assert merged["skills"] == ["at-target", "from-source"]
    assert not build_import_plan(source, target).blocked


def test_missing_references_block_all_changes(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "subagents/checker.md").write_text(
        '---\ndescription: "Check"\nagents: ["nonexistent"]\n---\nCheck.\n',
        encoding="utf-8",
    )
    (source / "skills.toml").write_text('skills = ["missing"]\n', encoding="utf-8")
    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert plan.blocked
    assert any(
        "subagent:checker references missing agent:nonexistent" in f
        for f in plan.findings
    )
    assert any(
        "skill-selection:missing references missing skill:missing" in f
        for f in plan.findings
    )
    assert not (target / "subagents/checker.md").exists()


def test_external_inbox_is_reported_and_blocks_import(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "config.toml").write_text(
        f'[inbox]\npath = "{tmp_path / "external"}"\n', encoding="utf-8"
    )
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert "Source inbox is outside the workspace" in plan.findings


@pytest.mark.skipif(os.name == "nt", reason="symlink creation needs privileges")
def test_symlinked_inbox_is_a_finding(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "actual-inbox").mkdir()
    (source / "inbox").symlink_to(source / "actual-inbox", target_is_directory=True)
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert any("Inbox path crosses a symbolic link" in f for f in plan.findings)


def test_inbox_uses_target_configured_location(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "inbox").mkdir()
    (source / "inbox/note.md").write_text("# Note\n", encoding="utf-8")
    (target / "config.toml").write_text(
        '[inbox]\npath = "notes/inbox"\n', encoding="utf-8"
    )
    plan = build_import_plan(source, target)
    assert not plan.blocked
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "notes/inbox/note.md").read_text(encoding="utf-8") == "# Note\n"
    assert not (target / "inbox/note.md").exists()


def test_configured_inbox_change_imports_notes_to_resulting_location(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    config = source / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'path = "inbox"', 'path = "notes/inbox"'
        ),
        encoding="utf-8",
    )
    note = source / "notes/inbox/note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n", encoding="utf-8")
    preview = build_import_plan(source, target)
    assert not preview.blocked
    assert preview.inbox_prefix == "notes/inbox"
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "notes/inbox/note.md").is_file()
    assert not (target / "inbox/note.md").exists()
    assert not build_import_plan(source, target).blocked


def test_recover_inbox_import_before_config_is_installed(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    config = source / "config.toml"
    config.write_text(
        config.read_text(encoding="utf-8").replace(
            'path = "inbox"', 'path = "notes/inbox"'
        ),
        encoding="utf-8",
    )
    note = source / "notes/inbox/note.md"
    note.parent.mkdir(parents=True)
    note.write_text("# Note\n", encoding="utf-8")
    original_replace = os.replace

    def interrupt(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == target / "notes/inbox/note.md":
            raise KeyboardInterrupt
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert recover_imports(target)
    assert not (target / "notes/inbox/note.md").exists()
    assert "[inbox]\n# Staging directory" in (target / "config.toml").read_text(
        encoding="utf-8"
    )


def test_import_merges_named_project_paths(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root, label in ((source, "source"), (target, "target")):
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\n[paths]\n{label} = "~/{label}"\n',
            encoding="utf-8",
        )

    plan = build_import_plan(source, target)
    assert not plan.blocked
    assert any(item.action == "MERGE" for item in plan.items)
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    with (target / "projects/demo/agent.toml").open("rb") as stream:
        paths = tomllib.load(stream)["paths"]
    assert set(paths.values()) == {"~/target", "~/source"}


def test_project_path_merge_keeps_distinct_raw_values(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root, path in ((source, "~/same"), (target, str(Path.home() / "same"))):
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\npath = {json.dumps(path)}\n', encoding="utf-8"
        )
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    with (target / "projects/demo/agent.toml").open("rb") as stream:
        paths = tomllib.load(stream)["paths"]
    assert set(paths) == {"~/same", str(Path.home() / "same")}
    assert not build_import_plan(source, target).blocked
    assert all(
        item.action != "MERGE" for item in build_import_plan(source, target).items
    )


def test_import_rolls_back_merged_lists_and_created_file(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    skill = source / "skills/demo"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Demo\n", encoding="utf-8")
    (source / "skills.toml").write_text('skills = ["demo"]\n', encoding="utf-8")
    original = (target / "skills.toml").read_text(encoding="utf-8")
    original_replace = os.replace

    def fail_merge(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == target / "skills.toml":
            raise OSError("simulated merge failure")
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=fail_merge):
        with pytest.raises(OSError, match="simulated merge failure"):
            run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not (target / "skills/demo").exists()
    assert (target / "skills.toml").read_text(encoding="utf-8") == original


def test_second_batch_adopts_agent_config_and_global_templates(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    template = load_template("agents/codex.toml")
    (target / "agents/codex.toml").write_text(template, encoding="utf-8")
    custom_agent = template.replace(
        'display_name = "Codex"', 'display_name = "My Codex"'
    )
    assert custom_agent != template
    (source / "agents/codex.toml").write_text(custom_agent, encoding="utf-8")
    original_config = (target / "config.toml").read_text(encoding="utf-8")
    (source / "config.toml").write_text(
        original_config.replace("stale_days = 30", "stale_days = 90").replace(
            "check = true", "check = false"
        ),
        encoding="utf-8",
    )
    (source / "global/AGENTS.md").write_text(
        "# Imported global instructions\n", encoding="utf-8"
    )
    preview = build_import_plan(source, target)
    assert not preview.blocked
    assert sum(item.action == "UPDATE" for item in preview.items) >= 2
    assert sum(item.action == "MERGE" for item in preview.items) >= 2
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "agents/codex.toml").read_text(encoding="utf-8") == custom_agent
    assert (target / "global/AGENTS.md").read_text(
        encoding="utf-8"
    ) == "# Imported global instructions\n"
    config_text = (target / "config.toml").read_text(encoding="utf-8")
    assert "# Number of days" in config_text
    with (target / "config.toml").open("rb") as stream:
        config = tomllib.load(stream)
    assert config["memory"]["stale_days"] == 90
    assert config["update"]["check"] is False
    assert config["inbox"]["path"] == "inbox"
    assert not build_import_plan(source, target).blocked


def test_second_batch_preserves_custom_target_and_conflicts_when_both_edit(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    template = load_template("agents/codex.toml")
    (source / "agents/codex.toml").write_text(template, encoding="utf-8")
    (target / "agents/codex.toml").write_text(
        template.replace('display_name = "Codex"', 'display_name = "Target"'),
        encoding="utf-8",
    )
    plan = build_import_plan(source, target)
    assert not plan.blocked
    assert any(
        item.resource.kind == "agent" and item.action == "NOOP" for item in plan.items
    )
    (source / "agents/codex.toml").write_text(
        template.replace('display_name = "Codex"', 'display_name = "Source"'),
        encoding="utf-8",
    )
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert any(
        item.resource.kind == "agent" and item.action == "CONFLICT"
        for item in plan.items
    )


def test_workspace_config_and_global_instructions_use_template_baseline(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    target_config = target / "config.toml"
    target_config.write_text(
        target_config.read_text(encoding="utf-8").replace(
            "stale_days = 30", "stale_days = 40"
        ),
        encoding="utf-8",
    )
    (target / "global/AGENTS.md").write_text(
        "# Target instructions\n", encoding="utf-8"
    )
    plan = build_import_plan(source, target)
    assert not plan.blocked
    assert any(
        item.resource.name == "memory.stale_days" and item.action == "NOOP"
        for item in plan.items
    )
    (source / "config.toml").write_text(
        (source / "config.toml")
        .read_text(encoding="utf-8")
        .replace("stale_days = 30", "stale_days = 90"),
        encoding="utf-8",
    )
    (source / "global/AGENTS.md").write_text(
        "# Source instructions\n", encoding="utf-8"
    )
    plan = build_import_plan(source, target)
    assert plan.blocked
    assert {item.resource.kind for item in plan.conflicts} >= {
        "config",
        "global-instructions",
    }


def test_import_creates_missing_workspace_config(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (target / "config.toml").unlink()
    plan = build_import_plan(source, target)
    assert not plan.blocked
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "config.toml").is_file()
    assert not build_import_plan(source, target).blocked


def test_preview_blocks_unrenderable_config_merge(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    with (source / "config.toml").open("a", encoding="utf-8") as stream:
        stream.write("\n[feature]\nenabled = true\n")
    target_config = target / "config.toml"
    target_config.write_text(
        "feature = 42\n" + target_config.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    preview = build_import_plan(source, target)
    assert preview.blocked
    assert any("Invalid merged TOML" in finding for finding in preview.findings)
    assert not (target / ".local").exists()


def test_new_agent_is_available_to_same_batch_subagent(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (target / "agents/codex.toml").unlink(missing_ok=True)
    (source / "agents/codex.toml").write_text(
        load_template("agents/codex.toml"), encoding="utf-8"
    )
    (source / "subagents/checker.md").write_text(
        '---\ndescription: "Check"\nagents: ["codex"]\n---\nCheck.\n',
        encoding="utf-8",
    )
    plan = build_import_plan(source, target)
    assert not plan.blocked
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert (target / "agents/codex.toml").is_file()
    assert (target / "subagents/checker.md").is_file()
