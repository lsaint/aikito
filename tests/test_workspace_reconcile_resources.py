"""Reconciliation of standalone payloads and shared TOML logical resources."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE, REPLICA_STATE
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
    path.write_text(value, encoding="utf-8")


def pair(tmp_path):
    a, b = workspace(tmp_path / "a"), workspace(tmp_path / "b")
    remote = FilesystemRemote.create(tmp_path / "center")
    home = tmp_path / "home"
    for local in (a, b):
        round_trip(local, remote, home)
    return a, b, remote, home


def round_trip(local, remote, home, **kwargs):
    plan = run_reconciliation(local, remote, home, dry_run=False, **kwargs)
    assert not plan.blocked
    return plan


STANDALONE = (
    ("inbox:deep/note.md", "inbox/deep/note.md", "first", "second"),
    ("global-instructions:AGENTS.md", "global/AGENTS.md", "first", "second"),
    ("project-instructions:demo", "projects/demo/AGENTS.md", "first", "second"),
    (
        "agent:custom",
        "agents/custom.toml",
        '[agents.custom]\nvalue = "first"\n',
        '[agents.custom]\nvalue = "second"\n',
    ),
    ("mcp:docs", "mcps/docs.toml", 'transport = "first"\n', 'transport = "second"\n'),
    (
        "subagent:review",
        "subagents/review.md",
        '---\ndescription: "first"\nagents: ["custom"]\n---\nReview\n',
        '---\ndescription: "second"\nagents: ["custom"]\n---\nReview again\n',
    ),
)


@pytest.mark.parametrize("identity,relative,first,second", STANDALONE)
def test_standalone_create_update_delete_and_conflict(
    tmp_path, identity, relative, first, second
):
    a, b, remote, home = pair(tmp_path)
    if identity.startswith("project-"):
        write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    if identity.startswith("subagent:"):
        write(a, "agents/custom.toml", '[agents.custom]\nvalue = "one"\n')
    write(a, relative, first)
    assert identity in {i.id for i in round_trip(a, remote, home).changes}
    round_trip(b, remote, home)
    assert (b / relative).read_text() == first
    write(b, relative, second)
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    assert (a / relative).read_text() == second
    write(a, relative, first)
    write(b, relative, second + "\n")
    # Whitespace is semantic NOOP for TOML but changes other standalone payloads.
    if identity.startswith(("agent:", "mcp:")):
        write(b, relative, second.replace("second", "third"))
    round_trip(b, remote, home)
    plan = build_reconcile_plan(a, remote)
    assert identity in {i.id for i in plan.conflicts}
    round_trip(a, remote, home, resolutions={identity: "remote"})
    (a / relative).unlink()
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert not (b / relative).exists()
    assert identity not in remote.read().resources
    assert not build_reconcile_plan(b, remote).changes


def test_shared_fields_memberships_and_project_delete(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "skills/example/SKILL.md", "# Example\n")
    write(a, "skills.toml", 'skills = ["example", "aikito"]\n')
    write(
        a,
        "projects/demo/agent.toml",
        'name = "demo"\nsync_mode = "link"\npaths = ["~/offline/a", "/offline/b"]\nskills = ["example", "durable-memory"]\n[memory]\nstale_days = 12\n',
    )
    write(
        a,
        "config.toml",
        '# Local config\n[inbox]\npath = "inbox"\n[memory]\nstale_days = 9\n[update]\ncheck = false\n',
    )
    write(b, "config.toml", '[inbox]\npath = "capture"\n')
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    resources = remote.read().resources
    assert {
        "project:demo",
        "project-field:demo/name",
        "project-field:demo/memory",
        "project-path:demo/~/offline/a",
        "project-path:demo//offline/b",
        "project-skill:demo/example",
        "skill-selection:example",
        "config:memory.stale_days",
        "config:update.check",
    } <= resources.keys()
    assert "config:inbox.path" not in resources
    assert not (remote.root / "config.toml").exists()
    assert not (remote.root / "skills.toml").exists()
    assert not (remote.root / "projects/demo/agent.toml").exists()
    assert tomllib.loads((b / "config.toml").read_text())["inbox"]["path"] == "capture"
    write(
        b,
        "projects/demo/agent.toml",
        'name = "changed"\npaths = ["/offline/c"]\nskills = ["durable-memory"]\n',
    )
    write(b, "skills.toml", 'skills = ["aikito"]\n')
    write(b, "config.toml", '[inbox]\npath = "capture"\n[memory]\nstale_days = 20\n')
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    project = tomllib.loads((a / "projects/demo/agent.toml").read_text())
    assert project == {
        "name": "changed",
        "paths": ["/offline/c"],
        "skills": ["durable-memory"],
    }
    assert tomllib.loads((a / "skills.toml").read_text())["skills"] == ["aikito"]
    assert tomllib.loads((a / "config.toml").read_text()) == {
        "inbox": {"path": "inbox"},
        "memory": {"stale_days": 20},
    }
    shutil.rmtree(b / "projects/demo")
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    assert not (a / "projects/demo/agent.toml").exists()
    assert not any(key.startswith("project") for key in snapshot_workspace(a).resources)


def test_inbox_uses_each_replica_prefix_and_outside_is_blocked(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", '[inbox]\npath = "capture-a"\n')
    write(b, "config.toml", '[inbox]\npath = "capture-b"\n')
    write(a, "capture-a/deep/a.md", "note")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert (b / "capture-b/deep/a.md").read_text() == "note"
    assert (remote.root / "inbox/deep/a.md").read_text() == "note"
    write(
        b, "config.toml", f"[inbox]\npath = {json.dumps(str(tmp_path / 'outside'))}\n"
    )
    plan = build_reconcile_plan(b, remote)
    # Existing base sees local absence as deletion; new incoming notes are blocked.
    write(a, "capture-a/new.md", "new")
    round_trip(a, remote, home)
    plan = build_reconcile_plan(b, remote)
    assert next(i for i in plan.items if i.id == "inbox:new.md").action == "BLOCKED"
    assert not (tmp_path / "outside").exists()


def test_shared_independent_changes_and_partial_credential_block(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "[memory]\nstale_days = 9\n[update]\ncheck = true\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(a, "config.toml", "[memory]\nstale_days = 10\n[update]\ncheck = true\n")
    write(b, "config.toml", "[memory]\nstale_days = 9\n[update]\ncheck = false\n")
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert tomllib.loads((a / "config.toml").read_text()) == {
        "memory": {"stale_days": 10},
        "update": {"check": False},
    }
    write(
        a,
        "config.toml",
        '[memory]\nstale_days = 11\n[service]\napi_key = "abcdefghijklmnop123456"\n',
    )
    plan = round_trip(a, remote, home)
    assert (
        next(i for i in plan.items if i.id == "config:service.api_key").action
        == "BLOCKED"
    )
    assert "config:service.api_key" not in remote.read().resources
    assert (
        "config:service.api_key"
        not in json.loads((a / REPLICA_STATE).read_text())["base"]
    )
    round_trip(b, remote, home)
    assert tomllib.loads((b / "config.toml").read_text()) == {
        "memory": {"stale_days": 11}
    }
    assert "abcdefghijklmnop" not in (remote.root / REMOTE_STATE).read_text()


@pytest.mark.parametrize(
    "consumer,relative,content",
    (
        ("mcp:docs", "mcps/docs.toml", 'agents = ["custom"]\n'),
        (
            "subagent:review",
            "subagents/review.md",
            '---\ndescription: "Review"\nagents: ["custom"]\n---\nReview\n',
        ),
    ),
)
def test_agent_reference_checks_creation_and_deletion(
    tmp_path, consumer, relative, content
):
    a, b, remote, home = pair(tmp_path)
    write(a, relative, content)
    plan = build_reconcile_plan(a, remote)
    assert plan.blocked
    assert consumer in {i.id for i in plan.conflicts}
    assert consumer not in remote.read().resources
    write(a, "agents/custom.toml", '[agents.custom]\nvalue = "one"\n')
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    (a / "agents/custom.toml").unlink()
    plan = build_reconcile_plan(a, remote)
    assert plan.blocked
    assert "agent:custom" in {i.id for i in plan.conflicts}
    assert "invalidate" in next(
        i.reason for i in plan.conflicts if i.id == "agent:custom"
    )
    assert (remote.root / "agents/custom.toml").is_file()


def test_project_deletion_cannot_orphan_instructions_or_notes(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    write(a, "projects/demo/AGENTS.md", "policy")
    write(a, "projects/demo/memory/notes/n.md", "note")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    shutil.rmtree(a / "projects/demo")
    write(b, "projects/demo/AGENTS.md", "changed policy")
    round_trip(b, remote, home)
    plan = round_trip(a, remote, home)
    assert {"project:demo", "project-instructions:demo"} <= {
        i.id for i in plan.conflicts
    }
    assert "project:demo" in remote.read().resources


def test_shared_multiple_changes_commit_once_and_recover(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "[memory]\nstale_days = 10\n[update]\ncheck = true\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(a, "config.toml", "[memory]\nstale_days = 20\n")
    round_trip(a, remote, home)
    before = (b / "config.toml").read_bytes(), (b / REPLICA_STATE).read_bytes()
    original = os.replace
    replacements = []

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == b / "config.toml":
            replacements.append(dst)
            original(src, dst)
            raise KeyboardInterrupt
        original(src, dst)

    with (
        patch("aikito.workspace.transactions.os.replace", side_effect=interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        round_trip(b, remote, home)
    assert len(replacements) == 1
    with pytest.raises(WorkspaceReconcileError, match="Recovered an interrupted"):
        round_trip(b, remote, home)
    assert (
        (b / "config.toml").read_bytes(),
        (b / REPLICA_STATE).read_bytes(),
    ) == before
    round_trip(b, remote, home)
    assert tomllib.loads((b / "config.toml").read_text()) == {
        "memory": {"stale_days": 20}
    }


def test_typed_and_literal_keys_survive_shared_rendering(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(
        a,
        "config.toml",
        '"feature.flag" = true\nstart = 2026-09-28\ntext = """line one\nline two"""\nvalues = [1, "two", { nested = false }]\n',
    )
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert tomllib.loads((a / "config.toml").read_text()) == tomllib.loads(
        (b / "config.toml").read_text()
    )
    write(b, "config.toml", 'start = 2026-09-29\ntext = "changed"\nvalues = []\n')
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    assert tomllib.loads((a / "config.toml").read_text()) == tomllib.loads(
        (b / "config.toml").read_text()
    )


def test_legacy_center_reads_then_upgrades_on_commit(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "memory/notes/a.md", "note")
    round_trip(a, remote, home)
    state_path = remote.root / REMOTE_STATE
    state = json.loads(state_path.read_text())
    state["version"] = 1
    state["resources"] = {
        key: value["fingerprint"] for key, value in state["resources"].items()
    }
    state.pop("values")
    state_path.write_text(json.dumps(state))
    assert remote.read().generation == state["generation"]
    assert json.loads(state_path.read_text())["version"] == 1
    write(a, "config.toml", "[update]\ncheck = false\n")
    round_trip(a, remote, home)
    assert json.loads(state_path.read_text())["version"] == 2


def test_center_rejects_tampered_field_payload_and_references(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "[memory]\nstale_days = 10\n")
    round_trip(a, remote, home)
    path = remote.root / REMOTE_STATE
    state = json.loads(path.read_text())
    state["values"]["config:memory.stale_days"]["toml"] = "value = 99\n"
    path.write_text(json.dumps(state))
    with pytest.raises(WorkspaceReconcileError, match="Invalid center field value"):
        build_reconcile_plan(b, remote)


def test_plan_rejects_ambiguous_config_ids(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", '"feature.flag" = true\n[feature]\nflag = false\n')
    plan = build_reconcile_plan(a, remote)
    assert plan.blocked
    before = (remote.root / REMOTE_STATE).read_bytes()
    with pytest.raises(WorkspaceReconcileError, match="blocking findings"):
        apply_reconcile_plan(plan, home)
    assert (remote.root / REMOTE_STATE).read_bytes() == before


@pytest.mark.parametrize("identity,relative,first,second", STANDALONE)
def test_standalone_interruption_restores_content_and_base(
    tmp_path, identity, relative, first, second
):
    a, b, remote, home = pair(tmp_path)
    if identity.startswith("project-"):
        write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    if identity.startswith("subagent:"):
        write(a, "agents/custom.toml", '[agents.custom]\nvalue = "one"\n')
    write(a, relative, first)
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    before = (b / relative).read_bytes(), (b / REPLICA_STATE).read_bytes()
    write(a, relative, second)
    round_trip(a, remote, home)
    original = os.replace

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == b / relative:
            original(src, dst)
            raise KeyboardInterrupt
        original(src, dst)

    with (
        patch("aikito.workspace.transactions.os.replace", side_effect=interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        round_trip(b, remote, home)
    with pytest.raises(WorkspaceReconcileError, match="Recovered an interrupted"):
        round_trip(b, remote, home)
    assert ((b / relative).read_bytes(), (b / REPLICA_STATE).read_bytes()) == before
    round_trip(b, remote, home)
    assert (b / relative).read_text() == second


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
    tmp_path, relative, identity, first, second, third
):
    a, b, remote, home = pair(tmp_path)
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


def test_shared_center_manifest_interruption_recovers_values_and_generation(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "[memory]\nstale_days = 10\n")
    round_trip(a, remote, home)
    before = (remote.root / REMOTE_STATE).read_bytes()
    state = (a / REPLICA_STATE).read_bytes()
    write(a, "config.toml", "[memory]\nstale_days = 20\n[update]\ncheck = false\n")
    original = os.replace

    def interrupt(src, dst):
        if Path(dst) == remote.root / REMOTE_STATE:
            original(src, dst)
            raise KeyboardInterrupt
        original(src, dst)

    with (
        patch("aikito.workspace.transactions.os.replace", side_effect=interrupt),
        pytest.raises(KeyboardInterrupt),
    ):
        round_trip(a, remote, home)
    assert (a / REPLICA_STATE).read_bytes() == state
    with pytest.raises(WorkspaceReconcileError, match="Recovered an interrupted"):
        round_trip(a, remote, home)
    assert (remote.root / REMOTE_STATE).read_bytes() == before
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert tomllib.loads((b / "config.toml").read_text()) == {
        "memory": {"stale_days": 20},
        "update": {"check": False},
    }


def test_legacy_project_notes_gain_provider_before_manifest_upgrade(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    write(a, "projects/demo/memory/notes/a.md", "note")
    round_trip(a, remote, home)
    path = remote.root / REMOTE_STATE
    state = json.loads(path.read_text())
    state["version"] = 1
    state["resources"] = {
        key: value["fingerprint"]
        for key, value in state["resources"].items()
        if key.startswith("project-memory:")
    }
    state.pop("values")
    path.write_text(json.dumps(state))
    # A phase-4 base knew only the note, so project resources are new uploads.
    local_state = json.loads((a / REPLICA_STATE).read_text())
    local_state["base"] = {
        key: value
        for key, value in local_state["base"].items()
        if key.startswith("project-memory:")
    }
    (a / REPLICA_STATE).write_text(json.dumps(local_state))
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert (b / "projects/demo/agent.toml").is_file()
    assert (b / "projects/demo/memory/notes/a.md").read_text() == "note"


def test_toml_scalar_and_table_merge_is_preview_conflict(tmp_path):
    a, b, remote, home = pair(tmp_path)
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


def test_nonfinite_toml_values_do_not_make_plans_stale(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "special = nan\nlimit = inf\n")
    round_trip(a, remote, home)
    plan = build_reconcile_plan(b, remote)
    apply_reconcile_plan(plan, home)
    assert not build_reconcile_plan(b, remote).changes


def test_outside_inbox_absence_cannot_delete_center_notes(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "inbox/a.md", "note")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(
        b, "config.toml", f"[inbox]\npath = {json.dumps(str(tmp_path / 'outside'))}\n"
    )
    plan = round_trip(b, remote, home)
    assert next(i for i in plan.items if i.id == "inbox:a.md").action == "BLOCKED"
    assert (remote.root / "inbox/a.md").read_text() == "note"


def test_center_rejects_unregistered_whole_configuration(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(remote.root, "config.toml", '[inbox]\npath = "private"\n')
    with pytest.raises(WorkspaceReconcileError, match="Unmanaged center content"):
        build_reconcile_plan(a, remote)


def test_date_and_same_text_are_distinct_shared_values(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "config.toml", "start = 2026-09-28\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    write(b, "config.toml", 'start = "2026-09-28"\n')
    plan = round_trip(b, remote, home)
    assert next(i for i in plan.changes if i.id == "config:start").action == "UPDATE"
    round_trip(a, remote, home)
    assert tomllib.loads((a / "config.toml").read_text())["start"] == "2026-09-28"
