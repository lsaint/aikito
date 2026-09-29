"""State revision validation must preserve guards and reject malformed records."""

from __future__ import annotations

import json

import pytest

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.resource_state import REMOTE_STATE, REPLICA_STATE
from workspace_reconcile_smoke import _workspace
from workspace_revision_smoke import exercise


def test_revision_state_round_trip(tmp_path):
    exercise(tmp_path)


@pytest.mark.parametrize("target", ["center", "replica"])
@pytest.mark.parametrize(
    "fields",
    [
        {},
        {"revision": True},
        {"revision": -1},
        {"revision": "0"},
        {"revision": None},
    ],
)
def test_invalid_state_revision_is_rejected_without_writes(tmp_path, target, fields):
    local = _workspace(tmp_path / "local")
    remote = FilesystemRemote.create(tmp_path / "center")
    run_reconciliation(local, remote, tmp_path / "home", dry_run=False)
    path = remote.root / REMOTE_STATE if target == "center" else local / REPLICA_STATE
    state = json.loads(path.read_text(encoding="utf-8"))
    state.pop("revision")
    state.update(fields)
    path.write_text(json.dumps(state), encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(WorkspaceReconcileError, match="revision"):
        build_reconcile_plan(local, remote)
    assert path.read_bytes() == before
