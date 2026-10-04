"""Credential resolution, authenticated binding and identity-first operations."""

import traceback
from dataclasses import replace

import pytest
from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from test_workspace_remote_receipts import request, mutation

from aikito.workspace.remote_access import RemoteAccessDenied, RemoteBindingError
from aikito.workspace.remote_binding import (
    RemoteAuth,
    load_remote_binding,
    remove_remote_binding,
)
from aikito.workspace.remote_factory import (
    bind_remote,
    open_bound_remote,
    verify_remote_binding,
    resolve_credential,
)
from aikito.workspace.remote_store import StoreIdentityMismatch, RecoveryRequired
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import run_reconciliation, WorkspaceReconcileError
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


def test_bind_factory_auth_and_secret_safe_errors(tmp_path):
    home = tmp_path / "home"
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        binding = bind_remote(tmp_path, server.url, AUTH, home=home, environ=ENV)
        assert binding.sync_id == backend.read().sync_id
        assert load_remote_binding(tmp_path) == binding
        assert TOKEN not in (tmp_path / REMOTE_BINDING_STATE).read_text()
        count = server.request_count
        remote = open_bound_remote(tmp_path, environ=ENV)
        assert server.request_count == count
        assert remote.read() == backend.read()
        assert verify_remote_binding(binding, environ=ENV) == backend.read()
        assert all(
            headers["Authorization"] == "Bearer " + TOKEN for headers in server.headers
        )
        assert TOKEN not in repr(remote)
        wrong = open_bound_remote(tmp_path, environ={AUTH.env: "wrong-secret"})
        with pytest.raises(RemoteAccessDenied) as caught:
            wrong.read()
        assert "wrong-secret" not in "".join(traceback.format_exception(caught.value))
        with pytest.raises(RemoteBindingError, match="already exists"):
            bind_remote(tmp_path, server.url, AUTH, home=home, environ=ENV)


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
        assert load_remote_binding(tmp_path) is None
        assert server.dispatch_count == 0


def test_remote_identity_must_follow_existing_rule(tmp_path):
    with HTTPRemoteServer(InMemoryRemote("invalid-center")) as server:
        with pytest.raises(RemoteBindingError):
            bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        assert load_remote_binding(tmp_path) is None


def invoke(remote, operation, snapshot, commit):
    if operation == Operation.READ:
        return remote.read()
    if operation == Operation.FETCH:
        return remote.fetch(snapshot, [])
    if operation == Operation.COMMIT:
        return remote.commit(commit)
    if operation == Operation.RECOVER:
        return remote.recover()
    return remote.resolve_commit(
        snapshot.sync_id, commit.client_id, commit.request_id, commit.mutation_digest
    )


@pytest.mark.parametrize(
    "operation",
    [
        Operation.READ,
        Operation.FETCH,
        Operation.COMMIT,
        Operation.RECOVER,
        Operation.RESOLVE_COMMIT,
    ],
)
def test_identity_read_precedes_every_first_operation(tmp_path, operation):
    backend = InMemoryRemote("a" * 32)
    snapshot = backend.read()
    commit = request(backend, mutation())
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        remote = open_bound_remote(tmp_path, environ=ENV)
        server.requests.clear()
        invoke(remote, operation, snapshot, commit)
        operations = [decode_request(value).operation for value in server.requests]
        assert operations == (
            [Operation.READ]
            if operation == Operation.READ
            else [Operation.READ, operation]
        )


@pytest.mark.parametrize(
    "operation",
    [
        Operation.READ,
        Operation.FETCH,
        Operation.COMMIT,
        Operation.RECOVER,
        Operation.RESOLVE_COMMIT,
    ],
)
def test_changed_center_never_receives_old_operations(tmp_path, operation):
    backend = InMemoryRemote("a" * 32)
    snapshot = backend.read()
    commit = request(backend, mutation())
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        backend._snapshot = replace(snapshot, sync_id="b" * 32)
        server.requests.clear()
        with pytest.raises(StoreIdentityMismatch):
            invoke(
                open_bound_remote(tmp_path, environ=ENV), operation, snapshot, commit
            )
        assert [decode_request(value).operation for value in server.requests] == [
            Operation.READ
        ]
        assert load_remote_binding(tmp_path).sync_id == "a" * 32


def test_each_read_checks_pin_after_initial_verification(tmp_path):
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        remote = open_bound_remote(tmp_path, environ=ENV)
        remote.read()
        backend._snapshot = replace(backend.read(), sync_id="b" * 32)
        with pytest.raises(StoreIdentityMismatch):
            remote.read()
        server.requests.clear()
        with pytest.raises(StoreIdentityMismatch):
            remote.recover()
        assert [decode_request(value).operation for value in server.requests] == [
            Operation.READ
        ]


def _interrupted(monkeypatch, backend):
    """Fail reads until the identity-free recover call completes."""
    read, recover = backend.read, backend.recover
    state = {"pending": True}

    def interrupted_read():
        if state["pending"]:
            raise RecoveryRequired("Backend recovery required")
        return read()

    def complete_recovery():
        state["pending"] = False
        recover()
        return True

    monkeypatch.setattr(backend, "read", interrupted_read)
    monkeypatch.setattr(backend, "recover", complete_recovery)


def _operations(server):
    return [decode_request(value).operation for value in server.requests]


def test_recover_restores_interrupted_backend_then_verifies(tmp_path, monkeypatch):
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        _interrupted(monkeypatch, backend)
        server.requests.clear()
        assert open_bound_remote(tmp_path, environ=ENV).recover() is True
        assert _operations(server) == [
            Operation.READ,
            Operation.RECOVER,
            Operation.READ,
        ]


def test_recovered_backend_with_new_identity_is_rejected(tmp_path, monkeypatch):
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        backend._snapshot = replace(backend.read(), sync_id="b" * 32)
        _interrupted(monkeypatch, backend)
        remote = open_bound_remote(tmp_path, environ=ENV)
        with pytest.raises(StoreIdentityMismatch):
            remote.recover()
        server.requests.clear()
        with pytest.raises(StoreIdentityMismatch):
            remote.resolve_commit("a" * 32, "client", "c" * 32, "d" * 64)
        assert Operation.RESOLVE_COMMIT not in _operations(server)


def test_identity_bearing_call_never_precedes_verification(tmp_path, monkeypatch):
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend) as server:
        bind_remote(tmp_path, server.url, AUTH, home=tmp_path / "home", environ=ENV)
        _interrupted(monkeypatch, backend)
        server.requests.clear()
        remote = open_bound_remote(tmp_path, environ=ENV)
        with pytest.raises(RecoveryRequired):
            remote.resolve_commit("a" * 32, "client", "c" * 32, "d" * 64)
        assert _operations(server) == [Operation.READ]
        remote.recover()
        assert remote.resolve_commit("a" * 32, "client", "c" * 32, "d" * 64) is None
        assert _operations(server)[-1] == Operation.RESOLVE_COMMIT


@pytest.mark.parametrize(
    "fault", ["reject_401", "reject_403", "http_500", "drop_after_handler"]
)
def test_pending_survives_access_failures_and_recovers_on_restart(tmp_path, fault):
    local, home = _workspace(tmp_path / "local"), tmp_path / "home"
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        bind_remote(local, server.url, AUTH, home=home, environ=ENV)
        run_reconciliation(
            local, open_bound_remote(local, environ=ENV), home, dry_run=False
        )
        (local / "memory/notes/recovery.md").write_text("original", encoding="utf-8")
        before = backend.read().revision
        server.arm(fault, operation=Operation.COMMIT)
        with pytest.raises(WorkspaceReconcileError):
            run_reconciliation(
                local, open_bound_remote(local, environ=ENV), home, dry_run=False
            )
        path = local / PENDING_COMMIT_STATE
        text = path.read_bytes()
        with pytest.raises(RemoteBindingError):
            open_bound_remote(local, environ={})
        with pytest.raises(RemoteBindingError, match="pending"):
            remove_remote_binding(local, home=home)
        with pytest.raises(WorkspaceReconcileError):
            run_reconciliation(
                local,
                open_bound_remote(local, environ={AUTH.env: "wrong"}),
                home,
                dry_run=False,
            )
        assert path.read_bytes() == text
        pending = PendingCommitStore(local, home).load()
        run_reconciliation(
            local, open_bound_remote(local, environ=ENV), home, dry_run=False
        )
        assert PendingCommitStore(local, home).load() is None
        assert backend.read().revision == before + 1
        decoded = [decode_request(value) for value in server.requests]
        commits = [item.body for item in decoded if item.operation == Operation.COMMIT]
        assert commits[-1] == pending.request
        assert sum(
            item.request_id == pending.request.request_id for item in commits
        ) == (2 if fault.startswith("reject") else 1)


def test_pending_center_replacement_stops_before_resolution(tmp_path):
    local, home = _workspace(tmp_path / "local"), tmp_path / "home"
    backend = InMemoryRemote("a" * 32)
    with HTTPRemoteServer(backend) as server:
        bind_remote(local, server.url, AUTH, home=home, environ=ENV)
        run_reconciliation(
            local, open_bound_remote(local, environ=ENV), home, dry_run=False
        )
        (local / "memory/notes/recovery.md").write_text("original", encoding="utf-8")
        server.arm("drop_after_handler", operation=Operation.COMMIT)
        with pytest.raises(WorkspaceReconcileError):
            run_reconciliation(
                local, open_bound_remote(local, environ=ENV), home, dry_run=False
            )
        path = local / PENDING_COMMIT_STATE
        text = path.read_bytes()
        backend._snapshot = replace(backend.read(), sync_id="b" * 32)
        server.requests.clear()
        with pytest.raises(WorkspaceReconcileError, match="binding identity mismatch"):
            run_reconciliation(
                local, open_bound_remote(local, environ=ENV), home, dry_run=False
            )
        assert path.read_bytes() == text
        assert [decode_request(value).operation for value in server.requests] == [
            Operation.READ
        ]
