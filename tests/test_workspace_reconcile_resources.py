"""Reconciliation of standalone payloads and shared TOML logical resources."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

import aikito.workspace.payload_io as payload_io
import aikito.workspace.resources as resources
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.resource_state import REPLICA_STATE
from workspace_reconcile_backend import BACKEND_FACTORIES
from aikito.workspace.resources import snapshot_workspace
from aikito.workspace.skill_metadata import (
    read_executable_metadata,
    write_executable_metadata,
)


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


@pytest.mark.parametrize("backend", tuple(BACKEND_FACTORIES))
def test_windows_deleted_executable_does_not_block_and_cleans_on_download(
    tmp_path, monkeypatch, backend
):
    monkeypatch.setattr(resources, "is_windows", lambda: True)
    monkeypatch.setattr(payload_io, "is_windows", lambda: True)
    a, b = workspace(tmp_path / "a"), workspace(tmp_path / "b")
    remote, home = (
        BACKEND_FACTORIES[backend](tmp_path / "center").remote,
        tmp_path / "home",
    )
    write(a, "skills/tool/SKILL.md", "# Tool\n")
    write(a, "skills/tool/bin/run.sh", "echo one\n")
    metadata = a / "skills/tool/.aikito-executable.json"
    write_executable_metadata(metadata, ["bin/run.sh"])
    round_trip(a, remote, home)
    round_trip(b, remote, home)

    (a / "skills/tool/bin/run.sh").unlink()
    plan = build_reconcile_plan(a, remote)
    assert not plan.blocked
    assert [(item.id, item.target) for item in plan.changes] == [
        ("skill:tool", "remote")
    ]
    round_trip(a, remote, home)
    assert read_executable_metadata(metadata) == {"bin/run.sh"}
    round_trip(b, remote, home)
    assert not (b / "skills/tool/bin/run.sh").exists()
    assert not read_executable_metadata(b / "skills/tool/.aikito-executable.json")

    write(b, "skills/tool/SKILL.md", "# Tool updated\n")
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    assert not read_executable_metadata(metadata)


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics")
def test_skill_executable_only_change_syncs_and_survives_content_upload(replicas):
    a, b, remote, home = replicas
    write(a, "skills/tool/SKILL.md", "# Tool\n")
    write(a, "skills/tool/run.sh", "echo one\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    script = a / "skills/tool/run.sh"
    script.chmod(script.stat().st_mode | 0o111)
    assert [
        (item.id, item.target) for item in build_reconcile_plan(a, remote).changes
    ] == [("skill:tool", "remote")]
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert (b / "skills/tool/run.sh").stat().st_mode & 0o111
    write(b, "skills/tool/run.sh", "echo two\n")
    round_trip(b, remote, home)
    round_trip(a, remote, home)
    assert script.stat().st_mode & 0o111


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics")
def test_old_skill_base_uses_current_mode_or_conflicts(replicas):
    a, b, remote, home = replicas
    write(a, "skills/tool/SKILL.md", "# Tool\n")
    write(a, "skills/tool/run.sh", "echo one\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)

    def strip_mode(local):
        path = local / REPLICA_STATE
        raw = json.loads(path.read_text())
        raw["base"]["skill:tool"].pop("mode_fingerprint")
        path.write_text(json.dumps(raw), encoding="utf-8")

    strip_mode(a)
    assert not build_reconcile_plan(a, remote).changes
    round_trip(a, remote, home)
    assert (
        "mode_fingerprint"
        in json.loads((a / REPLICA_STATE).read_text())["base"]["skill:tool"]
    )

    strip_mode(b)
    script = a / "skills/tool/run.sh"
    script.chmod(script.stat().st_mode | 0o111)
    round_trip(a, remote, home)
    assert [item.id for item in build_reconcile_plan(b, remote).conflicts] == [
        "skill:tool"
    ]
    round_trip(b, remote, home, resolutions={"skill:tool": "remote"})
    assert (b / "skills/tool/run.sh").stat().st_mode & 0o111


@pytest.mark.skipif(os.name == "nt", reason="POSIX chmod semantics")
def test_skill_mode_and_remote_content_edits_conflict(replicas):
    a, b, remote, home = replicas
    write(a, "skills/tool/SKILL.md", "# Tool\n")
    write(a, "skills/tool/run.sh", "echo one\n")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    script = a / "skills/tool/run.sh"
    script.chmod(script.stat().st_mode | 0o111)
    write(b, "skills/tool/run.sh", "echo two\n")
    round_trip(b, remote, home)
    assert [item.id for item in build_reconcile_plan(a, remote).conflicts] == [
        "skill:tool"
    ]
    round_trip(a, remote, home, resolutions={"skill:tool": "local"})
    round_trip(b, remote, home)
    assert (b / "skills/tool/run.sh").stat().st_mode & 0o111
    assert (b / "skills/tool/run.sh").read_text() == "echo one\n"


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
    tmp_path, replicas, identity, relative, first, second
):
    a, b, remote, home = replicas
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


def test_shared_fields_memberships_and_project_delete(tmp_path, dual_backend_replicas):
    a, b, remote, home = dual_backend_replicas
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


def test_inbox_uses_each_replica_prefix_and_outside_is_blocked(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "config.toml", '[inbox]\npath = "capture-a"\n')
    write(b, "config.toml", '[inbox]\npath = "capture-b"\n')
    write(a, "capture-a/deep/a.md", "note")
    round_trip(a, remote, home)
    round_trip(b, remote, home)
    assert (b / "capture-b/deep/a.md").read_text() == "note"
    assert store_content(remote)[1]["inbox:deep/a.md"].data == b"note"
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


def test_shared_independent_changes_and_partial_credential_block(tmp_path, replicas):
    a, b, remote, home = replicas
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
    assert "config:service.api_key" not in store_content(remote)[1]


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
    tmp_path, replicas, consumer, relative, content
):
    a, b, remote, home = replicas
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
    assert "agent:custom" in remote.read().resources


def test_project_deletion_cannot_orphan_instructions_or_notes(tmp_path, replicas):
    a, b, remote, home = replicas
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


def test_shared_multiple_changes_commit_once_and_recover(
    tmp_path, dual_backend_replicas
):
    a, b, remote, home = dual_backend_replicas
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


def test_typed_and_literal_keys_survive_shared_rendering(tmp_path, replicas):
    a, b, remote, home = replicas
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


def test_plan_rejects_ambiguous_config_ids(tmp_path, replicas):
    a, b, remote, home = replicas
    write(a, "config.toml", '"feature.flag" = true\n[feature]\nflag = false\n')
    plan = build_reconcile_plan(a, remote)
    assert plan.blocked
    before = store_content(remote)
    with pytest.raises(WorkspaceReconcileError, match="blocking findings"):
        apply_reconcile_plan(plan, home, remote=remote)
    assert store_content(remote) == before


@pytest.mark.parametrize("identity,relative,first,second", STANDALONE)
def test_standalone_interruption_restores_content_and_base(
    tmp_path, replicas, identity, relative, first, second
):
    a, b, remote, home = replicas
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
