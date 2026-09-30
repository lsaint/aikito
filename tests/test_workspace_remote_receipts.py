"""Latest receipt contract shared by durable and in-memory stores."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE
from aikito.workspace.remote_store import (
    InvalidContent,
    ReceiptCursor,
    ReplicaHistoryMismatch,
    RequestIdentityMismatch,
    SnapshotExpired,
    StoreIdentityMismatch,
)
from aikito.workspace.remote_wire import (
    build_commit_request,
    commit_request_digest,
    validate_commit_result,
)
from workspace_memory_remote import InMemoryRemote


@pytest.fixture(params=["memory", "filesystem"])
def store(request, tmp_path):
    return (
        InMemoryRemote()
        if request.param == "memory"
        else FilesystemRemote.create(tmp_path / "center")
    )


def mutation(name="one", data=b"one", before=None):
    payload = FilePayload(data)
    return ResourceMutation(
        f"memory:notes/{name}.md",
        before,
        ResourceDescriptor(hashlib.sha256(data).hexdigest(), payload_hash(payload)),
        payload,
    )


def request(
    store, *batch, client="client-a", identity="request-a", cursor=None, expected=None
):
    return build_commit_request(
        client,
        identity,
        expected or store.read(),
        batch or (mutation(),),
        previous_receipt=cursor,
    )


def resolve(store, req):
    return store.resolve_commit(
        req.expected.sync_id, req.client_id, req.request_id, req.mutation_digest
    )


def cursor(receipt):
    return ReceiptCursor(receipt.request_id, receipt.mutation_digest)


def checkpoint(store):
    snapshot = store.read()
    return snapshot, store.fetch(snapshot, snapshot.resources)


def test_duplicate_returns_original_before_cas_and_survives_other_clients(store):
    req = request(store)
    assert resolve(store, req) is None
    receipt = store.commit(req)
    validate_commit_result(req, receipt)
    for i in range(3):
        store.commit(
            request(store, mutation(f"b-{i}"), client=f"other-{i}", identity=f"b-{i}")
        )
    before = checkpoint(store)
    assert store.commit(req) == receipt == resolve(store, req)
    assert receipt.accepted_revision == req.expected.revision + 1
    assert before == checkpoint(store)


def test_latest_receipt_replacement_rejects_original_old_request(store):
    req = request(store)
    first = store.commit(req)
    updated = mutation(data=b"updated", before=req.mutations[0].after)
    next_req = request(store, updated, identity="request-next", cursor=cursor(first))
    second = store.commit(next_req)
    assert resolve(store, next_req) == second
    assert resolve(store, req) is None
    before = checkpoint(store)
    with pytest.raises(SnapshotExpired):
        store.commit(req)
    assert checkpoint(store) == before


def test_matching_id_with_different_valid_body_is_protocol_error(store):
    req = request(store)
    store.commit(req)
    different = request(store, mutation(data=b"other"), expected=req.expected)
    before = checkpoint(store)
    with pytest.raises(RequestIdentityMismatch):
        store.commit(different)
    with pytest.raises(RequestIdentityMismatch):
        resolve(store, different)
    missing = replace(different, request_id="another-request")
    assert resolve(store, missing) is None
    assert checkpoint(store) == before


def test_divergent_history_is_rejected_without_receipt_replacement(store):
    req = request(store)
    first = store.commit(req)
    stale = request(store, mutation("another"), identity="request-next")
    before = checkpoint(store)
    with pytest.raises(ReplicaHistoryMismatch):
        store.commit(stale)
    assert resolve(store, req) == first
    assert checkpoint(store) == before
    valid = request(
        store, mutation("another"), identity="request-next", cursor=cursor(first)
    )
    validate_commit_result(valid, store.commit(valid))


def test_cursor_without_remote_history_is_rejected(store):
    req = request(store, cursor=ReceiptCursor("lost", "a" * 64))
    before = checkpoint(store)
    with pytest.raises(ReplicaHistoryMismatch):
        store.commit(req)
    assert checkpoint(store) == before


def test_center_identity_error_is_distinct_from_cas(store):
    req = request(store)
    store.commit(req)
    other = request(store, expected=replace(req.expected, sync_id="other-center"))
    with pytest.raises(StoreIdentityMismatch):
        store.commit(other)
    with pytest.raises(StoreIdentityMismatch):
        resolve(store, other)


def test_concurrent_duplicate_delivery_accepts_once(store):
    req = request(store)
    ready = threading.Barrier(2)

    def submit(_):
        ready.wait(timeout=10)
        return store.commit(req)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, range(2)))
    assert results[0] == results[1] == resolve(store, req)
    assert store.read().revision == req.expected.revision + 1


def test_delete_has_the_same_receipt_and_retry_semantics(store):
    req = request(store)
    first = store.commit(req)
    delete = ResourceMutation(req.mutations[0].id, req.mutations[0].after, None, None)
    deletion = request(store, delete, identity="delete", cursor=cursor(first))
    result = store.commit(deletion)
    assert store.read().resources == {}
    assert store.commit(deletion) == resolve(store, deletion) == result
    assert store.read().revision == first.accepted_revision + 1


def test_historical_receipt_does_not_need_historical_payload(store):
    req = request(store)
    first = store.commit(req)
    deletion = ResourceMutation(req.mutations[0].id, req.mutations[0].after, None, None)
    store.commit(request(store, deletion, client="another-client", identity="delete"))
    assert store.read().resources == {}
    assert store.commit(req) == resolve(store, req) == first


def test_legacy_batch_preserves_receipts_without_creating_new_ones(store):
    req = request(store)
    receipt = store.commit(req)
    store.commit(store.read(), (mutation("legacy"),))
    assert resolve(store, req) == receipt
    assert store.commit(req) == receipt


@pytest.mark.parametrize("corruption", ["empty", "payload", "digest"])
def test_untrusted_request_validation_precedes_receipt_lookup(store, corruption):
    req = request(store)
    receipt = store.commit(req)
    bad = replace(req)
    if corruption == "empty":
        object.__setattr__(bad, "mutations", ())
        object.__setattr__(bad, "mutation_digest", commit_request_digest(bad))
    elif corruption == "payload":
        bad_mutation = replace(req.mutations[0])
        object.__setattr__(bad_mutation, "payload", FilePayload(b"corrupted"))
        object.__setattr__(bad, "mutations", (bad_mutation,))
        object.__setattr__(bad, "mutation_digest", commit_request_digest(bad))
    else:
        object.__setattr__(bad, "mutation_digest", "a" * 64)
    before = checkpoint(store)
    with pytest.raises(InvalidContent):
        store.commit(bad)
    assert resolve(store, req) == receipt
    assert checkpoint(store) == before


def test_filesystem_receipt_survives_new_instance(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    req = request(remote)
    receipt = remote.commit(req)
    reopened = FilesystemRemote(remote.root)
    assert reopened.commit(req) == resolve(reopened, req) == receipt
    assert reopened.read().revision == 1
    raw = json.loads((remote.root / REMOTE_STATE).read_text())
    assert set(raw["receipts"]["latest"]) == {req.client_id}


@pytest.mark.parametrize("bad", ["version", "revision", "client", "sync", "null"])
def test_filesystem_corrupt_receipt_blocks_resolution(tmp_path, bad):
    remote = FilesystemRemote.create(tmp_path / "center")
    req = request(remote)
    remote.commit(req)
    path = remote.root / REMOTE_STATE
    raw = json.loads(path.read_text())
    value = raw["receipts"]["latest"][req.client_id]
    if bad == "version":
        value["version"] = 99
    elif bad == "revision":
        value["accepted_revision"] = 99
    elif bad == "client":
        value["client_id"] = "another"
    elif bad == "sync":
        value["sync_id"] = "another"
    else:
        raw["receipts"] = None
    path.write_text(json.dumps(raw))
    with pytest.raises(InvalidContent):
        resolve(FilesystemRemote(remote.root), req)
