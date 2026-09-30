"""Deterministic delivery interleavings over both reference backends."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

import pytest

from aikito.skill_state import WorkspaceWriterLock
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.payload import (
    ResourceDescriptor,
    ResourceMutation,
    TomlPayload,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_store import SnapshotExpired
from aikito.workspace.remote_wire import validate_commit_result
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.resource_state import REPLICA_STATE
from aikito.workspace.resources import value_fingerprint
from aikito.workspace.toml_render import TomlValue
from test_workspace_remote_receipts import mutation, request
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from workspace_unreliable_remote import Fault, Gate, UnreliableRemote


@pytest.fixture(params=("memory", "filesystem"))
def case(request, tmp_path):
    backend = (
        InMemoryRemote()
        if request.param == "memory"
        else FilesystemRemote.create(tmp_path / "center")
    )
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    return local, backend, home, UnreliableRemote(backend)


def run(case):
    local, _, home, remote = case
    return run_reconciliation(local, remote, home, dry_run=False)


def confirm(local, remote, home):
    with WorkspaceWriterLock(home):
        return recover_pending(local, remote, home)


@pytest.mark.parametrize("fault", (Fault.UNAVAILABLE, Fault.LOSE_RESPONSE))
def test_restart_preserves_first_pairing_and_exact_request(case, fault):
    local, backend, home, remote = case
    (local / "memory/notes/upload.md").write_text("sent")
    remote.schedule("commit", fault)
    with pytest.raises(WorkspaceReconcileError):
        run(case)
    pending = PendingCommitStore(local, home).load()
    original = load_replica_state(local)[0]
    assert original.base == {}
    assert original.replica_id == pending.request.client_id
    assert backend.read().revision == (1 if fault is Fault.LOSE_RESPONSE else 0)
    resumed = UnreliableRemote(
        FilesystemRemote(backend.root)
        if isinstance(backend, FilesystemRemote)
        else backend
    )
    assert confirm(local, resumed, home)
    assert load_replica_state(local)[0].replica_id == original.replica_id
    if fault is Fault.UNAVAILABLE:
        assert resumed.commits == remote.commits
    else:
        assert resumed.commits == ()
    assert backend.read().revision == 1
    assert PendingCommitStore(local, home).load() is None


def test_duplicate_delivery_enters_actual_backend_and_returns_one_receipt(case):
    _, backend, _, remote = case
    req = request(backend)
    remote.schedule("commit", Fault.DUPLICATE_DELIVERY)
    result = remote.commit(req)
    validate_commit_result(req, result)
    assert remote.results == (result, result)
    assert len(remote.deliveries) == 2
    assert remote.deliveries[0] == remote.deliveries[1] == remote.commits[0]
    assert backend.read().revision == 1


@pytest.mark.parametrize("competing", (False, True))
def test_delayed_original_and_exact_retry_share_cas_and_receipt(case, competing):
    local, backend, home, remote = case
    (local / "memory/notes/upload.md").write_text("queued upload")
    remote.schedule("commit", Fault.DELAY_DELIVERY)
    with pytest.raises(WorkspaceReconcileError):
        run(case)
    pending = PendingCommitStore(local, home).load()
    assert remote.deliveries == ()
    assert (
        remote.resolve_commit(
            pending.request.expected.sync_id,
            pending.request.client_id,
            pending.request.request_id,
            pending.request.mutation_digest,
        )
        is None
    )
    if competing:
        backend.commit(request(backend, mutation("other"), client="other"))
    gate = Gate(arrivals=2)
    remote.schedule("commit", Fault.PAUSE_DELIVERY, gate=gate)
    with ThreadPoolExecutor(max_workers=2) as executor:
        original = executor.submit(remote.deliver_delayed, gate=gate)
        retry = executor.submit(confirm, local, remote, home)
        try:
            gate.wait()
            assert remote.commits[0] == remote.commits[1]
        finally:
            gate.release()
        assert retry.result(timeout=15)
        if competing:
            with pytest.raises(SnapshotExpired):
                original.result(timeout=15)
        else:
            receipt = original.result(timeout=15)
            validate_commit_result(pending.request, receipt)
            assert remote.results == (receipt, receipt)
    assert remote.deliveries[0] == remote.deliveries[1] == remote.commits[0]
    assert backend.read().revision == 1
    state, _ = load_replica_state(local)
    assert ("memory:notes/upload.md" in state.base) == (not competing)
    assert ("memory:notes/upload.md" in backend.read().resources) == (not competing)
    assert PendingCommitStore(local, home).load() is None


def test_resolve_failure_retains_pending_and_never_sends_new_commit(case):
    local, backend, home, remote = case
    (local / "memory/notes/upload.md").write_text("sent")
    remote.schedule("commit", Fault.LOSE_RESPONSE)
    with pytest.raises(WorkspaceReconcileError):
        run(case)
    store = PendingCommitStore(local, home)
    pending, before = store.load(), (local / REPLICA_STATE).read_bytes()
    for _ in range(2):
        remote.schedule("resolve_commit", Fault.UNAVAILABLE)
        with pytest.raises(WorkspaceReconcileError, match="resolve_commit unavailable"):
            run(case)
        assert store.load() == pending
        assert (local / REPLICA_STATE).read_bytes() == before
        assert len(remote.commits) == 1
    run(case)
    assert store.load() is None
    assert backend.read().revision == 1


def test_old_receipt_survives_other_clients_without_historical_fetch(case):
    local, backend, home, remote = case
    note = local / "memory/notes/upload.md"
    note.write_text("original")
    remote.schedule("commit", Fault.LOSE_RESPONSE)
    with pytest.raises(WorkspaceReconcileError):
        run(case)
    pending = PendingCommitStore(local, home).load()
    uploaded = next(
        m for m in pending.request.mutations if m.id == "memory:notes/upload.md"
    )
    backend.commit(
        request(
            backend,
            ResourceMutation(uploaded.id, uploaded.after, None, None),
            client="deleter",
        )
    )
    for index in range(3):
        backend.commit(
            request(backend, mutation(f"other-{index}"), client=f"other-{index}")
        )
    note.write_text("local edited while offline")
    remote.schedule("read", Fault.UNAVAILABLE)
    remote.schedule("fetch", Fault.UNAVAILABLE)
    assert confirm(local, remote, home)
    state, _ = load_replica_state(local)
    assert state.revision == pending.request.expected.revision + 1
    assert state.revision < backend.read().revision
    assert state.base[uploaded.id].fingerprint == uploaded.after.fingerprint
    assert note.read_text() == "local edited while offline"
    assert len(remote.commits) == 1
    assert "fetch" not in remote.calls


@pytest.mark.parametrize("target", ("memory", "toml"))
def test_wait_time_edits_block_local_write_and_recovery_only_confirms_upload(
    case, target
):
    local, backend, home, remote = case
    (local / "config.toml").write_text('[feature]\nkeep = "base"\n')
    run(case)
    if target == "memory":
        change = mutation("download", b"remote download")
        edited = local / "memory/notes/download.md"
        new_text = "user edit while waiting"
    else:
        payload = TomlPayload.from_value(TomlValue(("feature", "download"), "remote"))
        change = ResourceMutation(
            "config:feature.download",
            None,
            ResourceDescriptor(value_fingerprint("remote"), payload_hash(payload)),
            payload,
        )
        edited = local / "config.toml"
        new_text = '[feature]\nkeep = "user edit"\n'
    backend.commit(request(backend, change, client="other"))
    (local / "memory/notes/upload.md").write_text("upload")
    before = load_replica_state(local)[0]
    gate = Gate()
    remote.schedule("commit", Fault.PAUSE_RESPONSE, gate=gate)
    with ThreadPoolExecutor(max_workers=1) as executor:
        applying = executor.submit(run, case)
        try:
            gate.wait()
            edited.write_text(new_text)
        finally:
            gate.release()
        with pytest.raises(WorkspaceReconcileError):
            applying.result(timeout=15)
    assert edited.read_text() == new_text
    assert load_replica_state(local)[0] == before
    assert confirm(local, remote, home)
    state, _ = load_replica_state(local)
    assert change.id not in state.base
    assert (
        state.base["memory:notes/upload.md"].fingerprint
        == backend.read().resources["memory:notes/upload.md"].fingerprint
    )
    assert edited.read_text() == new_text


def test_stale_replica_cannot_replace_unknown_winner_receipt(case, tmp_path):
    local, backend, home, remote = case
    run(case)
    clone = _workspace(tmp_path / "clone")
    path = clone / REPLICA_STATE
    path.parent.mkdir(parents=True)
    path.write_bytes((local / REPLICA_STATE).read_bytes())
    (local / "memory/notes/winner.md").write_text("winner")
    remote.schedule("commit", Fault.LOSE_RESPONSE)
    with pytest.raises(WorkspaceReconcileError):
        run(case)
    winner = PendingCommitStore(local, home).load()
    (clone / "memory/notes/clone.md").write_text("stale clone upload")
    with pytest.raises(WorkspaceReconcileError, match="receipt history changed"):
        run_reconciliation(
            clone, UnreliableRemote(backend), tmp_path / "clone-home", dry_run=False
        )
    assert PendingCommitStore(clone, tmp_path / "clone-home").load() is not None
    assert "memory:notes/clone.md" not in backend.read().resources
    assert confirm(local, remote, home)
    assert (
        load_replica_state(local)[0].receipt_cursor.request_id
        == winner.request.request_id
    )
