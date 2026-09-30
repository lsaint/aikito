"""Remote Protocol handler: backend dispatch and stable error mapping."""

from __future__ import annotations

import json

import pytest
from test_workspace_remote_commit import mutation
from workspace_memory_remote import InMemoryRemote

from aikito.workspace import remote_protocol
from aikito.workspace.remote_protocol import (
    COMMIT_OUTCOME_UNKNOWN,
    ERROR_CODES,
    INVALID_CONTENT,
    PROTOCOL_ERROR,
    RECOVERY_REQUIRED,
    REPLICA_HISTORY_MISMATCH,
    REQUEST_IDENTITY_MISMATCH,
    SNAPSHOT_EXPIRED,
    STORE_IDENTITY_MISMATCH,
    STORE_UNAVAILABLE,
    Operation,
    RemoteProtocolHandler,
    decode_response,
    encode_commit_request,
    encode_fetch_request,
    encode_recover_request,
    encode_request,
    encode_resolve_request,
    encode_success,
)
from aikito.workspace.remote_store import (
    CommitOutcomeUnknown,
    InvalidContent,
    ProtocolError,
    RecoveryRequired,
    ReplicaHistoryMismatch,
    RequestIdentityMismatch,
    SnapshotExpired,
    StoreError,
    StoreIdentityMismatch,
    StoreUnavailable,
)
from aikito.workspace.remote_wire import build_commit_request, build_commit_result


class FailingStore:
    """Raise one stored exception from every operation and count dispatches."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = 0

    def _fail(self):
        self.calls += 1
        raise self.exc

    def validate_replica(self, local):
        return None

    def read(self):
        self._fail()

    def fetch(self, expected, ids):
        self._fail()

    def recover(self):
        self._fail()

    def commit(self, request):
        self._fail()

    def resolve_commit(self, *args):
        self._fail()


def commit_request(store: InMemoryRemote):
    return build_commit_request("client", "request-1", store.read(), (mutation(),))


def commit_message(store: InMemoryRemote) -> bytes:
    return encode_commit_request(commit_request(store))


# ---------------------------------------------------------------------------
# Round trips through the handler
# ---------------------------------------------------------------------------


def test_read_and_recover_round_trip():
    store = InMemoryRemote("center")
    handler = RemoteProtocolHandler(store)
    response = decode_response(handler.handle(encode_request(Operation.READ, {})))
    assert response.ok and response.body == store.read()

    recovered = decode_response(handler.handle(encode_recover_request()))
    assert recovered.body is False


def test_commit_fetch_and_resolve_round_trip():
    store = InMemoryRemote("center")
    handler = RemoteProtocolHandler(store)
    req = commit_request(store)

    committed = decode_response(handler.handle(encode_commit_request(req)))
    assert committed.ok
    assert committed.body == build_commit_result(req)

    snapshot = store.read()
    fetched = decode_response(
        handler.handle(encode_fetch_request(snapshot, ["opaque-resource"]))
    )
    assert fetched.body == {"opaque-resource": mutation().payload}

    resolved = decode_response(
        handler.handle(
            encode_resolve_request(
                snapshot.sync_id,
                "client",
                "request-1",
                committed.body.mutation_digest,
            )
        )
    )
    assert resolved.body == committed.body


def test_replayed_commit_returns_original_receipt():
    store = InMemoryRemote("center")
    handler = RemoteProtocolHandler(store)
    message = commit_message(store)
    first = decode_response(handler.handle(message))
    second = decode_response(handler.handle(message))
    assert second.ok and second.body == first.body
    assert store.read().revision == 1


# ---------------------------------------------------------------------------
# Error mapping
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "code"),
    [
        (SnapshotExpired("stale"), SNAPSHOT_EXPIRED),
        (InvalidContent("bad content"), INVALID_CONTENT),
        (RecoveryRequired("recover"), RECOVERY_REQUIRED),
        (StoreUnavailable("down"), STORE_UNAVAILABLE),
        (CommitOutcomeUnknown("unknown"), COMMIT_OUTCOME_UNKNOWN),
        (RequestIdentityMismatch("identity"), REQUEST_IDENTITY_MISMATCH),
        (StoreIdentityMismatch("center"), STORE_IDENTITY_MISMATCH),
        (ReplicaHistoryMismatch("history"), REPLICA_HISTORY_MISMATCH),
        (ProtocolError("wire"), PROTOCOL_ERROR),
    ],
)
def test_store_error_maps_to_exact_code(exc, code):
    handler = RemoteProtocolHandler(FailingStore(exc))
    response = decode_response(handler.handle(encode_request(Operation.READ, {})))
    assert not response.ok and response.error_code == code


def test_generic_backend_failure_is_safe():
    # An unknown backend exception during commit cannot imply a definite rejection.
    handler = RemoteProtocolHandler(FailingStore(RuntimeError("boom")))
    response = decode_response(handler.handle(commit_message(InMemoryRemote("c"))))
    assert response.error_code == COMMIT_OUTCOME_UNKNOWN

    # A non-commit failure is reported as unavailable, never as a CAS rejection.
    response = decode_response(handler.handle(encode_request(Operation.READ, {})))
    assert response.error_code == STORE_UNAVAILABLE

    # Base StoreError is also a safe generic, not a definite rejection.
    handler = RemoteProtocolHandler(FailingStore(StoreError("base")))
    response = decode_response(handler.handle(encode_request(Operation.READ, {})))
    assert response.error_code == STORE_UNAVAILABLE


def test_backend_payload_error_is_not_leaked():
    handler = RemoteProtocolHandler(FailingStore(ValueError("local /tmp/path")))
    response = decode_response(handler.handle(encode_request(Operation.READ, {})))
    assert response.error_code in ERROR_CODES
    assert "/tmp/path" not in json.dumps(response.error_code)


# ---------------------------------------------------------------------------
# Malformed input never reaches the backend
# ---------------------------------------------------------------------------


def test_malformed_input_is_not_dispatched():
    store = FailingStore(RuntimeError("should never run"))
    handler = RemoteProtocolHandler(store)
    for message in (
        b"not json",
        b"",
        b'{"protocol":1,"operation":"read"}',
        b'{"protocol":1,"operation":"read","body":{"extra":1}}',
        encode_success(Operation.READ, {}),  # a response is not a request
        b'{"body":{},"operation":"teleport","protocol":1}',
    ):
        response = decode_response(handler.handle(message))
        assert not response.ok and response.error_code == PROTOCOL_ERROR
    assert store.calls == 0


def test_error_response_attributes_operation_when_identifiable():
    handler = RemoteProtocolHandler(InMemoryRemote())
    response = decode_response(
        handler.handle(b'{"protocol":1,"operation":"read","body":{"unexpected":1}}')
    )
    assert response.error_code == PROTOCOL_ERROR and response.operation == "read"

    # Unknown protocol version still echoes the recognized operation.
    response = decode_response(
        handler.handle(b'{"protocol":2,"operation":"read","body":{}}')
    )
    assert response.error_code == PROTOCOL_ERROR and response.operation == "read"

    # Unparseable or unidentified messages report a null operation.
    response = decode_response(handler.handle(b"not json"))
    assert response.error_code == PROTOCOL_ERROR and response.operation is None

    response = decode_response(
        handler.handle(b'{"protocol":1,"operation":"teleport","body":{}}')
    )
    assert response.error_code == PROTOCOL_ERROR and response.operation is None


def test_handler_handles_non_bytes_input():
    handler = RemoteProtocolHandler(InMemoryRemote())
    response = decode_response(handler.handle(None))
    assert response.error_code == PROTOCOL_ERROR and response.operation is None


# ---------------------------------------------------------------------------
# Backend results are validated before encoding
# ---------------------------------------------------------------------------


class ReturningStore(InMemoryRemote):
    """Return one forged value from a chosen operation after the real call."""

    def __init__(self, operation, value):
        super().__init__("center")
        self._operation, self._value = operation, value

    def read(self):
        snapshot = super().read()
        return self._value if self._operation == Operation.READ else snapshot

    def fetch(self, expected, ids):
        payloads = super().fetch(expected, ids)
        return self._value if self._operation == Operation.FETCH else payloads

    def recover(self):
        recovered = super().recover()
        return self._value if self._operation == Operation.RECOVER else recovered

    def commit(self, request):
        result = super().commit(request)
        return self._value if self._operation == Operation.COMMIT else result

    def resolve_commit(self, *args):
        result = super().resolve_commit(*args)
        return self._value if self._operation == Operation.RESOLVE_COMMIT else result


def message_for(operation, store):
    if operation == Operation.READ:
        return encode_request(Operation.READ, {})
    if operation == Operation.RECOVER:
        return encode_recover_request()
    if operation == Operation.FETCH:
        return encode_fetch_request(store.read(), ())
    if operation == Operation.COMMIT:
        return commit_message(store)
    request = commit_request(store)
    return encode_resolve_request(
        "center", request.client_id, request.request_id, request.mutation_digest
    )


@pytest.mark.parametrize(
    ("operation", "value", "code"),
    [
        (Operation.READ, None, STORE_UNAVAILABLE),
        (Operation.READ, {"sync_id": "center"}, STORE_UNAVAILABLE),
        (Operation.FETCH, None, STORE_UNAVAILABLE),
        (Operation.FETCH, {"id": b"raw"}, STORE_UNAVAILABLE),
        (Operation.RECOVER, None, STORE_UNAVAILABLE),
        (Operation.RECOVER, 1, STORE_UNAVAILABLE),
        (Operation.RESOLVE_COMMIT, "receipt", STORE_UNAVAILABLE),
        (Operation.COMMIT, None, COMMIT_OUTCOME_UNKNOWN),
        (Operation.COMMIT, {"accepted_revision": 1}, COMMIT_OUTCOME_UNKNOWN),
    ],
)
def test_invalid_backend_result_is_a_safe_server_fault(operation, value, code):
    store = ReturningStore(operation, value)
    response = decode_response(
        RemoteProtocolHandler(store).handle(message_for(operation, store))
    )
    assert not response.ok and response.operation == operation
    assert response.error_code == code


def test_unencodable_commit_result_after_publication_is_unknown(monkeypatch):
    # The commit is already durable; failing to encode its receipt must not look
    # like a malformed request or a definite rejection.
    store = InMemoryRemote("center")
    original = remote_protocol._response_body

    def failing(operation, body):
        if operation == Operation.COMMIT:
            raise ProtocolError("encoding failed")
        return original(operation, body)

    monkeypatch.setattr(remote_protocol, "_response_body", failing)
    response = decode_response(
        RemoteProtocolHandler(store).handle(commit_message(store))
    )
    assert store.read().revision == 1
    assert response.error_code == COMMIT_OUTCOME_UNKNOWN
