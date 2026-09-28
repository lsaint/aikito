"""Logical-level characterization of import and reconcile decisions.

These tests pin outcomes per resource ID and the resulting workspace
contents, not the plan vocabulary or CLI output, so that the planners can be
restructured without changing observable semantics.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from aikito.init import init_workspace
from aikito.templating import load_template, render_project_files
from aikito.workspace_import import build_import_plan, run_workspace_import
from aikito.workspace_merge import compare
from aikito.workspace_reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    baseline_workspaces,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace_resources import snapshot_workspace
from aikito.workspace_templates import template_fingerprints

_OUTCOME = {
    "CREATE": "adopt",
    "UPDATE": "adopt",
    "NOOP": "keep",
    "CONFLICT": "conflict",
    "BLOCKED": "blocked",
}
_TEMPLATE = object()


def _workspace(root: Path) -> Path:
    with redirect_stdout(io.StringIO()):
        assert init_workspace(root, root.parent / "home")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def _outcomes(source: Path, target: Path) -> dict[str, str]:
    plan = build_import_plan(source, target)
    return {
        f"{item.resource.kind}:{item.resource.name}": _OUTCOME[item.action]
        for item in plan.items
    }


def _fingerprints(root: Path) -> dict[str, str]:
    return {
        key: resource.fingerprint
        for key, resource in snapshot_workspace(root).resources.items()
    }


# Import: resources without a template baseline.


def _memory(root: Path, value: str) -> None:
    (root / "memory/notes/a.md").write_text(value, encoding="utf-8")


def _project_memory(root: Path, value: str) -> None:
    notes = root / "projects/demo/memory/notes"
    notes.mkdir(parents=True, exist_ok=True)
    (root / "projects/demo/agent.toml").write_text('name = "demo"\n', encoding="utf-8")
    (notes / "a.md").write_text(value, encoding="utf-8")


def _skill(root: Path, value: str) -> None:
    (root / "skills/alpha").mkdir(exist_ok=True)
    (root / "skills/alpha/SKILL.md").write_text(value, encoding="utf-8")


def _inbox(root: Path, value: str) -> None:
    (root / "inbox").mkdir(exist_ok=True)
    (root / "inbox/idea.md").write_text(value, encoding="utf-8")


def _subagent(root: Path, value: str) -> None:
    (root / "subagents/checker.md").write_text(
        f'---\ndescription: "{value}"\nagents: ["codex"]\n---\nCheck.\n',
        encoding="utf-8",
    )


def _mcp(root: Path, value: str) -> None:
    (root / "mcps/docs.toml").write_text(
        f'transport = "remote"\nurl = "https://{value}.example"\nagents = ["codex"]\n',
        encoding="utf-8",
    )


_PLAIN = {
    "memory:notes/a.md": _memory,
    "project-memory:demo/notes/a.md": _project_memory,
    "skill:alpha": _skill,
    "inbox:idea.md": _inbox,
    "subagent:checker": _subagent,
    "mcp:docs": _mcp,
}


@pytest.mark.parametrize("resource_id", sorted(_PLAIN))
@pytest.mark.parametrize(
    ("source_value", "target_value", "expected"),
    [("s", None, "adopt"), ("s", "s", "keep"), ("s", "t", "conflict")],
)
def test_import_plain_resource_matrix(
    tmp_path: Path,
    resource_id: str,
    source_value: str,
    target_value: str | None,
    expected: str,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    write = _PLAIN[resource_id]
    write(source, source_value)
    if target_value is not None:
        write(target, target_value)
    assert _outcomes(source, target)[resource_id] == expected


def test_import_leaves_target_only_resources_untouched(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for write in _PLAIN.values():
        write(target, "target-only")
    before = _fingerprints(target)
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert _fingerprints(target) == before


# Import: resources compared against the installed template.


def _agent(root: Path, value: object) -> None:
    template = load_template("agents/codex.toml")
    if value is not _TEMPLATE:
        template = template.replace(
            'display_name = "Codex"', f'display_name = "{value}"'
        )
    (root / "agents/codex.toml").write_text(template, encoding="utf-8")


def _config(root: Path, value: object) -> None:
    days = 30 if value is _TEMPLATE else {"s": 90, "t": 40}[value]
    text = load_template("config.toml").replace(
        "stale_days = 30", f"stale_days = {days}"
    )
    (root / "config.toml").write_text(text, encoding="utf-8")


def _global(root: Path, value: object) -> None:
    text = load_template("global/AGENTS.md") if value is _TEMPLATE else f"# {value}\n"
    (root / "global/AGENTS.md").write_text(text, encoding="utf-8")


def _project(root: Path, *, mode: str = "link", instructions: object = _TEMPLATE):
    project = root / "projects/demo"
    project.mkdir(parents=True, exist_ok=True)
    (project / "agent.toml").write_text(
        f'name = "demo"\nsync_mode = "{mode}"\n', encoding="utf-8"
    )
    text = (
        render_project_files(project)[0][1]
        if instructions is _TEMPLATE
        else f"# {instructions}\n"
    )
    (project / "AGENTS.md").write_text(text, encoding="utf-8")


def _project_instructions(root: Path, value: object) -> None:
    _project(root, instructions=value)


def _sync_mode(root: Path, value: object) -> None:
    _project(
        root, mode="link" if value is _TEMPLATE else {"s": "copy", "t": "manual"}[value]
    )


_TEMPLATED: dict[str, Callable[[Path, object], None]] = {
    "agent:codex": _agent,
    "config:memory.stale_days": _config,
    "global-instructions:AGENTS.md": _global,
    "project-instructions:demo": _project_instructions,
    "project-field:demo/sync_mode": _sync_mode,
}


@pytest.mark.parametrize("resource_id", sorted(_TEMPLATED))
@pytest.mark.parametrize(
    ("source_value", "target_value"),
    [
        (_TEMPLATE, _TEMPLATE),
        ("s", _TEMPLATE),
        (_TEMPLATE, "t"),
        ("s", "t"),
        ("s", "s"),
    ],
    ids=["both-template", "source-custom", "target-custom", "both-custom", "same"],
)
def test_import_template_baseline_matrix(
    tmp_path: Path, resource_id: str, source_value: object, target_value: object
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    write = _TEMPLATED[resource_id]
    write(source, source_value)
    write(target, target_value)
    if source_value == target_value:
        expected = "keep"
    elif target_value is _TEMPLATE:
        expected = "adopt"
    elif source_value is _TEMPLATE:
        expected = "keep"
    else:
        expected = "conflict"
    assert _outcomes(source, target)[resource_id] == expected


# Import: end-to-end contents.


def _populate(root: Path, label: str) -> None:
    _memory(root, label)
    _skill(root, label)
    _inbox(root, label)
    _subagent(root, label)
    _mcp(root, label)
    _agent(root, label)
    _global(root, label)
    (root / "skills.toml").write_text('skills = ["alpha"]\n', encoding="utf-8")
    project = root / "projects/demo"
    (project / "memory/notes").mkdir(parents=True)
    (project / "agent.toml").write_text(
        f'name = "demo"\npath = "~/{label}"\nsync_mode = "copy"\nskills = ["alpha"]\n',
        encoding="utf-8",
    )
    (project / "AGENTS.md").write_text(f"# {label}\n", encoding="utf-8")
    (project / "memory/notes/d.md").write_text(label, encoding="utf-8")


def test_import_into_fresh_workspace_reproduces_every_source_resource(
    tmp_path: Path,
) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    _populate(source, "s")
    outcomes = _outcomes(source, target)
    assert "conflict" not in outcomes.values()
    assert {kind.split(":", 1)[0] for kind in outcomes} == {
        "agent",
        "config",
        "global-instructions",
        "inbox",
        "mcp",
        "memory",
        "project",
        "project-field",
        "project-instructions",
        "project-memory",
        "project-path",
        "project-skill",
        "skill",
        "skill-selection",
        "subagent",
    }
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    assert _fingerprints(target) == _fingerprints(source)
    assert set(_outcomes(source, target).values()) == {"keep"}


def test_import_merges_collections_and_keeps_target_members(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    for root, name in ((source, "from-source"), (target, "at-target")):
        (root / "skills" / name).mkdir()
        (root / "skills" / name / "SKILL.md").write_text(name, encoding="utf-8")
        (root / "skills.toml").write_text(f'skills = ["{name}"]\n', encoding="utf-8")
        project = root / "projects/demo"
        project.mkdir()
        (project / "agent.toml").write_text(
            f'name = "demo"\npath = "~/{name}"\nskills = ["{name}"]\n',
            encoding="utf-8",
        )
    run_workspace_import(source, target, tmp_path / "home", dry_run=False)
    resources = snapshot_workspace(target).resources
    for kind, prefix in (
        ("skill-selection", ""),
        ("project-skill", "demo/"),
        ("project-path", "demo/~/"),
    ):
        assert {key for key in resources if key.startswith(f"{kind}:")} == {
            f"{kind}:{prefix}at-target",
            f"{kind}:{prefix}from-source",
        }


def test_import_skips_unmanaged_entries_and_bundled_skills(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    (source / "docs").mkdir()
    plan = build_import_plan(source, target)
    assert "SKIPPED Source docs" in plan.excluded
    assert "SKIPPED Source skills/aikito" in plan.excluded
    assert "SKIPPED Source skills/durable-memory" in plan.excluded


# Reconcile.


def _reconcile_workspace(root: Path) -> Path:
    for directory in ("agents", "subagents", "mcps", "memory/notes", "projects"):
        (root / directory).mkdir(parents=True, exist_ok=True)
    for directory in ("skills", "global"):
        (root / directory).mkdir(exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def _pair(tmp_path: Path) -> tuple[Path, Path, Path]:
    left = _reconcile_workspace(tmp_path / "left")
    right = _reconcile_workspace(tmp_path / "right")
    home = tmp_path / "home"
    baseline_workspaces(left, right, home)
    return left, right, home


def _decisions(left: Path, right: Path) -> dict[str, tuple[str, str | None]]:
    return {
        item.path: (item.action, item.target)
        for item in build_reconcile_plan(left, right).items
    }


@pytest.mark.parametrize("deleted_on", ["left", "right"])
def test_reconcile_delete_against_modify_conflicts(
    tmp_path: Path, deleted_on: str
) -> None:
    left, right, home = _pair(tmp_path)
    note = Path("memory/notes/a.md")
    (left / note).write_text("base", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    deleted, modified = (left, right) if deleted_on == "left" else (right, left)
    (deleted / note).unlink()
    (modified / note).write_text("modified", encoding="utf-8")
    plan = run_reconciliation(left, right, home, dry_run=False)
    assert [item.path for item in plan.conflicts] == [note.as_posix()]
    assert not (deleted / note).exists()
    assert (modified / note).read_text(encoding="utf-8") == "modified"


def test_reconcile_concurrent_creates_with_different_content_conflict(
    tmp_path: Path,
) -> None:
    left, right, home = _pair(tmp_path)
    _skill(left, "left")
    _skill(right, "right")
    assert _decisions(left, right) == {"skills/alpha": ("CONFLICT", None)}


def test_reconcile_conflict_blocks_unrelated_changes(tmp_path: Path) -> None:
    left, right, home = _pair(tmp_path)
    (left / "memory/notes/a.md").write_text("left", encoding="utf-8")
    (right / "memory/notes/a.md").write_text("right", encoding="utf-8")
    (left / "memory/notes/b.md").write_text("only left", encoding="utf-8")
    run_reconciliation(left, right, home, dry_run=False)
    assert not (right / "memory/notes/b.md").exists()


def test_reconcile_covers_only_memory_and_skills(tmp_path: Path) -> None:
    left = _reconcile_workspace(tmp_path / "left")
    right = _reconcile_workspace(tmp_path / "right")
    for root in (left, right):
        (root / "projects/demo/memory/notes").mkdir(parents=True)
        (root / "projects/demo/agent.toml").write_text(
            'name = "demo"\n', encoding="utf-8"
        )
    home = tmp_path / "home"
    baseline_workspaces(left, right, home)
    (left / "memory/notes/a.md").write_text("a", encoding="utf-8")
    (left / "projects/demo/memory/notes/d.md").write_text("d", encoding="utf-8")
    _skill(left, "a")
    (left / "projects/demo/agent.toml").write_text(
        'name = "demo"\ndescription = "left"\n', encoding="utf-8"
    )
    (left / "global/AGENTS.md").write_text("# Global\n", encoding="utf-8")
    (left / "mcps/docs.toml").write_text('transport = "remote"\n', encoding="utf-8")
    assert set(_decisions(left, right)) == {
        "memory/notes/a.md",
        "projects/demo/memory/notes/d.md",
        "skills/alpha",
    }
    run_reconciliation(left, right, home, dry_run=False)
    assert (right / "projects/demo/memory/notes/d.md").is_file()
    assert not (right / "global/AGENTS.md").exists()
    assert not (right / "mcps/docs.toml").exists()
    assert "left" not in (right / "projects/demo/agent.toml").read_text(
        encoding="utf-8"
    )


def test_reconcile_cannot_create_memory_for_unknown_project(tmp_path: Path) -> None:
    # Known gap: project configuration is not reconciled, so project memory
    # cannot be created where the project directory is missing.
    left, right, home = _pair(tmp_path)
    notes = left / "projects/demo/memory/notes"
    notes.mkdir(parents=True)
    (left / "projects/demo/agent.toml").write_text('name = "demo"\n', encoding="utf-8")
    (notes / "d.md").write_text("d", encoding="utf-8")
    with pytest.raises(WorkspaceReconcileError, match="Unsafe resource parent"):
        run_reconciliation(left, right, home, dry_run=False)
    assert not (right / "projects/demo").exists()


def test_reconcile_rejects_plan_made_stale_by_later_edit(tmp_path: Path) -> None:
    left, right, home = _pair(tmp_path)
    (left / "memory/notes/a.md").write_text("planned", encoding="utf-8")
    plan = build_reconcile_plan(left, right)
    (right / "memory/notes/b.md").write_text("later", encoding="utf-8")
    with pytest.raises(WorkspaceReconcileError, match="changed after planning"):
        apply_reconcile_plan(plan, home)
    assert not (right / "memory/notes/a.md").exists()


def test_reconcile_rejects_unsupported_baseline_version(tmp_path: Path) -> None:
    left, right, _ = _pair(tmp_path)
    for root in (left, right):
        path = root / ".local/state/aikito/workspace-reconcile/baseline.json"
        state = json.loads(path.read_text(encoding="utf-8"))
        state["version"] = 0
        path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")
    with pytest.raises(WorkspaceReconcileError, match="Unsupported baseline format"):
        build_reconcile_plan(left, right)


# Shared three-way comparison and template history.


@pytest.mark.parametrize(
    ("base", "local", "remote", "action", "target"),
    [
        ((), None, None, "NOOP", None),
        ((), "a", "a", "NOOP", None),
        ((), None, "a", "CREATE", "local"),
        ((), "a", None, "CREATE", "remote"),
        ((), "a", "b", "CONFLICT", None),
        (("a",), "a", "b", "UPDATE", "local"),
        (("a",), "b", "a", "UPDATE", "remote"),
        (("a",), "b", "b", "NOOP", None),
        (("a",), "b", "c", "CONFLICT", None),
        (("a",), None, "a", "DELETE", "remote"),
        (("a",), "a", None, "DELETE", "local"),
        (("a",), None, "b", "CONFLICT", None),
        (("a", "old"), "old", "a", "NOOP", None),
        (("a", "old"), "old", "b", "UPDATE", "local"),
    ],
)
def test_three_way_compare(
    base: tuple[str, ...],
    local: str | None,
    remote: str | None,
    action: str,
    target: str | None,
) -> None:
    outcome = compare(frozenset(base), local, remote)
    assert (outcome.action, outcome.target) == (action, target)


def test_template_history_lists_every_current_template(tmp_path: Path) -> None:
    workspace = _workspace(tmp_path / "workspace")
    _project(workspace)
    for key, resource in snapshot_workspace(workspace).resources.items():
        if key.split(":", 1)[0] in ("agent", "config", "global-instructions"):
            assert resource.fingerprint in template_fingerprints(key), key
        if key in ("project-instructions:demo", "project-field:demo/sync_mode"):
            assert resource.fingerprint in template_fingerprints(key), key


def test_unmodified_older_template_adopts_source(tmp_path: Path) -> None:
    source = _workspace(tmp_path / "source")
    target = _workspace(tmp_path / "target")
    older = load_template("agents/codex.toml").replace(
        'builtin_mcps = ["openaiDeveloperDocs"]\n', ""
    )
    (target / "agents/codex.toml").write_text(older, encoding="utf-8")
    _agent(source, "s")
    assert _outcomes(source, target)["agent:codex"] == "adopt"
    _agent(source, _TEMPLATE)
    assert _outcomes(source, target)["agent:codex"] == "keep"
    (source / "agents/codex.toml").write_text(older, encoding="utf-8")
    _agent(target, _TEMPLATE)
    assert _outcomes(source, target)["agent:codex"] == "keep"
