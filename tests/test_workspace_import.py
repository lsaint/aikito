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
from aikito.workspace_core import PathPolicy, WorkspaceCoreError, validate_resource_path
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
