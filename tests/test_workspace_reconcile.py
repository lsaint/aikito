"""Local two-workspace reconciliation and recovery behavior."""

from __future__ import annotations

import os
import random
import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito import workspace_core
from aikito.workspace_reconcile import (
    ResourceVersion,
    WorkspaceReconcileError,
    build_reconcile_plan,
    baseline_workspaces,
    compare_versions,
    run_reconciliation,
)


def _workspace(root: Path) -> Path:
    for directory in ("mcps", "memory/notes", "projects", "skills", "global"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for marker in ("agents.toml", "skills.toml", "subagents.toml"):
        (root / marker).write_text("", encoding="utf-8")
    return root


def _baseline(tmp_path: Path) -> tuple[Path, Path, Path]:
    left = _workspace(tmp_path / "left")
    right = _workspace(tmp_path / "right")
    home = tmp_path / "home"
    baseline_workspaces(left, right, home)
    return left, right, home


@pytest.mark.parametrize(
    ("base", "left", "right", "action", "target"),
    [
        (None, "a", None, "COPY", "right"),
        (None, "a", "a", "NOOP", None),
        (None, "a", "b", "CONFLICT", None),
        ("a", "b", "a", "COPY", "right"),
        ("a", "b", "b", "NOOP", None),
        ("a", "b", "c", "CONFLICT", None),
        ("a", None, "a", "DELETE", "right"),
        ("a", None, "b", "CONFLICT", None),
        ("a", None, None, "NOOP", None),
    ],
)
def test_three_way_decisions(
    base: str | None,
    left: str | None,
    right: str | None,
    action: str,
    target: str | None,
) -> None:
    def version(value: str | None) -> dict[str, ResourceVersion]:
        return {"memory/notes/a.md": ResourceVersion("memory", value)} if value else {}

    item = compare_versions(version(base), version(left), version(right))[0]
    assert (item.action, item.target) == (action, target)


def test_baseline_requires_identical_snapshots(tmp_path: Path) -> None:
    left = _workspace(tmp_path / "left")
    right = _workspace(tmp_path / "right")
    (left / "memory/notes/a.md").write_text("left", encoding="utf-8")
    with pytest.raises(WorkspaceReconcileError, match="differ"):
        baseline_workspaces(left, right, tmp_path / "home")
    assert not (left / ".local/state/aikito/workspace-reconcile/baseline.json").exists()


def test_legacy_baseline_requires_review(tmp_path: Path) -> None:
    left = _workspace(tmp_path / "left")
    right = _workspace(tmp_path / "right")
    legacy = left / ".local/state/aikito/workspace-reconcile/pair.json"
    legacy.parent.mkdir(parents=True)
    legacy.write_text("{}", encoding="utf-8")

    with pytest.raises(WorkspaceReconcileError, match="Legacy baseline state"):
        baseline_workspaces(left, right, tmp_path / "home")


def test_add_update_delete_and_repeat(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    rel = Path("memory/notes/a.md")
    (left / rel).write_text("first", encoding="utf-8")
    preview = run_reconciliation(left, right, home, dry_run=True)
    assert [(item.action, item.target) for item in preview.changes] == [
        ("COPY", "right")
    ]
    assert not (right / rel).exists()
    run_reconciliation(left, right, home, dry_run=False)
    assert (right / rel).read_text(encoding="utf-8") == "first"
    generation = build_reconcile_plan(left, right).generation
    run_reconciliation(left, right, home, dry_run=False)
    assert build_reconcile_plan(left, right).generation == generation

    (right / rel).write_text("second", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    assert (left / rel).read_text(encoding="utf-8") == "second"
    (left / rel).unlink()
    run_reconciliation(left, right, home, dry_run=False)
    assert not (right / rel).exists()
    assert not build_reconcile_plan(left, right).changes


def test_conflict_preserves_both_sides(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    rel = Path("memory/notes/a.md")
    (left / rel).write_text("base", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    (left / rel).write_text("left", encoding="utf-8")
    (right / rel).write_text("right", encoding="utf-8")
    plan = run_reconciliation(left, right, home, dry_run=False)
    assert [item.action for item in plan.conflicts] == ["CONFLICT"]
    assert (left / rel).read_text(encoding="utf-8") == "left"
    assert (right / rel).read_text(encoding="utf-8") == "right"


def test_skill_directory_is_one_resource(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    skill = left / "skills/example"
    skill.mkdir()
    (skill / "SKILL.md").write_text("# Example\n", encoding="utf-8")
    (skill / "empty").mkdir()
    run_reconciliation(left, right, home, dry_run=False)
    assert (right / "skills/example/empty").is_dir()
    assert (right / "skills/example/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Example\n"
    (right / "skills/example/SKILL.md").write_text("# Updated\n", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    assert (left / "skills/example/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Updated\n"
    shutil.rmtree(skill)
    run_reconciliation(left, right, home, dry_run=False)
    assert not (right / "skills/example").exists()


def test_reconcile_can_start_from_either_workspace(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/peer.md")
    (right / note).write_text("peer", encoding="utf-8")
    run_reconciliation(right, left, home, dry_run=False)
    assert (left / note).read_text(encoding="utf-8") == "peer"


def test_matching_concurrent_edits_advance_common_version(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/same.md")
    for root in (left, right):
        (root / note).write_text("same", encoding="utf-8")
    preview = build_reconcile_plan(left, right)
    assert [item.action for item in preview.items] == ["NOOP"]
    run_reconciliation(left, right, home, dry_run=False)
    assert build_reconcile_plan(left, right).generation == 1
    (left / note).unlink()
    run_reconciliation(left, right, home, dry_run=False)
    assert not (right / note).exists()


def test_seeded_random_replay(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    randomizer = random.Random(27)
    names = [Path(f"memory/notes/n{index}.md") for index in range(5)]
    for iteration in range(30):
        side = randomizer.choice((left, right))
        note = randomizer.choice(names)
        if randomizer.choice((True, False)):
            (side / note).write_text(f"round {iteration}\n", encoding="utf-8")
        else:
            (side / note).unlink(missing_ok=True)
        run_reconciliation(left, right, home, dry_run=False)
        for name in names:
            a, b = left / name, right / name
            assert a.exists() == b.exists()
            if a.exists():
                assert a.read_bytes() == b.read_bytes()
        assert not build_reconcile_plan(left, right).changes


def test_interrupted_update_recovers_before_next_round(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    rel = Path("memory/notes/a.md")
    (left / rel).write_text("base", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    (left / rel).write_text("changed", encoding="utf-8")
    original_replace = os.replace
    interrupted = False

    def interrupt_stage(src: Path, dst: Path) -> None:
        nonlocal interrupted
        if not interrupted and "stage" in Path(src).parts and Path(dst) == right / rel:
            interrupted = True
            raise KeyboardInterrupt
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=interrupt_stage):
        with pytest.raises(KeyboardInterrupt):
            run_reconciliation(left, right, home, dry_run=False)
    assert not (right / rel).exists()
    with pytest.raises(WorkspaceReconcileError, match="Pending round"):
        build_reconcile_plan(left, right)
    with pytest.raises(WorkspaceReconcileError, match="Recovered an interrupted"):
        run_reconciliation(left, right, home, dry_run=False)
    assert (right / rel).read_text(encoding="utf-8") == "base"
    run_reconciliation(left, right, home, dry_run=False)
    assert (right / rel).read_text(encoding="utf-8") == "changed"


def test_recovery_preserves_external_change(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/a.md")
    (left / note).write_text("base", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    (left / note).write_text("changed", encoding="utf-8")
    original_replace = os.replace

    def interrupt_stage(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == right / note:
            raise KeyboardInterrupt
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=interrupt_stage):
        with pytest.raises(KeyboardInterrupt):
            run_reconciliation(left, right, home, dry_run=False)
    (right / note).write_text("external", encoding="utf-8")
    with pytest.raises(WorkspaceReconcileError, match="externally changed"):
        run_reconciliation(left, right, home, dry_run=False)
    assert (right / note).read_text(encoding="utf-8") == "external"


def test_staging_failure_leaves_no_pending_round(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/a.md")
    (left / note).write_text("new", encoding="utf-8")
    with patch(
        "aikito.workspace_core._copy_resource", side_effect=OSError("stage failed")
    ):
        with pytest.raises(OSError, match="stage failed"):
            run_reconciliation(left, right, home, dry_run=False)
    assert not (right / note).exists()
    assert not (
        left / ".local/state/aikito/workspace-transactions/pending.json"
    ).exists()
    assert not (
        right / ".local/state/aikito/workspace-transactions/pending.json"
    ).exists()
    assert len(build_reconcile_plan(left, right).changes) == 1


def test_second_write_failure_rolls_back_first_resource(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    for name in ("a", "b"):
        (left / f"memory/notes/{name}.md").write_text(name, encoding="utf-8")
    original_replace = os.replace

    def fail_second_stage(src: Path, dst: Path) -> None:
        if "stage" in Path(src).parts and Path(dst) == right / "memory/notes/b.md":
            raise OSError("second write failed")
        original_replace(src, dst)

    with patch("aikito.workspace_core.os.replace", side_effect=fail_second_stage):
        with pytest.raises(OSError, match="second write failed"):
            run_reconciliation(left, right, home, dry_run=False)
    assert not (right / "memory/notes/a.md").exists()
    assert not (right / "memory/notes/b.md").exists()
    assert len(build_reconcile_plan(left, right).changes) == 2


def test_baseline_write_failure_restores_resources_and_generation(
    tmp_path: Path,
) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/a.md")
    (left / note).write_text("new", encoding="utf-8")
    original_write = workspace_core.atomic_text
    failed = False

    def fail_second_baseline(path: Path, content: str) -> None:
        nonlocal failed
        if (
            path == right / ".local/state/aikito/workspace-reconcile/baseline.json"
            and not failed
        ):
            failed = True
            raise OSError("baseline write failed")
        original_write(path, content)

    with patch("aikito.workspace_core.atomic_text", side_effect=fail_second_baseline):
        with pytest.raises(OSError, match="baseline write failed"):
            run_reconciliation(left, right, home, dry_run=False)
    assert not (right / note).exists()
    assert build_reconcile_plan(left, right).generation == 0


def test_changed_staging_is_rejected_before_target_write(tmp_path: Path) -> None:
    left, right, home = _baseline(tmp_path)
    note = Path("memory/notes/a.md")
    (left / note).write_text("expected", encoding="utf-8")
    original_fingerprint = workspace_core._fingerprint_private
    stage_reads = 0

    def tamper_stage(path: Path, kind: str) -> str | None:
        nonlocal stage_reads
        if "stage" in path.parts and path.name == note.name:
            stage_reads += 1
            if stage_reads == 2:
                path.write_text("changed", encoding="utf-8")
        return original_fingerprint(path, kind)

    with patch("aikito.workspace_core._fingerprint_private", side_effect=tamper_stage):
        with pytest.raises(WorkspaceReconcileError, match="Staged resource changed"):
            run_reconciliation(left, right, home, dry_run=False)
    assert not (right / note).exists()
    assert len(build_reconcile_plan(left, right).changes) == 1
