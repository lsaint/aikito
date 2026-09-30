"""Bytes-level commit uncertainty: pending retained, then resolved by identity."""

from __future__ import annotations

import json

import pytest
from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace

from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote_protocol import Operation, encode_read_response
from aikito.workspace.remote_store import RemoteSnapshot
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.serialized_remote import (
    SerializedRemoteStore,
    TransportNotDelivered,
)


class FaultyLoopback(LoopbackTransport):
    """Deliver (or withhold) one commit, then corrupt its response."""

    def __init__(self, store, fault: str):
        super().__init__(store)
        self._fault = fault
        self._armed = False

    def arm(self) -> None:
        self._armed = True

    def exchange(self, request: bytes) -> bytes:
        if not (self._armed and self._is_commit(request)):
            return super().exchange(request)
        self._armed = False
        if self._fault == "not-delivered":
            raise TransportNotDelivered("simulated non-delivery")
        response = super().exchange(request)
        if self._fault == "lost":
            raise RuntimeError("simulated response loss")
        if self._fault == "corrupted":
            return b"garbage"
        if self._fault == "mismatch":
            return encode_read_response(RemoteSnapshot("mismatch", 0, {}))
        return response

    @staticmethod
    def _is_commit(request: bytes) -> bool:
        try:
            return json.loads(request).get("operation") == Operation.COMMIT
        except (ValueError, TypeError, AttributeError):
            return False


def paired(tmp_path, fault):
    a, b = _workspace(tmp_path / "a"), _workspace(tmp_path / "b")
    home = tmp_path / "home"
    backend = InMemoryRemote("center")
    transport = FaultyLoopback(backend, fault)
    remote = SerializedRemoteStore(transport.exchange)
    for local in (a, b):
        run_reconciliation(local, remote, home, dry_run=False)
    return a, b, home, backend, remote, transport


@pytest.mark.parametrize("fault", ["lost", "corrupted", "mismatch"])
def test_uncertain_commit_preserves_pending_and_resolves(tmp_path, fault):
    a, b, home, backend, remote, transport = paired(tmp_path, fault)
    transport.arm()
    write(a, "memory/notes/recovery.md", "first")

    with pytest.raises(WorkspaceReconcileError):
        run_reconciliation(a, remote, home, dry_run=False)

    # The request reached the center, so the outcome is unknown, not a failure.
    center = backend.read()
    assert "memory:notes/recovery.md" in center.resources
    store = PendingCommitStore(a, home)
    assert store.load() is not None
    state, _ = load_replica_state(a)
    assert state.revision < center.revision

    # Recovery resolves the original request identity instead of creating a new one.
    plan = run_reconciliation(a, remote, home, dry_run=False)
    assert not plan.blocked
    assert store.load() is None
    run_reconciliation(b, remote, home, dry_run=False)
    assert (b / "memory/notes/recovery.md").read_text() == "first"


def test_not_delivered_commit_retries_the_same_request(tmp_path):
    a, b, home, backend, remote, transport = paired(tmp_path, "not-delivered")
    transport.arm()
    write(a, "memory/notes/recovery.md", "first")

    with pytest.raises(WorkspaceReconcileError):
        run_reconciliation(a, remote, home, dry_run=False)

    # Nothing reached the center and the original request stays pending.
    assert "memory:notes/recovery.md" not in backend.read().resources
    store = PendingCommitStore(a, home)
    pending = store.load()
    assert pending is not None
    original = pending.request

    plan = run_reconciliation(a, remote, home, dry_run=False)
    assert not plan.blocked
    assert store.load() is None
    # Retry reused the original request identity, never a new one.
    receipt = remote.resolve_commit(
        original.expected.sync_id,
        original.client_id,
        original.request_id,
        original.mutation_digest,
    )
    assert receipt is not None and receipt.request_id == original.request_id
    run_reconciliation(b, remote, home, dry_run=False)
    assert (b / "memory/notes/recovery.md").read_text() == "first"
