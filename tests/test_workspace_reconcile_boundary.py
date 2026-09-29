"""Stage-zero behavior and state compatibility for Remote Store Boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aikito.workspace.reconcile import WorkspaceReconcileError
from aikito.workspace.remote import (
    REMOTE_STATE,
    REPLICA_STATE,
    validate_skill_fingerprint_scheme,
)
from aikito.workspace.resources import SKILL_FINGERPRINT_SCHEME
from aikito.workspace.transactions import WorkspaceCoreError
from workspace_reconcile_backend import FilesystemBackend, files
from workspace_reconcile_boundary_smoke import exercise
from workspace_reconcile_smoke import _workspace


def test_cross_platform_skill_round_trip_and_legacy_refusal(tmp_path: Path):
    exercise(tmp_path)


@pytest.mark.parametrize("target", ["center", "replica"])
def test_legacy_state_without_skills_remains_readable(tmp_path: Path, target: str):
    local = _workspace(tmp_path / "local")
    backend = FilesystemBackend(tmp_path / "center")
    backend.run(local, tmp_path / "home")
    path = (
        backend.remote.root / REMOTE_STATE
        if target == "center"
        else local / REPLICA_STATE
    )
    raw = json.loads(path.read_text())
    raw.pop("skill_fingerprint")
    path.write_text(json.dumps(raw), encoding="utf-8")
    before = files(local), backend.checkpoint()
    backend.plan(local)
    assert before == (files(local), backend.checkpoint())


@pytest.mark.parametrize("scheme", ["future-v99", "", 1, False, {}, []])
def test_unknown_fingerprint_scheme_is_not_silently_accepted(scheme):
    with pytest.raises(
        WorkspaceCoreError, match="Unsupported skill fingerprint scheme"
    ):
        validate_skill_fingerprint_scheme(scheme, {})


def test_new_state_records_portable_scheme(tmp_path: Path):
    local = _workspace(tmp_path / "local")
    backend = FilesystemBackend(tmp_path / "center")
    backend.run(local, tmp_path / "home")
    for path in (local / REPLICA_STATE, backend.remote.root / REMOTE_STATE):
        assert (
            json.loads(path.read_text())["skill_fingerprint"]
            == SKILL_FINGERPRINT_SCHEME
        )


def test_center_change_between_read_and_download_rejects_local_application(
    tmp_path: Path, monkeypatch
):
    """Reject an outdated fetch before staging or any local/Base write."""
    a, b = _workspace(tmp_path / "a"), _workspace(tmp_path / "b")
    backend = FilesystemBackend(tmp_path / "center")
    home = tmp_path / "home"
    backend.run(a, home)
    backend.run(b, home)
    (b / "memory/notes/download.md").write_text("download", encoding="utf-8")
    backend.run(b, home)
    plan = backend.plan(a)
    before = files(a)
    original = backend.remote.fetch

    def fetch(expected, ids):
        # A second accepted upload changes only the center revision after the
        # applying client read it, before it obtains planned download content.
        (b / "memory/notes/concurrent.md").write_text("concurrent", encoding="utf-8")
        monkeypatch.setattr(backend.remote, "fetch", original)
        try:
            backend.run(b, home)
        finally:
            monkeypatch.setattr(backend.remote, "fetch", fetch)
        return original(expected, ids)

    monkeypatch.setattr(backend.remote, "fetch", fetch)
    with pytest.raises(WorkspaceReconcileError, match="revision changed"):
        backend.apply(plan, home)
    assert files(a) == before
    assert backend.revision() == plan.revision + 1
