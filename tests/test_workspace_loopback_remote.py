"""Loopback transport: a real bytes boundary with injectable responses."""

from __future__ import annotations

import json

import pytest
from test_workspace_remote_commit import mutation
from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.remote_protocol import (
    PROTOCOL_ERROR,
    Operation,
    encode_error,
    encode_read_response,
)
from aikito.workspace.remote_store import (
    CommitOutcomeUnknown,
    ProtocolError,
    RemoteSnapshot,
    StoreUnavailable,
)
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.serialized_remote import SerializedRemoteStore


def canonical(raw) -> bytes:
    return json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def adapter_and_transport(store=None):
    store = InMemoryRemote("center") if store is None else store
    transport = LoopbackTransport(store)
    return SerializedRemoteStore(transport.exchange), transport, store


def test_records_raw_bytes_on_both_sides():
    adapter, transport, store = adapter_and_transport()
    assert adapter.read() == store.read()
    assert len(transport.requests) == 1 and len(transport.responses) == 1
    request = json.loads(transport.requests[0])
    response = json.loads(transport.responses[0])
    assert request["operation"] == Operation.READ and request["body"] == {}
    assert response["ok"] is True
    assert all(type(item) is bytes for item in transport.requests + transport.responses)


def test_commit_payload_crosses_as_encoded_bytes():
    adapter, transport, store = adapter_and_transport()
    request = build_commit_request("client", "request-1", store.read(), (mutation(),))
    adapter.commit(request)
    body = json.loads(transport.requests[-1])["body"]
    assert body["version"] == 1
    payload = body["mutations"][0]["payload"]
    assert set(payload) == {"content_hash", "data"}
    assert payload["content_hash"] == body["mutations"][0]["after"]["content_hash"]
    assert "FilePayload" not in transport.requests[-1].decode("utf-8")


def test_injected_malformed_response_fails_closed_then_recovers():
    adapter, transport, store = adapter_and_transport()
    transport.inject(b"garbage")
    with pytest.raises(ProtocolError):
        adapter.read()
    assert adapter.read() == store.read()

    transport.inject(
        canonical(
            {
                "protocol": 1,
                "operation": "read",
                "ok": False,
                "error": {"code": "made_up", "message": "boom"},
            }
        )
    )
    with pytest.raises(ProtocolError):
        adapter.read()


def test_injected_wrong_operation_is_rejected():
    store = InMemoryRemote("center")
    adapter, transport, _ = adapter_and_transport(store)
    snapshot = store.read()
    transport.inject(encode_read_response(snapshot))
    with pytest.raises(ProtocolError):
        adapter.fetch(snapshot, [])

    transport.inject(encode_read_response(snapshot))
    request = build_commit_request("client", "r", store.read(), (mutation(),))
    with pytest.raises(CommitOutcomeUnknown):
        adapter.commit(request)


def test_injected_decoded_error_maps_to_store_error():
    adapter, transport, _ = adapter_and_transport()
    transport.inject(encode_error(Operation.READ, "store_unavailable"))
    with pytest.raises(StoreUnavailable):
        adapter.read()


def test_recover_returns_boolean_through_boundary():
    adapter, transport, _ = adapter_and_transport()
    assert adapter.recover() is False
    assert json.loads(transport.requests[-1])["operation"] == Operation.RECOVER
    assert json.loads(transport.responses[-1])["body"] == {"recovered": False}


def test_read_snapshot_carries_no_payload_fields():
    adapter, transport, _ = adapter_and_transport()
    assert adapter.read() == RemoteSnapshot("center", 0, {})
    body = json.loads(transport.responses[-1])["body"]
    assert set(body) == {"sync_id", "revision", "resources"}


def test_protocol_error_code_round_trips_through_boundary():
    adapter, transport, _ = adapter_and_transport()
    transport.inject(encode_error(None, PROTOCOL_ERROR))
    with pytest.raises(ProtocolError):
        adapter.read()
