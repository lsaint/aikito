"""Pending identity and download atomicity across HTTP socket failures."""

from __future__ import annotations

import pytest
from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from test_workspace_remote_receipts import mutation, request

from aikito.workspace import remote_limits
from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.serialized_remote import SerializedRemoteStore


@pytest.mark.parametrize(
    "fault", ["drop_after_request", "drop_after_handler", "delay_response"]
)
def test_pending_resolves_or_resends_original_http_commit(tmp_path, fault):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    backend = InMemoryRemote("center")
    with HTTPRemoteServer(backend) as server:
        server.delay = 1
        remote = SerializedRemoteStore(HTTPTransport(server.url, timeout=0.2).exchange)
        run_reconciliation(local, remote, home, dry_run=False)
        note = local / "memory/notes/recovery.md"
        note.write_text("original", encoding="utf-8")
        before = backend.read().revision
        server.arm(fault, operation=Operation.COMMIT)
        with pytest.raises(WorkspaceReconcileError):
            run_reconciliation(local, remote, home, dry_run=False)
        pending = PendingCommitStore(local, home).load()
        assert pending is not None
        assert load_replica_state(local)[0].revision == before
        original = pending.request
        run_reconciliation(local, remote, home, dry_run=False)
        assert PendingCommitStore(local, home).load() is None
        assert backend.read().revision == before + 1
        decoded = [decode_request(value) for value in server.requests]
        commits = [item.body for item in decoded if item.operation == Operation.COMMIT]
        assert commits[-1] == original
        count = 2 if fault == "drop_after_request" else 1
        assert sum(item.request_id == original.request_id for item in commits) == count
        resolves = [
            item.body for item in decoded if item.operation == Operation.RESOLVE_COMMIT
        ]
        assert resolves[-1] == (
            original.expected.sync_id,
            original.client_id,
            original.request_id,
            original.mutation_digest,
        )
        assert note.read_text(encoding="utf-8") == "original"


def test_http_fetch_capacity_failure_preserves_local_and_base(tmp_path, monkeypatch):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    backend = InMemoryRemote("center")
    with HTTPRemoteServer(backend) as server:
        remote = SerializedRemoteStore(HTTPTransport(server.url).exchange)
        run_reconciliation(local, remote, home, dry_run=False)
        backend.commit(request(backend, mutation("large", b"x" * 8192)))
        previous_state = load_replica_state(local)
        monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 4096)
        plan = run_reconciliation(local, remote, home, dry_run=False)
        assert (
            next(
                item for item in plan.items if item.id == "memory:notes/large.md"
            ).action
            == "BLOCKED"
        )
        assert not (local / "memory/notes/large.md").exists()
        current = load_replica_state(local)[0]
        assert current.base == previous_state[0].base
        assert current.receipt_cursor == previous_state[0].receipt_cursor
        assert current.unpaired_ids == previous_state[0].unpaired_ids
        assert PendingCommitStore(local, home).load() is None
        monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 65536)
        run_reconciliation(local, remote, home, dry_run=False)
        assert (local / "memory/notes/large.md").read_bytes() == b"x" * 8192


def test_oversized_http_commit_never_reaches_server_commit(tmp_path, monkeypatch):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    with HTTPRemoteServer(InMemoryRemote("center")) as server:
        remote = SerializedRemoteStore(HTTPTransport(server.url).exchange)
        run_reconciliation(local, remote, home, dry_run=False)
        server.requests.clear()
        (local / "memory/notes/large.md").write_bytes(b"x" * 8192)
        monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 4096)
        plan = run_reconciliation(local, remote, home, dry_run=False)
        assert (
            next(
                item for item in plan.items if item.id == "memory:notes/large.md"
            ).action
            == "BLOCKED"
        )
        assert PendingCommitStore(local, home).load() is None
        assert not any(
            decode_request(value).operation == Operation.COMMIT
            for value in server.requests
        )
