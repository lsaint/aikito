"""Pending identity persistence, corruption guards and atomic local completion."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aikito.workspace import transactions
from aikito.workspace.pending_commit import (
    PendingCommitError,
    PendingCommitStore,
)
from aikito.workspace.replica_state import ReplicaState, load_replica_state
from aikito.workspace.resource_state import (
    PENDING_COMMIT_STATE,
    REPLICA_STATE,
    local_resource_for_id,
)
from aikito.workspace.remote_store import InvalidContent, SnapshotExpired
from aikito.workspace.remote_wire import encode_commit_request
from aikito.workspace.reconcile import WorkspaceReconcileError, build_reconcile_plan
from test_workspace_remote_receipts import mutation, request, resolve
from test_workspace_remote_receipt_crashes import SimulatedCrash
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace


@pytest.fixture
def pending_case(tmp_path):
    local = _workspace(tmp_path / "local")
    remote = InMemoryRemote()
    req = request(remote, client="a" * 32)
    store = PendingCommitStore(local, tmp_path / "home")
    pending = store.persist(req, safe_resource_ids=(req.mutations[0].id,))
    return local, remote, store, pending


def completed_state(pending, receipt):
    resources = {
        m.id: replace(
            local_resource_for_id(m.id, m.after.fingerprint),
            references=m.after.references,
            mode_fingerprint=m.after.mode_fingerprint,
        )
        for m in pending.request.mutations
        if m.after is not None
    }
    return ReplicaState(
        receipt.sync_id, receipt.client_id, receipt.accepted_revision, resources
    )


def test_first_pairing_is_persisted_with_exact_request(pending_case):
    local, remote, store, pending = pending_case
    initial, _ = load_replica_state(local)
    assert initial.replica_id == pending.request.client_id
    assert initial.base == {} and initial.revision == 0
    reopened = PendingCommitStore(local, store.home)
    assert reopened.load() == pending
    assert encode_commit_request(reopened.load().request) == encode_commit_request(
        pending.request
    )
    assert remote.read().revision == 0
    assert (local / PENDING_COMMIT_STATE).exists()


def test_unresolved_pending_cannot_be_overwritten_or_cleared(pending_case):
    local, remote, store, pending = pending_case
    before = (
        (local / PENDING_COMMIT_STATE).read_bytes(),
        (local / REPLICA_STATE).read_bytes(),
    )
    assert resolve(remote, pending.request) is None
    with pytest.raises(PendingCommitError, match="already exists"):
        store.persist(
            replace(pending.request, request_id="other"),
            safe_resource_ids=pending.safe_resource_ids,
        )
    with pytest.raises(PendingCommitError, match="neither completed"):
        store.clear(pending)
    assert before == (
        (local / PENDING_COMMIT_STATE).read_bytes(),
        (local / REPLICA_STATE).read_bytes(),
    )


def test_definite_cas_rejection_can_clear_without_advancing_base(pending_case):
    local, remote, store, pending = pending_case
    initial, _ = load_replica_state(local)
    remote.commit(request(remote, mutation("other"), client="other"))
    with pytest.raises(SnapshotExpired) as rejection:
        remote.commit(pending.request)
    store.clear(pending, rejected=rejection.value)
    assert store.load() is None
    assert load_replica_state(local)[0] == initial


def test_completion_and_cleanup_do_not_touch_later_local_edits(pending_case):
    local, remote, store, pending = pending_case
    note = local / "memory/notes/one.md"
    note.write_bytes(b"user edited after upload")
    receipt = remote.commit(pending.request)
    state = store.complete(pending, receipt, completed_state(pending, receipt))
    assert (
        state.base[pending.request.mutations[0].id].fingerprint
        == pending.request.mutations[0].after.fingerprint
    )
    assert pending.completed_by(load_replica_state(local)[0])
    assert store.load() == pending
    # A repeated completion cannot replay a previously prepared local write.
    assert store.complete(pending, receipt, completed_state(pending, receipt)) == state
    store.clear(pending)
    store.clear(pending)
    assert store.load() is None
    assert note.read_bytes() == b"user edited after upload"


def test_bad_result_keeps_pending_and_original_state(pending_case):
    local, remote, store, pending = pending_case
    before = (local / REPLICA_STATE).read_bytes()
    receipt = remote.commit(pending.request)
    with pytest.raises(InvalidContent):
        store.complete(
            pending,
            replace(receipt, result_digest="f" * 64),
            completed_state(pending, receipt),
        )
    assert store.load() == pending
    assert (local / REPLICA_STATE).read_bytes() == before


def test_completion_cannot_change_unsafe_or_wrong_upload_base(pending_case):
    local, remote, store, pending = pending_case
    receipt = remote.commit(pending.request)
    correct = completed_state(pending, receipt)
    unsafe = local_resource_for_id("memory:notes/blocked.md", "a" * 64)
    with pytest.raises(PendingCommitError, match="outside"):
        store.complete(
            pending, receipt, replace(correct, base={**correct.base, unsafe.id: unsafe})
        )
    with pytest.raises(PendingCommitError, match="confirmed descriptor"):
        store.complete(pending, receipt, replace(correct, base={}))
    assert store.load() == pending


def test_pending_scope_excludes_conflict_and_blocked_uploads(pending_case):
    _, _, _, pending = pending_case
    with pytest.raises(PendingCommitError):
        replace(pending, excluded_resource_ids=pending.safe_resource_ids)
    with pytest.raises(PendingCommitError):
        replace(pending, safe_resource_ids=())


@pytest.mark.parametrize(
    "corruption",
    [
        "version",
        "checksum",
        "request-id",
        "request-digest",
        "duplicate-field",
        "invalid-json",
        "safe-scope",
    ],
)
def test_corrupt_pending_is_preserved_and_blocks(pending_case, corruption):
    local, _, store, pending = pending_case
    path = local / PENDING_COMMIT_STATE
    raw = json.loads(path.read_text())
    if corruption == "version":
        raw["version"] = 99
    elif corruption == "checksum":
        raw["envelope_digest"] = "f" * 64
    elif corruption == "request-id":
        changed = replace(pending.request, request_id="tampered")
        raw["request"] = base64.b64encode(encode_commit_request(changed)).decode()
    elif corruption == "request-digest":
        body = json.loads(base64.b64decode(raw["request"]))
        body["mutation_digest"] = "b" * 64
        raw["request"] = base64.b64encode(json.dumps(body).encode()).decode()
        unsigned = {
            key: value for key, value in raw.items() if key != "envelope_digest"
        }
        raw["envelope_digest"] = hashlib.sha256(
            json.dumps(
                unsigned, sort_keys=True, ensure_ascii=False, separators=(",", ":")
            ).encode()
        ).hexdigest()
    elif corruption == "safe-scope":
        raw["safe_resource_ids"] = []
    text = json.dumps(raw)
    if corruption == "duplicate-field":
        text = '{"version":1,' + text[1:]
    if corruption == "invalid-json":
        text = "not json"
    path.write_text(text)
    before = path.read_bytes(), (local / REPLICA_STATE).read_bytes()
    with pytest.raises(PendingCommitError):
        store.load()
    assert before == (path.read_bytes(), (local / REPLICA_STATE).read_bytes())


def test_replica_identity_drift_blocks_without_deleting_pending(pending_case):
    local, _, store, _ = pending_case
    path = local / REPLICA_STATE
    state, _ = load_replica_state(local)
    path.write_text(replace(state, replica_id="b" * 32).encode())
    with pytest.raises(PendingCommitError, match="outside pending"):
        store.load()
    assert (local / PENDING_COMMIT_STATE).exists()


def test_legacy_engine_blocks_unresolved_pending(pending_case):
    local, remote, _, _ = pending_case
    with pytest.raises(WorkspaceReconcileError, match="receipt-aware"):
        build_reconcile_plan(local, remote)


@pytest.mark.parametrize("checkpoint", ["replica", "pending"])
def test_first_pairing_journal_recovers_both_state_files(
    tmp_path, monkeypatch, checkpoint
):
    local = _workspace(tmp_path / "local")
    store = PendingCommitStore(local, tmp_path / "home")
    req = request(InMemoryRemote(), client="a" * 32)
    original = transactions.atomic_text

    def interrupt(path, text):
        original(path, text)
        target = REPLICA_STATE if checkpoint == "replica" else PENDING_COMMIT_STATE
        if path == local / target:
            raise SimulatedCrash

    with monkeypatch.context() as patch:
        patch.setattr(transactions, "atomic_text", interrupt)
        with pytest.raises(SimulatedCrash):
            store.persist(req, safe_resource_ids=(req.mutations[0].id,))
    reopened = PendingCommitStore(local, store.home)
    assert reopened.load() is None
    assert load_replica_state(local)[0] is None
    pending = reopened.persist(req, safe_resource_ids=(req.mutations[0].id,))
    assert reopened.load() == pending


@pytest.mark.parametrize("checkpoint", ["resource", "state", "committed", "cleanup"])
@pytest.mark.parametrize("kind", ["memory", "inbox"])
def test_local_resources_base_and_marker_share_one_journal(
    pending_case, tmp_path, monkeypatch, checkpoint, kind
):
    local, remote, store, original_pending = pending_case
    resource_id = (
        "memory:notes/download.md" if kind == "memory" else "inbox:download.md"
    )
    relative = "memory/notes/download.md" if kind == "memory" else "capture/download.md"
    if kind == "inbox":
        monkeypatch.setattr(
            "aikito.workspace.pending_commit.get_inbox_path",
            lambda _: local / "capture",
        )
    # Add a verified download to the round's safe scope before sending the request.
    pending = replace(
        original_pending,
        safe_resource_ids=(
            *original_pending.safe_resource_ids,
            resource_id,
        ),
    )
    (local / PENDING_COMMIT_STATE).write_text(pending.encode())
    receipt = remote.commit(pending.request)
    payload = b"downloaded content"
    source = tmp_path / "staging.txt"
    source.write_bytes(payload)
    fingerprint = hashlib.sha256(payload).hexdigest()
    resource = local_resource_for_id(resource_id, fingerprint)
    correct = completed_state(pending, receipt)
    correct = replace(correct, base={**correct.base, resource.id: resource})
    destination = local / relative
    change = transactions.Change(
        target=0,
        path=relative,
        kind=kind,
        source=source,
        before=None,
        after=fingerprint,
    )
    atomic, move, cleanup = (
        transactions.atomic_text,
        transactions.os.replace,
        transactions._cleanup,
    )

    def write(path, text):
        atomic(path, text)
        if checkpoint == "state" and path == local / REPLICA_STATE:
            raise SimulatedCrash
        if (
            checkpoint == "committed"
            and "workspace-transactions" in path.parts
            and path.name == "pending.json"
            and json.loads(text)["phase"] == "committed"
        ):
            raise SimulatedCrash

    def replace_file(src, dst):
        move(src, dst)
        if checkpoint == "resource" and Path(dst) == destination:
            raise SimulatedCrash

    def clean(*args):
        if checkpoint == "cleanup":
            raise SimulatedCrash
        cleanup(*args)

    with monkeypatch.context() as patch:
        patch.setattr(transactions, "atomic_text", write)
        patch.setattr(transactions.os, "replace", replace_file)
        patch.setattr(transactions, "_cleanup", clean)
        with pytest.raises(SimulatedCrash):
            store.complete(pending, receipt, correct, changes=(change,))
    if kind == "inbox":
        # Recovery must use the saved journal policy after configuration changes.
        monkeypatch.setattr(
            "aikito.workspace.pending_commit.get_inbox_path",
            lambda _: local / "new-inbox",
        )
    reopened = PendingCommitStore(local, store.home)
    assert reopened.load() == pending
    retained = checkpoint in {"committed", "cleanup"}
    assert destination.exists() == retained
    assert pending.completed_by(load_replica_state(local)[0]) == retained
    if retained:
        reopened.clear(pending)
    else:
        with pytest.raises(PendingCommitError):
            reopened.clear(pending)


def test_old_replica_state_reads_without_writing(tmp_path):
    local = _workspace(tmp_path / "local")
    path = local / REPLICA_STATE
    path.parent.mkdir(parents=True)
    old = ReplicaState("center", "a" * 32, 0, {}).encode()
    path.write_text(old)
    before = path.read_bytes()
    state, _ = load_replica_state(local)
    assert state.receipt_cursor is state.completion_marker is None
    assert path.read_bytes() == before
