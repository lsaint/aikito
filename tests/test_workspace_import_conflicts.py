"""Partial imports, per-resource choices, and reference-safe shared writes."""

from __future__ import annotations

import io
import os
import tomllib
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.cli_parser import build_parser
from aikito.init import init_workspace
from aikito.workspace_import import (
    WorkspaceImportError,
    apply_import_plan,
    build_import_plan,
    run_workspace_import,
)
from aikito.workspace_resource_write import reference_conflicts
from aikito.workspace_resources import Resource, snapshot_workspace


def _workspace(root: Path) -> Path:
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, root.parent / "home")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def _conflicting_notes(tmp_path: Path) -> tuple[Path, Path]:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    for root, value in ((source, "source"), (target, "target")):
        (root / "memory/notes/conflict.md").write_text(value, encoding="utf-8")
    (source / "memory/notes/new.md").write_text("new", encoding="utf-8")
    return source, target


def test_partial_application_repeats_without_touching_conflict(tmp_path: Path) -> None:
    source, target = _conflicting_notes(tmp_path)
    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=True)
    assert not plan.blocked and plan.conflicts
    assert not (target / "memory/notes/new.md").exists()
    apply_import_plan(plan, tmp_path / "home")
    assert (target / "memory/notes/new.md").read_text() == "new"
    assert (target / "memory/notes/conflict.md").read_text() == "target"
    again = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not again.changes and again.conflicts


@pytest.mark.parametrize("missing", (False, True))
def test_keep_target_skips_create_before_reference_checks(
    tmp_path: Path, missing: bool
) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    if missing:
        (source / "skills.toml").write_text('skills = ["missing"]\n')
        identity = "skill-selection:missing"
    else:
        (source / "memory/notes/new.md").write_text("new")
        identity = "memory:notes/new.md"
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={identity: "target"},
    )
    item = next(item for item in plan.items if item.resource.id == identity)
    assert item.action == "NOOP"
    assert item.reason == "Skipped by --keep-target; target kept unchanged"
    assert not plan.blocked and not plan.conflicts
    assert not (target / "memory/notes/new.md").exists()
    assert tomllib.loads((target / "skills.toml").read_text())["skills"] == []


@pytest.mark.parametrize("new_file", (False, True))
def test_keep_target_skips_config_field_in_shared_file(
    tmp_path: Path, new_file: bool
) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    config = source / "config.toml"
    config.write_text(
        config.read_text().replace("check = true", "check = false")
        + '\n[custom]\nkeep = 1979-05-27\nskip = "source"\n'
    )
    if new_file:
        (target / "config.toml").unlink()
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={"config:update.check": "target", "config:custom.skip": "target"},
    )
    assert not plan.blocked and not plan.conflicts
    result = tomllib.loads((target / "config.toml").read_text())
    assert "skip" not in result["custom"]
    assert str(result["custom"]["keep"]) == "1979-05-27"
    if new_file:
        assert "check" not in result.get("update", {})
    else:
        assert result["update"]["check"] is True


def test_keep_target_skips_new_project_fields_and_paths(tmp_path: Path) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    project = source / "projects/demo"
    project.mkdir()
    (project / "agent.toml").write_text(
        'name = "demo"\ncustom = "skip"\npaths = ["~/keep", "~/skip"]\n'
    )
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={
            "project-field:demo/custom": "target",
            "project-path:demo/~/skip": "target",
        },
    )
    assert not plan.blocked and not plan.conflicts
    result = tomllib.loads((target / "projects/demo/agent.toml").read_text())
    assert "custom" not in result
    assert result["paths"] == ["~/keep"]


@pytest.mark.parametrize("side", ("target", "source"))
def test_note_resolution_is_previewable_and_repeatable(
    tmp_path: Path, side: str
) -> None:
    source, target = _conflicting_notes(tmp_path)
    choices = {"memory:notes/conflict.md": side}
    plan = build_import_plan(source, target, resolutions=choices)
    assert not plan.conflicts and not plan.blocked
    assert (target / "memory/notes/conflict.md").read_text() == "target"
    apply_import_plan(plan, tmp_path / "home")
    assert (target / "memory/notes/conflict.md").read_text() == side
    assert (source / "memory/notes/conflict.md").read_text() == "source"
    again = run_workspace_import(
        source, target, tmp_path / "home", dry_run=False, resolutions=choices
    )
    assert not again.changes and not again.conflicts


def test_same_toml_file_can_apply_and_skip_fields(tmp_path: Path) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    for root, days in ((source, 90), (target, 60)):
        config = root / "config.toml"
        config.write_text(
            config.read_text().replace("stale_days = 30", f"stale_days = {days}"),
            encoding="utf-8",
        )
    config = source / "config.toml"
    config.write_text(config.read_text().replace("check = true", "check = false"))
    target_config = target / "config.toml"
    target_config.write_text("# Keep target comment\n" + target_config.read_text())
    first = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert [item.resource.id for item in first.conflicts] == [
        "config:memory.stale_days"
    ]
    document = tomllib.loads(target_config.read_text())
    assert document["memory"]["stale_days"] == 60
    assert document["update"]["check"] is False
    run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={"config:memory.stale_days": "source"},
    )
    assert tomllib.loads(target_config.read_text())["memory"]["stale_days"] == 90
    assert "# Keep target comment" in target_config.read_text()


def test_new_config_preserves_quoted_fields_and_custom_skills_field(
    tmp_path: Path,
) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    config = source / "config.toml"
    config.write_text(
        '"feature.flag" = true\nskills = ["metadata"]\n' + config.read_text()
    )
    expected = config.read_bytes()
    (target / "config.toml").unlink()
    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not plan.blocked and not plan.conflicts
    assert (target / "config.toml").read_bytes() == expected


@pytest.mark.parametrize("new_project", (False, True))
def test_skipped_members_do_not_leak_into_shared_files(
    tmp_path: Path, new_project: bool
) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    skill = source / "skills/safe"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Safe\n", encoding="utf-8")
    (source / "skills.toml").write_text('skills = ["safe", "missing"]\n')
    if new_project:
        project = source / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            'name = "demo"\nskills = ["safe", "missing"]\npaths = ["~/offline"]\n'
        )
    plan = run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not plan.blocked
    assert "skill-selection:missing" in {i.resource.id for i in plan.conflicts}
    assert tomllib.loads((target / "skills.toml").read_text())["skills"] == ["safe"]
    if new_project:
        document = tomllib.loads((target / "projects/demo/agent.toml").read_text())
        assert document["skills"] == ["safe"]
        assert document["paths"] == ["~/offline"]
        assert "project-skill:demo/missing" in {i.resource.id for i in plan.conflicts}
    actual = snapshot_workspace(target)
    assert not actual.findings
    assert "skill:safe" in actual.resources
    assert not build_import_plan(source, target).changes


@pytest.mark.parametrize("side", ("target", "source"))
def test_skill_resolution_uses_whole_directory(tmp_path: Path, side: str) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    for root, value in ((source, "source"), (target, "target")):
        skill = root / "skills/reviewer"
        skill.mkdir()
        (skill / "SKILL.md").write_text(value)
        (skill / f"{value}.txt").write_text(value)
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={"skill:reviewer": side},
    )
    assert not plan.conflicts
    skill = target / "skills/reviewer"
    assert (skill / "SKILL.md").read_text() == side
    assert {p.name for p in skill.iterdir()} == {"SKILL.md", f"{side}.txt"}


def test_cli_rejects_opposite_choices_for_same_resource(tmp_path: Path) -> None:
    source, target = _conflicting_notes(tmp_path)
    args = build_parser().parse_args(
        [
            "import",
            "workspace",
            str(source),
            "--keep-target",
            "memory:notes/conflict.md",
            "--take-source",
            "memory:notes/conflict.md",
        ]
    )
    with patch("aikito.cli.get_aikito_dir", return_value=target):
        with pytest.raises(WorkspaceImportError, match="Conflicting resolutions"):
            args.func(args)
    assert not (target / "memory/notes/new.md").exists()


def test_reference_pruning_skips_dependents_to_fixed_point() -> None:
    provider = Resource("agent", "new", "p", (), ("skill:missing",))
    dependent = Resource("mcp", "docs", "d", (), (provider.id,))
    independent = Resource("memory", "notes/new.md", "m", ())
    source = {r.id: r for r in (provider, dependent, independent)}
    conflicts, findings = reference_conflicts(source, {}, set(source))
    assert set(conflicts) == {provider.id, dependent.id}
    assert "skill:missing" in conflicts[provider.id]
    assert provider.id in conflicts[dependent.id]
    assert not findings


def test_source_resolution_cannot_bypass_reference_checks(tmp_path: Path) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    (source / "skills.toml").write_text('skills = ["missing"]\n')
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={"skill-selection:missing": "source"},
    )
    assert [i.resource.id for i in plan.conflicts] == ["skill-selection:missing"]
    assert tomllib.loads((target / "skills.toml").read_text())["skills"] == []


@pytest.mark.parametrize("side", ("source", "target"))
def test_inbox_resolution_preserves_existing_notes(tmp_path: Path, side: str) -> None:
    source, target = _workspace(tmp_path / "source"), _workspace(tmp_path / "target")
    for root, inbox in ((source, "source-inbox"), (target, "target-inbox")):
        config = root / "config.toml"
        config.write_text(
            config.read_text().replace('path = "inbox"', f'path = "{inbox}"')
        )
    note = target / "target-inbox/existing.md"
    note.parent.mkdir()
    note.write_text("existing")
    imported = source / "source-inbox/imported.md"
    imported.parent.mkdir()
    imported.write_text("imported")
    plan = run_workspace_import(
        source,
        target,
        tmp_path / "home",
        dry_run=False,
        resolutions={"config:inbox.path": side},
    )
    assert plan.blocked == (side == "source")
    assert note.read_text() == "existing"
    assert not (target / "source-inbox").exists()
    destination = target / "target-inbox/imported.md"
    if side == "target":
        assert destination.read_text() == "imported"
        assert not plan.conflicts
    else:
        assert not destination.exists()


def test_partial_transaction_failure_rolls_back_only_planned_changes(
    tmp_path: Path,
) -> None:
    source, target = _conflicting_notes(tmp_path)
    (source / "memory/notes/second.md").write_text("second")
    original = os.replace

    def fail_second(src: Path, dst: Path) -> None:
        if (
            "stage" in Path(src).parts
            and Path(dst) == target / "memory/notes/second.md"
        ):
            raise OSError("simulated failure")
        original(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=fail_second):
        with pytest.raises(OSError, match="simulated failure"):
            run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert not (target / "memory/notes/new.md").exists()
    assert (target / "memory/notes/conflict.md").read_text() == "target"


@pytest.mark.parametrize(
    "choices", ({"unknown:id": "source"}, {"memory:notes/new.md": "bad"})
)
def test_invalid_resolution_is_rejected_before_writes(
    tmp_path: Path, choices: dict
) -> None:
    source, target = _conflicting_notes(tmp_path)
    with pytest.raises(
        WorkspaceImportError, match="Unknown import resource|Invalid resolution"
    ):
        run_workspace_import(
            source, target, tmp_path / "home", dry_run=False, resolutions=choices
        )
    assert not (target / "memory/notes/new.md").exists()


@pytest.mark.parametrize("dry_run", (False, True))
def test_cli_reports_partial_application_and_resolves_on_retry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], dry_run: bool
) -> None:
    source, target = _conflicting_notes(tmp_path)
    args = build_parser().parse_args(
        ["import", "workspace", str(source)] + (["--dry-run"] if dry_run else [])
    )
    with (
        patch("aikito.cli.get_aikito_dir", return_value=target),
        patch("aikito.cli.Path.home", return_value=tmp_path / "home"),
    ):
        with pytest.raises(SystemExit) as result:
            args.func(args)
        assert result.value.code == 2
        output = capsys.readouterr()
        assert ("[DRY RUN]" if dry_run else "[PARTIAL]") in output.out
        assert "--take-source RESOURCE_ID" in output.err
        assert (target / "memory/notes/new.md").exists() is not dry_run
        args = build_parser().parse_args(
            [
                "import",
                "workspace",
                str(source),
                "--take-source",
                "memory:notes/conflict.md",
            ]
        )
        args.func(args)
        assert "[SUCCESS]" in capsys.readouterr().out
    assert (target / "memory/notes/conflict.md").read_text() == "source"
