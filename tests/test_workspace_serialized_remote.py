"""Client adapter over a byte exchange: context validation and failure mapping."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest
from test_workspace_remote_commit import mutation
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.payload import FilePayload, ResourceDescriptor
from aikito.workspace.remote_protocol import (
    SNAPSHOT_EXPIRED,
    STORE_UNAVAILABLE,
    Operation,
    RemoteProtocolHandler,
    encode_commit_response,
    encode_error,
    encode_fetch_response,
    encode_read_response,
    encode_resolve_response,
    encode_success,
)
from aikito.workspace.remote_store import (
    CommitOutcomeUnknown,
    InvalidContent,
    ProtocolError,
    RemoteSnapshot,
    SnapshotExpired,
    StoreUnavailable,
)
from aikito.workspace.remote_wire import build_commit_request, build_commit_result
from aikito.workspace.serialized_remote import (
    SerializedRemoteStore,
    TransportNotDelivered,
)


def seeded():
    store = InMemoryRemote("center")
    request = build_commit_request("client", "request-1", store.read(), (mutation(),))
    store.commit(request)
    return store, request, store.read()


def wire_fetch(payloads):
    body = json.loads(encode_fetch_response(payloads))["body"]
    return encode_success(Operation.FETCH, body)


def raising(exc):
    def exchange(message):
        raise exc

    return exchange


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_round_trips_through_handler():
    store, request, snapshot = seeded()
    adapter = SerializedRemoteStore(RemoteProtocolHandler(store).handle)

    assert adapter.read() == snapshot
    assert adapter.fetch(snapshot, ["opaque-resource"]) == {
        "opaque-resource": mutation().payload
    }
    assert adapter.recover() is False
    assert adapter.resolve_commit(
        request.expected.sync_id,
        request.client_id,
        request.request_id,
        request.mutation_digest,
    ) == build_commit_result(request)


def test_commit_and_replay_through_handler():
    store = InMemoryRemote("center")
    adapter = SerializedRemoteStore(RemoteProtocolHandler(store).handle)
    request = build_commit_request("client", "request-1", store.read(), (mutation(),))
    assert adapter.commit(request) == build_commit_result(request)
    assert adapter.commit(request) == build_commit_result(request)
    assert store.read().revision == 1


# ---------------------------------------------------------------------------
# Response context validation
# ---------------------------------------------------------------------------


def test_fetch_rejects_missing_or_extra_ids():
    descriptor = ResourceDescriptor("fp", "a" * 64)
    expected = RemoteSnapshot("center", 1, {"x": descriptor, "y": descriptor})
    adapter = SerializedRemoteStore(
        lambda message: wire_fetch({"x": FilePayload(b"data")})
    )
    with pytest.raises(ProtocolError):
        adapter.fetch(expected, ["x", "y"])


def test_fetch_rejects_payload_not_matching_expected_hash():
    expected = RemoteSnapshot("center", 1, {"x": ResourceDescriptor("fp", "a" * 64)})
    adapter = SerializedRemoteStore(
        lambda message: wire_fetch({"x": FilePayload(b"data")})
    )
    with pytest.raises(ProtocolError):
        adapter.fetch(expected, ["x"])


def test_operation_mismatch_is_protocol_error_or_unknown_commit():
    store, request, _ = seeded()
    snapshot = store.read()

    def wrong(message):
        return encode_read_response(snapshot)

    with pytest.raises(ProtocolError):
        SerializedRemoteStore(wrong).fetch(snapshot, [])
    with pytest.raises(CommitOutcomeUnknown):
        SerializedRemoteStore(wrong).commit(request)


def test_resolve_receipt_identity_mismatch():
    _, request, snapshot = seeded()
    forged = replace(build_commit_result(request), request_id="other")
    adapter = SerializedRemoteStore(lambda message: encode_resolve_response(forged))
    with pytest.raises(ProtocolError):
        adapter.resolve_commit(
            snapshot.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )


def test_resolve_absent_receipt_is_none():
    adapter = SerializedRemoteStore(lambda message: encode_resolve_response(None))
    assert adapter.resolve_commit("center", "client", "request-1", "a" * 64) is None


def test_forged_commit_result_is_unknown():
    _, request, _ = seeded()
    forged = replace(
        build_commit_result(request),
        accepted_revision=build_commit_result(request).accepted_revision + 1,
    )
    adapter = SerializedRemoteStore(lambda message: encode_commit_response(forged))
    with pytest.raises(CommitOutcomeUnknown):
        adapter.commit(request)


# ---------------------------------------------------------------------------
# Transport and protocol failure mapping
# ---------------------------------------------------------------------------


def test_malformed_response_fails_closed_per_operation():
    _, request, _ = seeded()
    adapter = SerializedRemoteStore(lambda message: b"garbage")
    with pytest.raises(ProtocolError):
        adapter.read()
    with pytest.raises(CommitOutcomeUnknown):
        adapter.commit(request)


def test_transport_not_delivered_is_unavailable_for_all_operations():
    _, request, _ = seeded()
    adapter = SerializedRemoteStore(raising(TransportNotDelivered("lost")))
    with pytest.raises(StoreUnavailable):
        adapter.read()
    with pytest.raises(StoreUnavailable):
        adapter.commit(request)


def test_unknown_transport_failure_is_unknown_only_for_commit():
    _, request, _ = seeded()
    adapter = SerializedRemoteStore(raising(RuntimeError("io")))
    with pytest.raises(StoreUnavailable):
        adapter.read()
    with pytest.raises(CommitOutcomeUnknown):
        adapter.commit(request)


def test_decoded_backend_error_maps_to_stable_code():
    _, request, _ = seeded()
    unavailable = SerializedRemoteStore(
        lambda message: encode_error(Operation.READ, STORE_UNAVAILABLE)
    )
    with pytest.raises(StoreUnavailable):
        unavailable.read()

    expired = SerializedRemoteStore(
        lambda message: encode_error(Operation.COMMIT, SNAPSHOT_EXPIRED)
    )
    with pytest.raises(SnapshotExpired):
        expired.commit(request)


# ---------------------------------------------------------------------------
# Local encoding and replica boundary
# ---------------------------------------------------------------------------


def test_locally_unencodable_commit_never_reaches_transport():
    _, request, _ = seeded()
    calls = []
    adapter = SerializedRemoteStore(lambda message: calls.append(message) or b"")
    forged = replace(request, mutation_digest="0" * 64)
    with pytest.raises(InvalidContent):
        adapter.commit(forged)
    assert calls == []


def test_validate_replica_is_a_noop_without_exchange():
    calls = []
    adapter = SerializedRemoteStore(lambda message: calls.append(message) or b"")
    assert adapter.validate_replica(Path("/tmp/replica")) is None
    assert calls == []
