"""Strict private binding state and safe lifecycle writes."""

import json
import os

import pytest
from test_workspace_remote_receipt_crashes import SimulatedCrash
from test_workspace_remote_receipts import request
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace

from aikito.workspace import transactions
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.remote_access import RemoteBindingError
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
    save_remote_binding,
    remove_remote_binding,
)
from aikito.workspace.remote_store import StoreIdentityMismatch
from aikito.workspace.replica_state import ReplicaState
from aikito.workspace.resource_state import (
    REMOTE_BINDING_STATE,
    RECONCILE_POLICY,
    REPLICA_POLICY,
    REPLICA_STATE,
    state_path,
)
from aikito.workspace.transactions import WorkspaceCoreError, atomic_text


def binding():
    return RemoteBinding(
        1,
        "https://example.invalid/v1/remote",
        "a" * 32,
        RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN"),
    )


def test_round_trip_private_state_and_removal(tmp_path):
    local, home = tmp_path / "local", tmp_path / "home"
    local.mkdir()
    save_remote_binding(local, binding(), home=home)
    path = local / REMOTE_BINDING_STATE
    assert load_remote_binding(local) == binding()
    assert "secret-value" not in path.read_text()
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700
    assert REMOTE_BINDING_STATE not in RECONCILE_POLICY.states
    assert REMOTE_BINDING_STATE not in REPLICA_POLICY.states
    with pytest.raises(RemoteBindingError, match="already exists"):
        save_remote_binding(local, binding(), home=home)
    remove_remote_binding(local, home=home)
    assert load_remote_binding(local) is None
    remove_remote_binding(local, home=home)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", 2),
        ("version", True),
        ("version", "1"),
        ("sync_id", "center"),
        ("sync_id", "A" * 32),
        ("endpoint", "http://example.invalid/remote"),
        ("endpoint", "https://user:secret@example.invalid/remote"),
        ("endpoint", "https://example.invalid/?access%5Ftoken=secret"),
        ("endpoint", "https://example.invalid/?API-Key=secret"),
        ("endpoint", "https://example.invalid/#secret"),
        ("endpoint", "http://127.0.0.1.evil.invalid/remote"),
        ("auth", {"type": "other", "env": "TOKEN"}),
        ("auth", {"type": "bearer_env", "env": "token"}),
        ("auth", {"type": "bearer_env", "env": "1TOKEN"}),
        ("auth", {"type": "bearer_env", "env": "TOKEN", "value": "secret"}),
        ("unknown", "secret"),
    ],
)
def test_invalid_binding_fields(field, value):
    raw = json.loads(binding().encode())
    raw[field] = value
    with pytest.raises(RemoteBindingError):
        RemoteBinding.decode(json.dumps(raw))


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1:1234/remote",
        "http://[::1]/remote",
        "http://localhost/remote",
        "https://example.invalid/remote?route=abc",
    ],
)
def test_valid_endpoints(endpoint):
    raw = json.loads(binding().encode())
    raw["endpoint"] = endpoint
    assert RemoteBinding.decode(json.dumps(raw)).endpoint == endpoint


@pytest.mark.parametrize(
    "text",
    [
        b"{secret",
        b"\xff",
        b"{}",
        b"[]",
        b'{"version":1,"version":1}',
        b'{"version":NaN}',
    ],
)
def test_malformed_state_retained(tmp_path, text):
    path = state_path(tmp_path, REMOTE_BINDING_STATE, create=True)
    path.write_bytes(text)
    with pytest.raises(RemoteBindingError):
        load_remote_binding(tmp_path)
    with pytest.raises(RemoteBindingError):
        save_remote_binding(tmp_path, binding(), home=tmp_path / "home")
    with pytest.raises(RemoteBindingError):
        remove_remote_binding(tmp_path, home=tmp_path / "home")
    assert path.read_bytes() == text


def test_missing_load_creates_nothing(tmp_path):
    assert load_remote_binding(tmp_path) is None
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("relative", [".local", REMOTE_BINDING_STATE])
def test_symlink_state_rejected(tmp_path, relative):
    target = tmp_path / "target"
    target.mkdir()
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlink creation unavailable")
    with pytest.raises(WorkspaceCoreError, match="Unsafe"):
        load_remote_binding(tmp_path)
    with pytest.raises(WorkspaceCoreError, match="Unsafe"):
        save_remote_binding(tmp_path, binding(), home=tmp_path / "home")
    assert list(target.iterdir()) == []


def test_existing_replica_identity_prevents_rebind(tmp_path):
    path = state_path(tmp_path, REPLICA_STATE, create=True)
    original = ReplicaState("b" * 32, "c" * 32, 0, {}).encode()
    atomic_text(path, original)
    with pytest.raises(StoreIdentityMismatch):
        save_remote_binding(tmp_path, binding(), home=tmp_path / "home")
    assert path.read_text() == original
    assert load_remote_binding(tmp_path) is None


def test_binding_write_recovers_existing_journal_without_policy_change(
    tmp_path, monkeypatch
):
    local, home = _workspace(tmp_path / "local"), tmp_path / "home"
    pending = PendingCommitStore(local, home)
    commit = request(InMemoryRemote("a" * 32), client="b" * 32)
    original = transactions.atomic_text

    def interrupt(path, text):
        original(path, text)
        if "workspace-transactions" in path.parts and path.name == "pending.json":
            raise SimulatedCrash

    with monkeypatch.context() as patch:
        patch.setattr(transactions, "atomic_text", interrupt)
        with pytest.raises(SimulatedCrash):
            pending.persist(commit, safe_resource_ids=(commit.mutations[0].id,))
    journal = local / ".local/state/aikito/workspace-transactions/pending.json"
    before = journal.read_bytes()
    assert load_remote_binding(local) is None
    assert journal.read_bytes() == before
    save_remote_binding(local, binding(), home=home)
    assert not journal.exists()
    assert pending.load() is None
    assert load_remote_binding(local) == binding()


def test_existing_pending_identity_prevents_binding(tmp_path):
    local, home = _workspace(tmp_path / "local"), tmp_path / "home"
    pending = PendingCommitStore(local, home)
    commit = request(InMemoryRemote("b" * 32), client="c" * 32)
    pending.persist(commit, safe_resource_ids=(commit.mutations[0].id,))
    path = local / ".local/state/aikito/workspace-reconcile/pending.json"
    before = path.read_bytes()
    with pytest.raises(StoreIdentityMismatch):
        save_remote_binding(local, binding(), home=home)
    assert path.read_bytes() == before
    assert load_remote_binding(local) is None
