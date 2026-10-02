"""Receipt-aware engine boundaries and conservative restart confirmation."""

from dataclasses import replace

import pytest

from aikito.workspace import reconcile
from aikito.workspace import remote_limits
from aikito.workspace.remote_protocol import (
    encode_commit_request as encode_protocol_request,
)
from aikito.workspace.serialized_remote import SerializedRemoteStore
from workspace_loopback_remote import LoopbackTransport
from aikito.workspace.payload import ResourceMutation
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.remote_store import CommitOutcomeUnknown, StoreUnavailable
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.resource_state import PENDING_COMMIT_STATE
from aikito.workspace.remote_wire import encode_commit_request
from aikito.skill_state import WorkspaceWriterLock
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from test_workspace_remote_receipts import mutation, request


@pytest.fixture
def case(tmp_path):
    local = _workspace(tmp_path / "local")
    remote = InMemoryRemote()
    home = tmp_path / "home"
    return local, remote, home, PendingCommitStore(local, home)


def run(case):
    local, remote, home, _ = case
    return reconcile.run_reconciliation(local, remote, home, dry_run=False)


@pytest.mark.parametrize("serialized", [False, True])
@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_commit_capacity_before_pending(case, monkeypatch, serialized, headroom):
    local, backend, home, store = case
    remote = (
        SerializedRemoteStore(LoopbackTransport(backend).exchange)
        if serialized
        else backend
    )
    case = local, remote, home, store
    run(case)
    state_before = load_replica_state(local)
    snapshot_before = backend.read()
    note = local / "memory/notes/upload.md"
    note.write_text("容量检查" * 100, encoding="utf-8")
    original_build = reconcile.build_commit_request
    original_commit = remote.commit
    sent = []

    def build(*args, **kwargs):
        req = original_build(*args, **kwargs)
        encoded = encode_protocol_request(req)
        assert len(encoded) > len(note.read_bytes())
        monkeypatch.setattr(
            remote_limits, "MAX_REMOTE_REQUEST_BYTES", len(encoded) + headroom
        )
        return req

    def commit(req):
        assert store.load().request == req
        sent.append(req)
        return original_commit(req)

    monkeypatch.setattr(reconcile, "build_commit_request", build)
    monkeypatch.setattr(remote, "commit", commit)
    if headroom < 0:
        with monkeypatch.context() as guard:

            def forbidden(*args, **kwargs):
                raise AssertionError("Oversized commit persisted pending")

            guard.setattr(PendingCommitStore, "persist", forbidden)
            with pytest.raises(reconcile.WorkspaceReconcileError, match="size limit"):
                run(case)
        assert sent == []
        assert store.load() is None
        assert load_replica_state(local) == state_before
        assert backend.read() == snapshot_before
        assert note.read_text(encoding="utf-8") == "容量检查" * 100
        monkeypatch.setattr(reconcile, "build_commit_request", original_build)
        note.write_text("smaller", encoding="utf-8")
        run(case)
    else:
        run(case)
    assert len(sent) == 1
    assert store.load() is None
    assert backend.read().revision == snapshot_before.revision + 1


def test_oversized_first_commit_does_not_pair_replica(case, monkeypatch):
    local, remote, _, store = case
    (local / "memory/notes/upload.md").write_text("upload", encoding="utf-8")
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 1)

    def forbidden(*args, **kwargs):
        raise AssertionError("Oversized first commit persisted or sent")

    monkeypatch.setattr(PendingCommitStore, "persist", forbidden)
    monkeypatch.setattr(remote, "commit", forbidden)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="size limit"):
        run(case)
    assert store.load() is None
    assert load_replica_state(local)[0] is None
    assert remote.read().revision == 0


def test_identity_and_exact_pending_are_durable_before_send(case, monkeypatch):
    local, remote, _, store = case
    (local / "memory/notes/upload.md").write_text("upload")
    original = remote.commit
    sent = []

    def commit(req):
        pending = store.load()
        state, _ = load_replica_state(local)
        assert encode_commit_request(pending.request) == encode_commit_request(req)
        assert state.replica_id == req.client_id
        sent.append(req)
        return original(req)

    monkeypatch.setattr(remote, "commit", commit)
    run(case)
    assert len(sent) == 1
    assert store.load() is None
    state, _ = load_replica_state(local)
    assert state.receipt_cursor.request_id == sent[0].request_id
    assert state.completion_marker.request_id == sent[0].request_id


def test_download_and_noop_never_commit_or_persist_pending(case, monkeypatch):
    local, remote, _, store = case
    run(case)
    remote.commit(request(remote, mutation("download"), client="other"))
    revision = remote.read().revision

    def forbidden(*args, **kwargs):
        raise AssertionError("No-upload round tried to commit")

    monkeypatch.setattr(remote, "commit", forbidden)
    monkeypatch.setattr(PendingCommitStore, "persist", forbidden)
    monkeypatch.setattr(reconcile, "build_commit_request", forbidden)
    run(case)
    assert (local / "memory/notes/download.md").read_bytes() == b"one"
    assert load_replica_state(local)[0].revision == revision
    run(case)
    assert remote.read().revision == revision
    assert store.load() is None


@pytest.mark.parametrize("accepted", [False, True])
def test_restart_confirms_exact_upload_then_replans_user_edit(
    case, monkeypatch, accepted
):
    local, remote, home, store = case
    note = local / "memory/notes/upload.md"
    note.write_text("sent")
    original = remote.commit
    sent = []

    def lose(req):
        sent.append(req)
        if accepted:
            original(req)
        raise CommitOutcomeUnknown("response unavailable")

    monkeypatch.setattr(remote, "commit", lose)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="response unavailable"):
        run(case)
    pending = store.load()
    assert load_replica_state(local)[0].base == {}
    note.write_text("edited after send")

    def retry(req):
        sent.append(req)
        return original(req)

    monkeypatch.setattr(remote, "commit", retry)
    run(case)
    assert store.load() is None
    assert note.read_text() == "edited after send"
    assert remote.read().revision == 2
    if not accepted:
        assert encode_commit_request(sent[0]) == encode_commit_request(sent[1])
    assert sent[-1].request_id != pending.request.request_id
    assert sent[-1].previous_receipt.request_id == pending.request.request_id


def test_missing_receipt_and_failed_retry_keep_pending_and_base(case, monkeypatch):
    local, remote, home, store = case
    (local / "memory/notes/upload.md").write_text("sent")
    calls = []

    def unavailable(req):
        calls.append(req)
        raise StoreUnavailable("offline")

    monkeypatch.setattr(remote, "commit", unavailable)
    for _ in range(2):
        with pytest.raises(reconcile.WorkspaceReconcileError, match="offline"):
            run(case)
    assert len(calls) == 2
    assert calls[0] == calls[1] == store.load().request
    assert load_replica_state(local)[0].base == {}
    assert remote.read().revision == 0


def test_invalid_response_retains_accepted_pending(case, monkeypatch):
    local, remote, _, store = case
    (local / "memory/notes/upload.md").write_text("sent")
    original = remote.commit

    def corrupt(req):
        return replace(original(req), result_digest="f" * 64)

    monkeypatch.setattr(remote, "commit", corrupt)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="digest mismatch"):
        run(case)
    assert store.load() is not None
    assert load_replica_state(local)[0].base == {}
    assert remote.read().revision == 1
    monkeypatch.setattr(remote, "commit", original)
    run(case)
    assert remote.read().revision == 1
    assert store.load() is None


def test_recovery_never_replays_download_over_wait_time_edit(case, monkeypatch):
    local, remote, home, store = case
    upload = local / "memory/notes/upload.md"
    upload.write_text("upload")
    remote.commit(
        request(remote, mutation("download", b"remote download"), client="other")
    )
    original = remote.commit
    download = local / "memory/notes/download.md"

    def edit_after_accept(req):
        result = original(req)
        download.write_text("user edit during network wait")
        return result

    monkeypatch.setattr(remote, "commit", edit_after_accept)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        run(case)
    pending = store.load()
    monkeypatch.setattr(remote, "commit", original)
    with WorkspaceWriterLock(home):
        assert recover_pending(local, remote, home)
    state, _ = load_replica_state(local)
    assert "memory:notes/upload.md" in state.base
    assert "memory:notes/download.md" not in state.base
    assert download.read_text() == "user edit during network wait"
    assert store.load() is None
    plan = run(case)
    assert any(item.id == "memory:notes/download.md" for item in plan.conflicts)
    assert remote.read().revision == pending.request.expected.revision + 1


def test_completion_marker_cleanup_needs_no_remote_and_does_not_replay(
    case, monkeypatch
):
    local, remote, home, store = case
    note = local / "memory/notes/upload.md"
    note.write_text("upload")
    original_clear = PendingCommitStore.clear

    def interrupt(*args, **kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(PendingCommitStore, "clear", interrupt)
    with pytest.raises(KeyboardInterrupt):
        run(case)
    pending = store.load()
    assert pending.completed_by(load_replica_state(local)[0])
    note.write_text("later edit")
    monkeypatch.setattr(PendingCommitStore, "clear", original_clear)

    def offline(*args):
        raise AssertionError("Cleanup accessed remote")

    monkeypatch.setattr(remote, "recover", offline)
    monkeypatch.setattr(remote, "resolve_commit", offline)
    monkeypatch.setattr(remote, "read", offline)
    assert not reconcile.recover_reconciliation(local, remote, home)
    assert not (local / PENDING_COMMIT_STATE).exists()
    assert note.read_text() == "later edit"


def test_definite_cas_rejection_clears_old_request_before_replanning(case, monkeypatch):
    local, remote, home, store = case
    (local / "memory/notes/upload.md").write_text("upload")
    original = remote.commit

    def failed_send(req):
        raise CommitOutcomeUnknown("not confirmed")

    monkeypatch.setattr(remote, "commit", failed_send)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        run(case)
    pending = store.load()
    monkeypatch.setattr(remote, "commit", original)
    original(request(remote, mutation("other"), client="other"))
    with WorkspaceWriterLock(home):
        assert recover_pending(local, remote, home)
    assert store.load() is None
    assert load_replica_state(local)[0].base == {}
    run(case)
    assert remote.read().revision == 2
    assert (
        load_replica_state(local)[0].receipt_cursor.request_id
        != pending.request.request_id
    )


def test_preview_with_pending_is_read_only_and_blocked(case, monkeypatch):
    local, remote, home, store = case
    (local / "memory/notes/upload.md").write_text("upload")

    def unavailable(req):
        raise StoreUnavailable("offline")

    monkeypatch.setattr(remote, "commit", unavailable)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        run(case)
    before = (local / PENDING_COMMIT_STATE).read_bytes()
    with pytest.raises(reconcile.WorkspaceReconcileError, match="Pending"):
        reconcile.run_reconciliation(local, remote, home, dry_run=True)
    assert (local / PENDING_COMMIT_STATE).read_bytes() == before


def test_accepted_recovery_needs_no_historical_snapshot_or_payload(case, monkeypatch):
    local, remote, home, store = case
    note = local / "memory/notes/upload.md"
    note.write_text("original")
    original = remote.commit

    def lose(req):
        original(req)
        raise CommitOutcomeUnknown("lost")

    monkeypatch.setattr(remote, "commit", lose)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        run(case)
    pending = store.load()
    uploaded = next(
        m for m in pending.request.mutations if m.id == "memory:notes/upload.md"
    )
    deletion = ResourceMutation(uploaded.id, uploaded.after, None, None)
    original(request(remote, deletion, client="other"))
    note.write_text("new local edit")

    def forbidden(*args):
        raise AssertionError("Historical confirmation fetched current content")

    monkeypatch.setattr(remote, "fetch", forbidden)
    monkeypatch.setattr(remote, "read", forbidden)
    with WorkspaceWriterLock(home):
        assert recover_pending(local, remote, home)
    assert (
        load_replica_state(local)[0].base[uploaded.id].fingerprint
        == uploaded.after.fingerprint
    )
    assert note.read_text() == "new local edit"
    assert store.load() is None


@pytest.mark.parametrize("response", ["corrupt", "wrong-type"])
def test_invalid_resolve_response_stops_without_advancing_base(
    case, monkeypatch, response
):
    local, remote, home, store = case
    (local / "memory/notes/upload.md").write_text("upload")
    original = remote.commit

    def lose(req):
        original(req)
        raise CommitOutcomeUnknown("lost")

    monkeypatch.setattr(remote, "commit", lose)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        run(case)
    pending = store.load()
    receipt = remote.resolve_commit(
        pending.request.expected.sync_id,
        pending.request.client_id,
        pending.request.request_id,
        pending.request.mutation_digest,
    )
    bad = replace(receipt, client_id="wrong") if response == "corrupt" else object()
    monkeypatch.setattr(remote, "resolve_commit", lambda *args: bad)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="digest mismatch"):
        run(case)
    assert store.load() == pending
    assert load_replica_state(local)[0].base == {}


@pytest.mark.parametrize("rejections", [1, 2])
def test_new_commit_cas_replanning_is_bounded_and_uses_fresh_identity(
    case, monkeypatch, rejections
):
    local, remote, _, store = case
    (local / "memory/notes/upload.md").write_text("upload")
    original = remote.commit
    sent = []

    def compete(req):
        sent.append(req)
        assert store.load().request == req
        if len(sent) <= rejections:
            original(
                request(
                    remote, mutation(f"other-{len(sent)}"), client=f"other-{len(sent)}"
                )
            )
        return original(req)

    monkeypatch.setattr(remote, "commit", compete)
    if rejections == 1:
        run(case)
        assert load_replica_state(local)[0].base
    else:
        with pytest.raises(reconcile.WorkspaceReconcileError, match="changed"):
            run(case)
        assert load_replica_state(local)[0].base == {}
    assert len(sent) == 2
    assert sent[0].client_id == sent[1].client_id
    assert sent[0].request_id != sent[1].request_id
    assert store.load() is None
