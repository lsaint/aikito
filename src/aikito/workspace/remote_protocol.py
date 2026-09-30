"""Transport-neutral Remote Protocol v1 codec.

This module turns portable domain objects into canonical envelope bytes and
back. It knows the wire schema and the domain objects it carries; it knows
nothing about transport, HTTP, local paths or storage backends.

The envelope carries its own version in ``protocol``. Operation bodies may
carry a separate ``version`` (commit uses ``COMMIT_ENCODING_VERSION``); the
decoder always dispatches on ``operation`` before interpreting a body version,
so the two version domains stay independent.
"""

from __future__ import annotations

import base64
import json
import re
from dataclasses import dataclass
from types import MappingProxyType

from .payload import (
    PayloadError,
    ResourceDescriptor,
    ResourceMutation,
    decode_payload,
    encode_payload,
    payload_hash,
)
from .remote_store import (
    CommitOutcomeUnknown,
    CommitRequest,
    CommitResult,
    InvalidContent,
    ProtocolError,
    ReceiptCursor,
    RecoveryRequired,
    RemoteSnapshot,
    RemoteStore,
    ReplicaHistoryMismatch,
    RequestIdentityMismatch,
    SnapshotExpired,
    StoreError,
    StoreIdentityMismatch,
    StoreUnavailable,
)
from .remote_wire import (
    COMMIT_ENCODING_VERSION,
    decode_base64,
    decode_receipt,
    decode_state_json,
    encode_receipt,
    validate_commit_request,
)

REMOTE_PROTOCOL_VERSION = 1


class Operation:
    """Stable wire names for the RemoteStore operations."""

    READ = "read"
    FETCH = "fetch"
    COMMIT = "commit"
    RESOLVE_COMMIT = "resolve_commit"
    RECOVER = "recover"


OPERATIONS = frozenset(
    {
        Operation.READ,
        Operation.FETCH,
        Operation.COMMIT,
        Operation.RESOLVE_COMMIT,
        Operation.RECOVER,
    }
)

SNAPSHOT_EXPIRED = "snapshot_expired"
INVALID_CONTENT = "invalid_content"
RECOVERY_REQUIRED = "recovery_required"
STORE_UNAVAILABLE = "store_unavailable"
COMMIT_OUTCOME_UNKNOWN = "commit_outcome_unknown"
REQUEST_IDENTITY_MISMATCH = "request_identity_mismatch"
STORE_IDENTITY_MISMATCH = "store_identity_mismatch"
REPLICA_HISTORY_MISMATCH = "replica_history_mismatch"
PROTOCOL_ERROR = "protocol_error"

ERROR_CODES = frozenset(
    {
        SNAPSHOT_EXPIRED,
        INVALID_CONTENT,
        RECOVERY_REQUIRED,
        STORE_UNAVAILABLE,
        COMMIT_OUTCOME_UNKNOWN,
        REQUEST_IDENTITY_MISMATCH,
        STORE_IDENTITY_MISMATCH,
        REPLICA_HISTORY_MISMATCH,
        PROTOCOL_ERROR,
    }
)

# Fixed safe text per code; never forward str(exc) or local detail.
ERROR_MESSAGES = MappingProxyType(
    {
        SNAPSHOT_EXPIRED: "Snapshot preconditions failed",
        INVALID_CONTENT: "Stored or submitted content is invalid",
        RECOVERY_REQUIRED: "Backend recovery is required before planning",
        STORE_UNAVAILABLE: "Remote store is unavailable",
        COMMIT_OUTCOME_UNKNOWN: "Commit outcome could not be confirmed",
        REQUEST_IDENTITY_MISMATCH: "Request identity matches a different commit digest",
        STORE_IDENTITY_MISMATCH: "Remote store identity does not match",
        REPLICA_HISTORY_MISMATCH: "Previous receipt does not match accepted history",
        PROTOCOL_ERROR: "Protocol message is malformed or unsupported",
    }
)

# Client-side decoding of a stable code.
ERROR_EXCEPTIONS = MappingProxyType(
    {
        SNAPSHOT_EXPIRED: SnapshotExpired,
        INVALID_CONTENT: InvalidContent,
        RECOVERY_REQUIRED: RecoveryRequired,
        STORE_UNAVAILABLE: StoreUnavailable,
        COMMIT_OUTCOME_UNKNOWN: CommitOutcomeUnknown,
        REQUEST_IDENTITY_MISMATCH: RequestIdentityMismatch,
        STORE_IDENTITY_MISMATCH: StoreIdentityMismatch,
        REPLICA_HISTORY_MISMATCH: ReplicaHistoryMismatch,
        PROTOCOL_ERROR: ProtocolError,
    }
)

_DIGEST = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class ProtocolRequest:
    operation: str
    body: object


@dataclass(frozen=True)
class ProtocolResponse:
    operation: str | None
    ok: bool
    body: object = None
    error_code: str | None = None


def _canonical(body: object) -> bytes:
    try:
        return json.dumps(
            body,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise ProtocolError("Invalid protocol encoding") from exc


def _load(encoded: bytes) -> object:
    if type(encoded) is not bytes:
        raise ProtocolError("Protocol message requires bytes")
    try:
        return decode_state_json(encoded)
    except InvalidContent as exc:
        raise ProtocolError("Invalid protocol JSON") from exc


def _object(raw: object, fields: set[str]) -> dict:
    if type(raw) is not dict or set(raw) != fields:
        raise ProtocolError("Invalid protocol fields")
    return raw


def _identity(value: object) -> str:
    if type(value) is not str or not value:
        raise ProtocolError("Invalid protocol identity")
    return value


def _digest(value: object) -> str:
    if type(value) is not str or not _DIGEST.fullmatch(value):
        raise ProtocolError("Invalid protocol digest")
    return value


# ---------------------------------------------------------------------------
# Envelope
# ---------------------------------------------------------------------------


def encode_request(operation: str, body: object) -> bytes:
    if operation not in OPERATIONS:
        raise ProtocolError("Unsupported protocol operation")
    return _canonical(
        {"protocol": REMOTE_PROTOCOL_VERSION, "operation": operation, "body": body}
    )


def encode_success(operation: str, body: object) -> bytes:
    if operation not in OPERATIONS:
        raise ProtocolError("Unsupported protocol operation")
    return _canonical(
        {
            "protocol": REMOTE_PROTOCOL_VERSION,
            "operation": operation,
            "ok": True,
            "body": body,
        }
    )


def encode_error(operation: str | None, code: str) -> bytes:
    if operation is not None and operation not in OPERATIONS:
        raise ProtocolError("Unsupported protocol operation")
    if code not in ERROR_CODES:
        raise ProtocolError("Unsupported protocol error code")
    return _canonical(
        {
            "protocol": REMOTE_PROTOCOL_VERSION,
            "operation": operation,
            "ok": False,
            "error": {"code": code, "message": ERROR_MESSAGES[code]},
        }
    )


def _check_protocol(raw: dict) -> None:
    if type(raw["protocol"]) is not int or raw["protocol"] != REMOTE_PROTOCOL_VERSION:
        raise ProtocolError("Unsupported remote protocol version")


# ---------------------------------------------------------------------------
# Descriptor and snapshot codec
# ---------------------------------------------------------------------------


def _descriptor(descriptor: ResourceDescriptor) -> dict:
    return {
        "fingerprint": descriptor.fingerprint,
        "content_hash": descriptor.content_hash,
        "references": list(descriptor.references),
        "mode_fingerprint": descriptor.mode_fingerprint,
    }


def _decode_descriptor(raw: object) -> ResourceDescriptor | None:
    if raw is None:
        return None
    value = _object(
        raw, {"fingerprint", "content_hash", "references", "mode_fingerprint"}
    )
    if type(value["references"]) is not list:
        raise ProtocolError("Invalid descriptor references")
    try:
        return ResourceDescriptor(
            value["fingerprint"],
            value["content_hash"],
            tuple(value["references"]),
            value["mode_fingerprint"],
        )
    except PayloadError as exc:
        raise ProtocolError("Invalid resource descriptor") from exc


def _snapshot(snapshot: RemoteSnapshot) -> dict:
    return {
        "sync_id": snapshot.sync_id,
        "revision": snapshot.revision,
        "resources": {
            key: _descriptor(value) for key, value in snapshot.resources.items()
        },
    }


def _decode_snapshot(raw: object) -> RemoteSnapshot:
    value = _object(raw, {"sync_id", "revision", "resources"})
    if type(value["resources"]) is not dict:
        raise ProtocolError("Invalid snapshot resources")
    resources = {}
    for key, descriptor in value["resources"].items():
        decoded = _decode_descriptor(descriptor)
        if decoded is None:
            raise ProtocolError("Snapshot descriptor cannot be absent")
        resources[key] = decoded
    try:
        return RemoteSnapshot(value["sync_id"], value["revision"], resources)
    except InvalidContent as exc:
        raise ProtocolError("Invalid remote snapshot") from exc


# ---------------------------------------------------------------------------
# Payload and mutation codec
# ---------------------------------------------------------------------------


def _encode_wire_payload(payload) -> dict:
    return {
        "content_hash": payload_hash(payload),
        "data": base64.b64encode(encode_payload(payload)).decode("ascii"),
    }


def _decode_wire_payload(raw: object):
    value = _object(raw, {"content_hash", "data"})
    try:
        encoded = decode_base64(value["data"])
        return decode_payload(encoded, value["content_hash"])
    except (InvalidContent, PayloadError) as exc:
        raise ProtocolError("Invalid wire payload") from exc


def _mutation(mutation: ResourceMutation) -> dict:
    return {
        "id": mutation.id,
        "before": None if mutation.before is None else _descriptor(mutation.before),
        "after": None if mutation.after is None else _descriptor(mutation.after),
        "payload": None
        if mutation.payload is None
        else base64.b64encode(encode_payload(mutation.payload)).decode("ascii"),
    }


def _decode_mutation(raw: object) -> ResourceMutation:
    value = _object(raw, {"id", "before", "after", "payload"})
    before = _decode_descriptor(value["before"])
    after = _decode_descriptor(value["after"])
    payload = None
    if value["payload"] is not None:
        if after is None or type(value["payload"]) is not str:
            raise ProtocolError("Invalid mutation payload presence")
        try:
            payload = decode_payload(
                decode_base64(value["payload"]), after.content_hash
            )
        except (InvalidContent, PayloadError) as exc:
            raise ProtocolError("Invalid mutation payload") from exc
    try:
        return ResourceMutation(value["id"], before, after, payload)
    except PayloadError as exc:
        raise ProtocolError("Invalid resource mutation") from exc


# ---------------------------------------------------------------------------
# Operation bodies
# ---------------------------------------------------------------------------


def _request_body(operation: str, body: object) -> dict:
    if operation in (Operation.READ, Operation.RECOVER):
        return {}
    if operation == Operation.FETCH:
        expected, ids = body
        return {
            "expected": _snapshot(expected),
            "ids": sorted({_identity(item) for item in ids}),
        }
    if operation == Operation.COMMIT:
        return _commit_request_body(body)
    if operation == Operation.RESOLVE_COMMIT:
        sync_id, client_id, request_id, mutation_digest = body
        return {
            "sync_id": sync_id,
            "client_id": client_id,
            "request_id": request_id,
            "mutation_digest": mutation_digest,
        }
    raise ProtocolError("Unsupported protocol operation")


def _decode_request_body(operation: str, raw: object) -> object:
    if operation in (Operation.READ, Operation.RECOVER):
        _object(raw, set())
        return None
    if operation == Operation.FETCH:
        value = _object(raw, {"expected", "ids"})
        if type(value["ids"]) is not list:
            raise ProtocolError("Invalid fetch ID list")
        ids = tuple(sorted({_identity(item) for item in value["ids"]}))
        return _decode_snapshot(value["expected"]), ids
    if operation == Operation.COMMIT:
        return _decode_commit_request_body(raw)
    if operation == Operation.RESOLVE_COMMIT:
        value = _object(raw, {"sync_id", "client_id", "request_id", "mutation_digest"})
        return (
            _identity(value["sync_id"]),
            _identity(value["client_id"]),
            _identity(value["request_id"]),
            _digest(value["mutation_digest"]),
        )
    raise ProtocolError("Unsupported protocol operation")


def _response_body(operation: str, body: object) -> dict:
    if operation == Operation.READ:
        return _snapshot(body)
    if operation == Operation.FETCH:
        return {"payloads": {key: _encode_wire_payload(v) for key, v in body.items()}}
    if operation == Operation.COMMIT:
        return encode_receipt(body)
    if operation == Operation.RESOLVE_COMMIT:
        return {"result": None if body is None else encode_receipt(body)}
    if operation == Operation.RECOVER:
        return {"recovered": body}
    raise ProtocolError("Unsupported protocol operation")


def _decode_response_body(operation: str, raw: object) -> object:
    if operation == Operation.READ:
        return _decode_snapshot(raw)
    if operation == Operation.FETCH:
        value = _object(raw, {"payloads"})
        if type(value["payloads"]) is not dict:
            raise ProtocolError("Invalid fetch payloads")
        return {
            _identity(key): _decode_wire_payload(item)
            for key, item in value["payloads"].items()
        }
    if operation == Operation.COMMIT:
        return _decode_commit_result_body(raw)
    if operation == Operation.RESOLVE_COMMIT:
        value = _object(raw, {"result"})
        if value["result"] is None:
            return None
        return _decode_commit_result_body(value["result"])
    if operation == Operation.RECOVER:
        value = _object(raw, {"recovered"})
        if type(value["recovered"]) is not bool:
            raise ProtocolError("Invalid recover result")
        return value["recovered"]
    raise ProtocolError("Unsupported protocol operation")


# ---------------------------------------------------------------------------
# Commit codec
# ---------------------------------------------------------------------------


def _commit_request_body(request: CommitRequest) -> dict:
    try:
        validate_commit_request(request)
    except InvalidContent as exc:
        raise ProtocolError("Invalid commit request") from exc
    cursor = request.previous_receipt
    return {
        "version": COMMIT_ENCODING_VERSION,
        "client_id": request.client_id,
        "request_id": request.request_id,
        "mutation_digest": request.mutation_digest,
        "previous_receipt": None
        if cursor is None
        else {
            "request_id": cursor.request_id,
            "mutation_digest": cursor.mutation_digest,
        },
        "expected": _snapshot(request.expected),
        "mutations": [_mutation(item) for item in request.mutations],
    }


def _decode_commit_request_body(raw: object) -> CommitRequest:
    value = _object(
        raw,
        {
            "version",
            "client_id",
            "request_id",
            "mutation_digest",
            "previous_receipt",
            "expected",
            "mutations",
        },
    )
    if type(value["version"]) is not int or value["version"] != COMMIT_ENCODING_VERSION:
        raise ProtocolError("Unsupported commit encoding version")
    cursor = None
    if value["previous_receipt"] is not None:
        previous = _object(value["previous_receipt"], {"request_id", "mutation_digest"})
        cursor = ReceiptCursor(
            _identity(previous["request_id"]), _digest(previous["mutation_digest"])
        )
    if type(value["mutations"]) is not list:
        raise ProtocolError("Invalid commit mutation batch")
    try:
        request = CommitRequest(
            _identity(value["client_id"]),
            _identity(value["request_id"]),
            cursor,
            _decode_snapshot(value["expected"]),
            tuple(_decode_mutation(item) for item in value["mutations"]),
            _digest(value["mutation_digest"]),
        )
        validate_commit_request(request)
        return request
    except (InvalidContent, PayloadError) as exc:
        raise ProtocolError("Invalid commit request") from exc


def _decode_commit_result_body(raw: object) -> CommitResult:
    try:
        return decode_receipt(raw)
    except InvalidContent as exc:
        raise ProtocolError("Invalid commit result") from exc


# ---------------------------------------------------------------------------
# Typed request encoders
# ---------------------------------------------------------------------------


def encode_read_request() -> bytes:
    return encode_request(Operation.READ, {})


def encode_fetch_request(expected: RemoteSnapshot, ids) -> bytes:
    return encode_request(
        Operation.FETCH, _request_body(Operation.FETCH, (expected, ids))
    )


def encode_commit_request(request: CommitRequest) -> bytes:
    return encode_request(Operation.COMMIT, _commit_request_body(request))


def encode_resolve_request(
    sync_id: str, client_id: str, request_id: str, mutation_digest: str
) -> bytes:
    return encode_request(
        Operation.RESOLVE_COMMIT,
        _request_body(
            Operation.RESOLVE_COMMIT,
            (sync_id, client_id, request_id, mutation_digest),
        ),
    )


def encode_recover_request() -> bytes:
    return encode_request(Operation.RECOVER, {})


# ---------------------------------------------------------------------------
# Typed response encoders
# ---------------------------------------------------------------------------


def encode_read_response(snapshot: RemoteSnapshot) -> bytes:
    return encode_success(Operation.READ, _snapshot(snapshot))


def encode_fetch_response(payloads) -> bytes:
    return encode_success(Operation.FETCH, _response_body(Operation.FETCH, payloads))


def encode_commit_response(result: CommitResult) -> bytes:
    return encode_success(Operation.COMMIT, encode_receipt(result))


def encode_resolve_response(result: CommitResult | None) -> bytes:
    return encode_success(
        Operation.RESOLVE_COMMIT, _response_body(Operation.RESOLVE_COMMIT, result)
    )


def encode_recover_response(recovered: bool) -> bytes:
    if type(recovered) is not bool:
        raise ProtocolError("Invalid recover result")
    return encode_success(Operation.RECOVER, {"recovered": recovered})


# ---------------------------------------------------------------------------
# Decoders
# ---------------------------------------------------------------------------


def decode_request(encoded: bytes) -> ProtocolRequest:
    raw = _load(encoded)
    value = _object(raw, {"protocol", "operation", "body"})
    _check_protocol(value)
    operation = value["operation"]
    if operation not in OPERATIONS:
        raise ProtocolError("Unsupported protocol operation")
    body = _decode_request_body(operation, value["body"])
    if encode_request(operation, _request_body(operation, body)) != encoded:
        raise ProtocolError("Noncanonical protocol request")
    return ProtocolRequest(operation, body)


def decode_response(encoded: bytes) -> ProtocolResponse:
    raw = _load(encoded)
    if type(raw) is not dict or "ok" not in raw:
        raise ProtocolError("Invalid protocol response")
    if type(raw["ok"]) is not bool:
        raise ProtocolError("Invalid protocol response status")
    if raw["ok"]:
        value = _object(raw, {"protocol", "operation", "ok", "body"})
        _check_protocol(value)
        operation = value["operation"]
        if operation not in OPERATIONS:
            raise ProtocolError("Unsupported protocol operation")
        body = _decode_response_body(operation, value["body"])
        if encode_success(operation, _response_body(operation, body)) != encoded:
            raise ProtocolError("Noncanonical protocol response")
        return ProtocolResponse(operation, True, body=body)
    value = _object(raw, {"protocol", "operation", "ok", "error"})
    _check_protocol(value)
    operation = value["operation"]
    if operation is not None and operation not in OPERATIONS:
        raise ProtocolError("Unsupported protocol operation")
    error = _object(value["error"], {"code", "message"})
    code = error["code"]
    if code not in ERROR_CODES or type(error["message"]) is not str:
        raise ProtocolError("Invalid protocol error")
    if encode_error(operation, code) != encoded:
        raise ProtocolError("Noncanonical protocol error")
    return ProtocolResponse(operation, False, error_code=code)


# ---------------------------------------------------------------------------
# Handler
# ---------------------------------------------------------------------------

_CODE_BY_EXCEPTION = MappingProxyType(
    {
        SnapshotExpired: SNAPSHOT_EXPIRED,
        InvalidContent: INVALID_CONTENT,
        RecoveryRequired: RECOVERY_REQUIRED,
        StoreUnavailable: STORE_UNAVAILABLE,
        CommitOutcomeUnknown: COMMIT_OUTCOME_UNKNOWN,
        RequestIdentityMismatch: REQUEST_IDENTITY_MISMATCH,
        StoreIdentityMismatch: STORE_IDENTITY_MISMATCH,
        ReplicaHistoryMismatch: REPLICA_HISTORY_MISMATCH,
        ProtocolError: PROTOCOL_ERROR,
    }
)


def error_code_for(exc: BaseException | None, operation: str | None = None) -> str:
    """Map a backend failure to a stable, non-leaking wire code.

    Only exact ``StoreError`` subclasses identify themselves. Anything else is
    a safe generic failure that never masquerades as a definite CAS rejection.
    """
    code = _CODE_BY_EXCEPTION.get(type(exc))
    if code is not None:
        return code
    if operation == Operation.COMMIT:
        return COMMIT_OUTCOME_UNKNOWN
    return STORE_UNAVAILABLE


def _peek_operation(message: object) -> str | None:
    """Best-effort operation attribution for an error response."""
    try:
        raw = decode_state_json(message)
    except InvalidContent:
        return None
    if type(raw) is dict and raw.get("operation") in OPERATIONS:
        return raw["operation"]
    return None


class RemoteProtocolHandler:
    """Serve one RemoteStore backend over a byte exchange.

    Malformed, unknown or unsupported input never reaches backend dispatch.
    """

    def __init__(self, store: RemoteStore) -> None:
        self._store = store

    def handle(self, message: bytes) -> bytes:
        try:
            request = decode_request(message)
        except ProtocolError:
            return encode_error(_peek_operation(message), PROTOCOL_ERROR)
        try:
            value = self._dispatch(request)
            return encode_success(
                request.operation, _response_body(request.operation, value)
            )
        except StoreError as exc:
            return encode_error(
                request.operation, error_code_for(exc, request.operation)
            )
        except Exception:  # noqa: BLE001 - never leak backend exceptions
            return encode_error(
                request.operation, error_code_for(None, request.operation)
            )

    def _dispatch(self, request: ProtocolRequest) -> object:
        operation, body = request.operation, request.body
        if operation == Operation.READ:
            return self._store.read()
        if operation == Operation.FETCH:
            expected, ids = body
            return self._store.fetch(expected, ids)
        if operation == Operation.COMMIT:
            return self._store.commit(body)
        if operation == Operation.RESOLVE_COMMIT:
            return self._store.resolve_commit(*body)
        if operation == Operation.RECOVER:
            return self._store.recover()
        raise ProtocolError("Unsupported protocol operation")
