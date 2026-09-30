"""Filesystem center layout, manifest compatibility, and journal defenses."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.workspace.reconcile import WorkspaceReconcileError, build_reconcile_plan
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE, REPLICA_STATE
from test_workspace_reconcile_resources import pair as replica_pair, round_trip, write


def pair(tmp_path):
    return replica_pair(tmp_path, FilesystemRemote.create(tmp_path / "center"))


def test_shared_resources_are_manifest_values_not_rendered_center_files(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "skills/example/SKILL.md", "# Example\n")
    write(a, "skills.toml", 'skills = ["example"]\n')
    write(a, "config.toml", "[memory]\nstale_days = 12\n")
    write(a, "projects/demo/agent.toml", 'name = "demo"\n')
    round_trip(a, remote, home)
    for relative in ("config.toml", "skills.toml", "projects/demo/agent.toml"):
        assert not (remote.root / relative).exists()
    manifest = json.loads((remote.root / REMOTE_STATE).read_text())
    assert "config:memory.stale_days" in manifest["values"]
    assert "skill-selection:example" in manifest["resources"]
    assert "project:demo" in manifest["resources"]


def test_legacy_center_reads_then_upgrades_on_commit(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(a, "memory/notes/a.md", "note")
    round_trip(a, remote, home)
    state_path = remote.root / REMOTE_STATE
    state = json.loads(state_path.read_text())
    state["version"] = 1
    state.pop("payload_hashes")
    state["resources"] = {
        key: value["fingerprint"] for key, value in state["resources"].items()
    }
    state.pop("values")
    state_path.write_text(json.dumps(state))
    assert remote.read().revision == state["revision"]
    assert json.loads(state_path.read_text())["version"] == 1
    write(a, "config.toml", "[update]\ncheck = false\n")
    round_trip(a, remote, home)
    updated = json.loads(state_path.read_text())
    assert updated["version"] == 2
    assert updated["revision"] == state["revision"] + 1


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


def test_shared_center_manifest_interruption_recovers_values_and_revision(tmp_path):
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
    round_trip(a, remote, home)
    assert remote.read().revision == json.loads(before)["revision"] + 1
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
    state.pop("payload_hashes")
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


def test_center_rejects_unregistered_whole_configuration(tmp_path):
    a, b, remote, home = pair(tmp_path)
    write(remote.root, "config.toml", '[inbox]\npath = "private"\n')
    with pytest.raises(WorkspaceReconcileError, match="Unmanaged center content"):
        build_reconcile_plan(a, remote)
