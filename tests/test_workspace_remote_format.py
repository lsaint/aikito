"""Client format guards stay outside the content-blind storage contract."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.payload import ResourceDescriptor
from aikito.workspace.reconcile import WorkspaceReconcileError, build_reconcile_plan
from aikito.workspace.remote_binding import RemoteAuth
from aikito.workspace.remote_factory import bind_remote
from aikito.workspace.remote_format import (
    FORMAT_RESOURCE_ID,
    EncryptedStoreUnsupported,
    require_plaintext_snapshot,
)
from aikito.workspace.remote_store import RemoteSnapshot, RemoteStore


def snapshot(identity=None):
    resources = (
        {}
        if identity is None
        else {identity: ResourceDescriptor("unsupported", "0" * 64, 0)}
    )
    return RemoteSnapshot("a" * 32, 0, resources)


@pytest.mark.parametrize(
    "identity",
    [
        FORMAT_RESOURCE_ID,
        "e1:tag:digest",
        # Future format headers and IDs must fail with the same code.
        "format:encrypted-v2",
        "format:",
        "e2:tag:digest",
        "e10:tag:digest",
    ],
)
def test_guard_does_not_interpret_or_disclose_encrypted_descriptors(identity):
    with pytest.raises(EncryptedStoreUnsupported) as caught:
        require_plaintext_snapshot(snapshot(identity))
    assert caught.value.code == "encrypted_store_unsupported"
    assert identity not in str(caught.value)
    assert "unsupported" not in str(caught.value).split(":", 1)[1]


@pytest.mark.parametrize(
    "identity",
    [
        "global-memory:notes/plain.md",
        "project-memory:demo/notes/plain.md",
        "config:inbox.path",
        "skill:example",
        # Names that merely contain the reserved words stay plaintext.
        "global-memory:format-notes/e1.md",
        "project-memory:demo/e1:x.md",
    ],
)
def test_plaintext_snapshot_is_unchanged(identity):
    require_plaintext_snapshot(snapshot(identity))


@pytest.mark.parametrize("identity", [FORMAT_RESOURCE_ID, "e1:tag:digest"])
def test_planning_stops_before_replica_and_resource_decoding(
    tmp_path, monkeypatch, identity
):
    local = tmp_path / "local"
    local.mkdir()
    (local / "layout.toml").write_text("version = 2\n")
    (local / "skills.toml").write_text("skills = []\n")
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "skills",
        "global",
        "memory",
        "projects",
    ):
        (local / directory).mkdir()
    remote = Mock(spec=RemoteStore)
    remote.read.return_value = snapshot(identity)
    read_state = Mock(side_effect=AssertionError("Replica must not be decoded"))
    monkeypatch.setattr("aikito.workspace.reconcile._read_state", read_state)
    before = (local / "layout.toml").read_bytes()
    with pytest.raises(WorkspaceReconcileError, match="encrypted_store_unsupported"):
        build_reconcile_plan(local, remote)
    read_state.assert_not_called()
    remote.fetch.assert_not_called()
    remote.commit.assert_not_called()
    assert not (local / ".local").exists()
    assert (local / "layout.toml").read_bytes() == before


def test_binding_refuses_format_before_persisting(tmp_path, monkeypatch):
    remote = Mock(spec=RemoteStore)
    remote.read.return_value = snapshot("global-memory:notes/plain.md")
    monkeypatch.setattr(
        "aikito.workspace.remote_factory._open_remote", lambda *args, **kwargs: remote
    )
    save = Mock()
    monkeypatch.setattr("aikito.workspace.remote_factory.save_remote_binding", save)
    with pytest.raises(EncryptedStoreUnsupported):
        bind_remote(
            tmp_path / "local",
            "https://example.invalid/remote",
            RemoteAuth("bearer_env", "TOKEN"),
            home=tmp_path / "home",
        )
    save.assert_not_called()


def test_pending_retry_retains_original_request_when_format_changed(
    tmp_path, monkeypatch
):
    request = SimpleNamespace(
        expected=snapshot(),
        client_id="client",
        request_id="request",
        mutation_digest="0" * 64,
    )
    pending = SimpleNamespace(request=request, completed_by=lambda state: False)
    store = Mock()
    store.load.return_value = pending
    monkeypatch.setattr(
        "aikito.workspace.commit_recovery.PendingCommitStore", lambda *args: store
    )
    monkeypatch.setattr(
        "aikito.workspace.commit_recovery.load_replica_state", lambda *args: (None, "")
    )
    remote = Mock(spec=RemoteStore)
    remote.resolve_commit.return_value = None
    remote.read.return_value = snapshot(FORMAT_RESOURCE_ID)
    with pytest.raises(EncryptedStoreUnsupported):
        recover_pending(tmp_path / "local", remote, tmp_path / "home")
    remote.commit.assert_not_called()
    store.clear.assert_not_called()
    store.complete.assert_not_called()


def test_locally_completed_pending_is_cleared_without_remote_access(
    tmp_path, monkeypatch
):
    store = Mock()
    pending = SimpleNamespace(completed_by=lambda state: True)
    store.load.return_value = pending
    monkeypatch.setattr(
        "aikito.workspace.commit_recovery.PendingCommitStore", lambda *args: store
    )
    monkeypatch.setattr(
        "aikito.workspace.commit_recovery.load_replica_state", lambda *args: (None, "")
    )
    remote = Mock(spec=RemoteStore)
    remote.read.side_effect = AssertionError("Completed pending must clear offline")
    assert recover_pending(tmp_path / "local", remote, tmp_path / "home")
    store.clear.assert_called_once_with(pending)
    remote.read.assert_not_called()
    remote.commit.assert_not_called()
    remote.resolve_commit.assert_not_called()
