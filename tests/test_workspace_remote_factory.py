"""Credential resolution, authenticated binding and identity-first operations."""

import traceback
from dataclasses import replace

import pytest
from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from test_workspace_remote_receipts import request

from aikito.workspace.remote_access import RemoteAccessDenied, RemoteBindingError
from aikito.workspace.remote_binding import (
    RemoteAuth,
    load_remote_binding,
)
from aikito.workspace.remote_factory import (
    bind_remote,
    open_bound_remote,
    verify_remote_binding,
    resolve_credential,
)
from aikito.workspace.remote_store import StoreIdentityMismatch
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.reconcile import WorkspaceReconcileError
from aikito.workspace.resource_state import REMOTE_BINDING_STATE, PENDING_COMMIT_STATE

AUTH = RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN")
TOKEN = "binding-test-secret"
ENV = {AUTH.env: TOKEN}


@pytest.mark.parametrize(
    "value", [None, "", "\rsecret", "secret\n", "中文", "a b", "a\x7fb", 123]
)
def test_missing_or_invalid_credential(value):
    with pytest.raises(RemoteBindingError) as caught:
        resolve_credential(AUTH, {} if value is None else {AUTH.env: value})
    assert "secret" not in str(caught.value)


def test_environment_resolution(monkeypatch):
    monkeypatch.setenv(AUTH.env, TOKEN)
    assert resolve_credential(AUTH) == TOKEN
    assert resolve_credential(AUTH, ENV) == TOKEN
    with pytest.raises(RemoteBindingError):
        resolve_credential(AUTH, {})


def test_missing_binding_is_local_error(tmp_path):
    with pytest.raises(RemoteBindingError, match="binding is missing"):
        open_bound_remote(tmp_path, environ=ENV)
    assert list(tmp_path.iterdir()) == []


@pytest.fixture
def bound(tmp_path, monkeypatch):
    from aikito.workspace.encrypted_sync import initialize_encrypted_workspace

    monkeypatch.setenv(AUTH.env, TOKEN)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    local, home = _workspace(tmp_path / "local"), tmp_path / "home"
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        binding = bind_remote(local, server.url, AUTH, home=home, environ=ENV)
        codes = []
        initialize_encrypted_workspace(local, home, codes.append)
        yield local, home, backend, server, binding


def test_bind_factory_auth_and_secret_safe_errors(bound):
    local, home, backend, server, binding = bound
    assert TOKEN not in (local / REMOTE_BINDING_STATE).read_text()
    count = server.request_count
    remote = open_bound_remote(local, home=home, environ=ENV)
    assert server.request_count == count
    assert remote.read().resources == {}
    assert verify_remote_binding(binding, environ=ENV) == backend.read()
    assert all(h["Authorization"] == "Bearer " + TOKEN for h in server.headers)
    wrong = open_bound_remote(local, home=home, environ={AUTH.env: "wrong-secret"})
    with pytest.raises(RemoteAccessDenied) as caught:
        wrong.read()
    assert "wrong-secret" not in "".join(traceback.format_exception(caught.value))
    with pytest.raises(RemoteBindingError, match="already exists"):
        bind_remote(local, server.url, AUTH, home=home, environ=ENV)


def test_failed_bind_persists_nothing(tmp_path):
    with HTTPRemoteServer(InMemoryRemote("a" * 32), token=TOKEN) as server:
        with pytest.raises(RemoteAccessDenied):
            bind_remote(
                tmp_path,
                server.url,
                AUTH,
                home=tmp_path / "home",
                environ={AUTH.env: "wrong"},
            )
        assert load_remote_binding(tmp_path) is None and server.dispatch_count == 0


def test_plaintext_store_is_refused_before_binding(tmp_path):
    from aikito.workspace.remote_format import EncryptedStoreUnsupported

    backend = InMemoryRemote("a" * 32)
    backend.commit(request(backend))
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        with pytest.raises(EncryptedStoreUnsupported):
            bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        assert load_remote_binding(tmp_path) is None
        assert all(
            decode_request(raw).operation == Operation.READ for raw in server.requests
        )


def test_uninitialized_store_cannot_synchronize(tmp_path):
    from aikito.workspace.remote_format import EncryptedStoreUnsupported

    local = _workspace(tmp_path / "local")
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        bind_remote(local, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        with pytest.raises(EncryptedStoreUnsupported):
            open_bound_remote(local, home=tmp_path / "home", environ=ENV).read()
        assert not backend.read().resources
        assert all(
            decode_request(raw).operation == Operation.READ for raw in server.requests
        )


@pytest.mark.parametrize("operation", ["read", "recover"])
def test_changed_store_receives_no_identity_bearing_operations(bound, operation):
    local, home, backend, server, _ = bound
    remote = open_bound_remote(local, home=home, environ=ENV)
    remote.read()
    backend._snapshot = replace(backend.read(), sync_id="b" * 32)
    start = len(server.requests)
    with pytest.raises(StoreIdentityMismatch):
        getattr(remote, operation)()
    assert all(
        decode_request(raw).operation == Operation.READ
        for raw in server.requests[start:]
    )


@pytest.mark.parametrize("fault", ["drop_after_request", "drop_after_handler"])
def test_encrypted_pending_survives_auth_failures_and_recovers(bound, fault):
    local, home, backend, server, _ = bound
    from aikito.remote_service import sync_workspace_remote

    sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    note = local / "memory/notes/private.md"
    note.write_text('password = "test-only-abcdefghijklmnopqrstuv"\n')
    server.arm(fault, operation=Operation.COMMIT)
    with pytest.raises(WorkspaceReconcileError):
        sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    path = local / PENDING_COMMIT_STATE
    original = path.read_bytes()
    assert note.read_bytes() not in original
    revision = backend.read().revision
    for credentials in ({}, {AUTH.env: "wrong"}):
        with pytest.raises(
            (RemoteBindingError, WorkspaceReconcileError, RemoteAccessDenied)
        ):
            sync_workspace_remote(local, home=home, environ=credentials, dry_run=False)
        assert path.read_bytes() == original and backend.read().revision == revision
    sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    assert not path.exists()
    assert all(
        k == "format:encrypted-v1" or k.startswith("e1:")
        for k in backend.read().resources
    )


def test_pending_store_replacement_stops_before_receipt_lookup(bound):
    from aikito.remote_service import sync_workspace_remote
    local, home, backend, server, _ = bound
    sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    (local / "memory/notes/retained.md").write_text("Retained unsent content\n")
    server.arm("drop_after_request", operation=Operation.COMMIT)
    with pytest.raises(WorkspaceReconcileError):
        sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    path = local / PENDING_COMMIT_STATE
    original = path.read_bytes()
    backend._snapshot = replace(backend.read(), sync_id="b" * 32)
    start = len(server.requests)
    with pytest.raises(WorkspaceReconcileError, match="binding identity mismatch"):
        sync_workspace_remote(local, home=home, environ=ENV, dry_run=False)
    assert path.read_bytes() == original
    assert all(decode_request(raw).operation == Operation.READ for raw in server.requests[start:])
