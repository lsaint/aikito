"""Reconciliation of standalone payloads and shared TOML logical resources."""

from __future__ import annotations

import json
import shutil
import tomllib
from pathlib import Path

import pytest

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.resource_state import REPLICA_STATE
from workspace_reconcile_backend import BACKEND_FACTORIES
from aikito.workspace.resources import snapshot_workspace


def workspace(root: Path) -> Path:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects",
        "skills",
        "global",
        "inbox",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def pair(tmp_path, remote):
    a, b = workspace(tmp_path / "a"), workspace(tmp_path / "b")
    home = tmp_path / "home"
    for local in (a, b):
        round_trip(local, remote, home)
    return a, b, remote, home


@pytest.fixture(params=("memory",))
def replicas(request, tmp_path):
    backend = BACKEND_FACTORIES[request.param](tmp_path / "center")
    return pair(tmp_path, backend.remote)


@pytest.fixture(params=tuple(BACKEND_FACTORIES))
def dual_backend_replicas(request, tmp_path):
    backend = BACKEND_FACTORIES[request.param](tmp_path / "center")
    return pair(tmp_path, backend.remote)


def store_content(remote):
    snapshot = remote.read()
    return snapshot, remote.fetch(snapshot, snapshot.resources)


def round_trip(local, remote, home, **kwargs):
    plan = run_reconciliation(local, remote, home, dry_run=False, **kwargs)
    assert not plan.blocked
    return plan


@pytest.mark.parametrize(
    "relative,identity,first,second,third",
    (
        (
            "config.toml",
            "config:memory.stale_days",
            "[memory]\nstale_days = 10\n",
            "[memory]\nstale_days = 20\n",
            "[memory]\nstale_days = 30\n",
        ),
        (
            "projects/demo/agent.toml",
            "project-field:demo/name",
            'name = "one"\n',
            'name = "two"\n',
            'name = "three"\n',
        ),
    ),
)
def test_shared_same_field_conflict_keeps_base_until_resolution(
    tmp_path, dual_backend_replicas, relative, identity, first, second, third
):
    a, b, remote, home = dual_backend_replicas
    write(a, relative, first)
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    base = json.loads((a / REPLICA_STATE).read_text())["base"][identity]
    write(a, relative, second)
    write(b, relative, third)
    round_trip(b, remote, home)
    plan = round_trip(a, remote, home)
    assert identity in {i.id for i in plan.conflicts}
    assert json.loads((a / REPLICA_STATE).read_text())["base"][identity] == base
    round_trip(a, remote, home, resolutions={identity: "local"})
    round_trip(b, remote, home)
    assert tomllib.loads((b / relative).read_text()) == tomllib.loads(second)


def test_toml_scalar_and_table_merge_is_preview_conflict(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "config.toml", 'feature = "scalar"\n')
    write(b, "config.toml", "[feature]\nflag = true\n[update]\ncheck = false\n")
    round_trip(b, remote, home)
    plan = round_trip(a, remote, home)
    assert {"config:feature", "config:feature.flag"} <= {i.id for i in plan.conflicts}
    assert tomllib.loads((a / "config.toml").read_text()) == {
        "feature": "scalar",
        "update": {"check": False},
    }
    assert "config:feature" not in remote.read().resources


def test_nonfinite_toml_values_do_not_make_plans_stale(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "config.toml", "special = nan\nlimit = inf\n")
    round_trip(a, remote, home)
    plan = build_reconcile_plan(b, remote)
    apply_reconcile_plan(plan, home, remote=remote)
    assert not build_reconcile_plan(b, remote).changes


def test_outside_inbox_absence_cannot_delete_center_notes(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "inbox/a.md", "note")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(
        b, "config.toml", f"[inbox]\npath = {json.dumps(str(tmp_path / 'outside'))}\n"
    )
    plan = round_trip(b, remote, home)
    assert next(i for i in plan.items if i.id == "inbox:a.md").action == "BLOCKED"
    assert store_content(remote)[1]["inbox:a.md"].data == b"note"


def test_date_and_same_text_are_distinct_shared_values(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "config.toml", "start = 2026-09-28\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(b, "config.toml", 'start = "2026-09-28"\n')
    plan = round_trip(b, remote, home)
    assert next(i for i in plan.changes if i.id == "config:start").action == "UPDATE"
    round_trip(a, remote, home)
    assert tomllib.loads((a / "config.toml").read_text())["start"] == "2026-09-28"


def deletion_reference_race(replicas):
    a, b, remote, home = replicas
    write(a, "skills/x/SKILL.md", "# X\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    shutil.rmtree(a / "skills/x")
    round_trip(a, remote, home)
    write(b, "projects/demo/agent.toml", 'name = "demo"\nskills = ["x", "aikito"]\n')
    plan = round_trip(b, remote, home)
    assert {i.id for i in plan.conflicts} == {"skill:x", "project-skill:demo/x"}
    return a, b, remote, home


@pytest.mark.parametrize("identity", ("skill:x", "project-skill:demo/x"))
@pytest.mark.parametrize("side", ("local", "remote"))
def test_reference_conflicts_accept_each_resolution_and_converge(
    tmp_path, replicas, identity, side
):
    a, b, remote, home = deletion_reference_race(replicas)
    before = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    plan = build_reconcile_plan(b, remote, resolutions={identity: side})
    assert not plan.conflicts and not plan.blocked
    assert before == {
        p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()
    }
    apply_reconcile_plan(plan, home, remote=remote)
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    for local in (a, b):
        assert (local / "skills/x/SKILL.md").exists() == (side == "local")
        skills = tomllib.loads((local / "projects/demo/agent.toml").read_text())[
            "skills"
        ]
        assert set(skills) == ({"aikito", "x"} if side == "local" else {"aikito"})
        assert not build_reconcile_plan(local, remote).changes
        assert not build_reconcile_plan(local, remote).conflicts
    assert ("skill:x" in remote.read().resources) == (side == "local")
    assert ("project-skill:demo/x" in remote.read().resources) == (side == "local")
    assert ("skill:x" in json.loads((b / REPLICA_STATE).read_text())["base"]) == (
        side == "local"
    )


@pytest.mark.parametrize("side", ("local", "remote"))
def test_consistent_multiple_reference_choices(tmp_path, replicas, side):
    a, b, remote, home = deletion_reference_race(replicas)
    plan = round_trip(
        b, remote, home, resolutions={"skill:x": side, "project-skill:demo/x": side}
    )
    assert not plan.conflicts
    round_trip(a, remote, home)
    assert (a / "skills/x").exists() == (side == "local")


def test_contradictory_reference_choices_do_not_override_each_other(tmp_path, replicas):
    a, b, remote, home = deletion_reference_race(replicas)
    plan = round_trip(
        b,
        remote,
        home,
        resolutions={"skill:x": "remote", "project-skill:demo/x": "local"},
    )
    assert {i.id for i in plan.conflicts} == {"skill:x", "project-skill:demo/x"}
    assert (b / "skills/x/SKILL.md").is_file()
    assert "project-skill:demo/x" not in remote.read().resources


@pytest.mark.parametrize("action", ("CREATE", "UPDATE", "DELETE", "NOOP", "BLOCKED"))
@pytest.mark.parametrize("side", ("local", "remote"))
def test_nonconflicting_resolution_is_rejected_without_writes(
    tmp_path, replicas, action, side
):
    a, b, remote, home = replicas
    if action in {"UPDATE", "DELETE", "NOOP"}:
        write(a, "memory/notes/n.md", "base")
        round_trip(a, remote, home)
        if action == "UPDATE":
            write(a, "memory/notes/n.md", "changed")
        elif action == "DELETE":
            (a / "memory/notes/n.md").unlink()
    else:
        write(
            a,
            "memory/notes/n.md",
            'api_key = "abcdefghijklmnop123456"' if action == "BLOCKED" else "new",
        )
    plan = build_reconcile_plan(a, remote)
    assert next(i for i in plan.items if i.id == "memory:notes/n.md").action == action
    before = (
        {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()},
        store_content(remote),
    )
    with pytest.raises(
        WorkspaceReconcileError, match="requires a conflicting resource"
    ):
        run_reconciliation(
            a, remote, home, dry_run=False, resolutions={"memory:notes/n.md": side}
        )
    assert before == (
        {p.relative_to(a): p.read_bytes() for p in a.rglob("*") if p.is_file()},
        store_content(remote),
    )


def test_resolution_inferred_provider_still_checks_credentials(tmp_path, replicas):
    a, b, remote, home = deletion_reference_race(replicas)
    write(b, "skills/x/SKILL.md", 'api_key = "abcdefghijklmnop123456"')
    plan = round_trip(b, remote, home, resolutions={"project-skill:demo/x": "local"})
    assert next(i for i in plan.items if i.id == "skill:x").action == "BLOCKED"
    assert (
        next(i for i in plan.items if i.id == "project-skill:demo/x").action
        == "CONFLICT"
    )
    assert "skill:x" not in remote.read().resources
    assert "project-skill:demo/x" not in remote.read().resources


@pytest.mark.parametrize("identity", ("skill:x", "skill-selection:x"))
@pytest.mark.parametrize("side", ("local", "remote"))
def test_root_selection_reference_choices_converge(tmp_path, replicas, identity, side):
    a, b, remote, home = replicas
    write(a, "skills/x/SKILL.md", "# X\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    shutil.rmtree(a / "skills/x")
    round_trip(a, remote, home)
    write(b, "skills.toml", 'skills = ["x", "aikito"]\n')
    assert {i.id for i in round_trip(b, remote, home).conflicts} == {
        "skill:x",
        "skill-selection:x",
    }
    assert not round_trip(b, remote, home, resolutions={identity: side}).conflicts
    for local in (a, b):
        assert not round_trip(local, remote, home).conflicts
        assert (local / "skills/x/SKILL.md").exists() == (side == "local")
        assert set(tomllib.loads((local / "skills.toml").read_text())["skills"]) == (
            {"aikito", "x"} if side == "local" else {"aikito"}
        )
        assert not build_reconcile_plan(local, remote).changes


def test_reference_resolution_preview_is_stale_after_provider_edit(tmp_path, replicas):
    a, b, remote, home = deletion_reference_race(replicas)
    plan = build_reconcile_plan(
        b, remote, resolutions={"project-skill:demo/x": "local"}
    )
    write(b, "skills/x/SKILL.md", "# Edited after preview\n")
    revision = remote.read().revision
    with pytest.raises(WorkspaceReconcileError, match="changed after planning"):
        apply_reconcile_plan(plan, home, remote=remote)
    assert remote.read().revision == revision
    assert "skill:x" not in remote.read().resources
    assert (b / "skills/x/SKILL.md").read_text() == "# Edited after preview\n"


def test_provider_deletion_preserves_standalone_dependents_until_explicit_choice(
    tmp_path,
    dual_backend_replicas,
):
    a, b, remote, home = dual_backend_replicas
    write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    shutil.rmtree(a / "projects/demo")
    round_trip(a, remote, home)
    write(b, "projects/demo/AGENTS.md", "# Keep these instructions\n")
    unresolved = round_trip(b, remote, home)
    assert {i.id for i in unresolved.conflicts} == {
        "project:demo",
        "project-instructions:demo",
    }
    plan = round_trip(b, remote, home, resolutions={"project:demo": "remote"})
    assert {i.id for i in plan.conflicts} == {
        "project:demo",
        "project-instructions:demo",
    }
    assert (b / "projects/demo/AGENTS.md").read_text() == "# Keep these instructions\n"
    plan = round_trip(
        b,
        remote,
        home,
        resolutions={"project:demo": "remote", "project-instructions:demo": "remote"},
    )
    assert not plan.conflicts
    assert not (b / "projects/demo/agent.toml").exists()
    assert not (b / "projects/demo/AGENTS.md").exists()
    assert "project:demo" not in snapshot_workspace(b).resources
    assert not build_reconcile_plan(b, remote).changes
