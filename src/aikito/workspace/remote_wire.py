"""Versioned canonical commit encoding for digests and durable pending state.

This is an internal persistence encoding, not a frozen HTTP wire format.
Payload bytes use the existing codec. IDs, fingerprints and references stay
opaque; only batch structure and transport integrity are checked here.
"""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

from .payload import (
    PayloadError,
    ResourceDescriptor,
    ResourceMutation,
    TreePayload,
    decode_payload,
    encode_payload,
)
from .remote_store import (
    CommitRequest,
    CommitResult,
    InvalidContent,
    ReceiptCursor,
    RemoteSnapshot,
)

COMMIT_ENCODING_VERSION = 1
MAX_COMMIT_JSON_DEPTH = 64


def _canonical(body: dict) -> bytes:
    try:
        return json.dumps(
            body,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise InvalidContent("Invalid commit encoding") from exc


def _descriptor(value: ResourceDescriptor | None) -> dict | None:
    if value is None:
        return None
    return {
        "fingerprint": value.fingerprint,
        "content_hash": value.content_hash,
        "references": list(value.references),
        "mode_fingerprint": value.mode_fingerprint,
    }


def _resources(snapshot: RemoteSnapshot) -> dict:
    return {key: _descriptor(value) for key, value in snapshot.resources.items()}


def _request_body(request: CommitRequest) -> dict:
    cursor = request.previous_receipt
    return {
        "version": COMMIT_ENCODING_VERSION,
        "kind": "aikito.commit-request",
        "client_id": request.client_id,
        "previous_receipt": None
        if cursor is None
        else {
            "request_id": cursor.request_id,
            "mutation_digest": cursor.mutation_digest,
        },
        "expected": {
            "sync_id": request.expected.sync_id,
            "revision": request.expected.revision,
            "resources": _resources(request.expected),
        },
        "mutations": [
            {
                "id": mutation.id,
                "before": _descriptor(mutation.before),
                "after": _descriptor(mutation.after),
                "payload": None
                if mutation.payload is None
                else base64.b64encode(encode_payload(mutation.payload)).decode("ascii"),
            }
            for mutation in request.mutations
        ],
    }


def commit_request_digest(request: CommitRequest) -> str:
    """Cover all semantics except the request ID and the digest itself."""
    return hashlib.sha256(_canonical(_request_body(request))).hexdigest()


def build_commit_request(
    client_id: str,
    request_id: str,
    expected: RemoteSnapshot,
    mutations: tuple[ResourceMutation, ...],
    *,
    previous_receipt: ReceiptCursor | None = None,
) -> CommitRequest:
    """Capture a complete immutable batch with its independently verifiable digest."""
    request = CommitRequest(
        client_id, request_id, previous_receipt, expected, mutations, "0" * 64
    )
    request = replace(request, mutation_digest=commit_request_digest(request))
    validate_commit_request(request)
    return request


def validate_commit_request(request: CommitRequest) -> None:
    """Recompute the digest at each trust boundary, including backend receipt lookup."""
    if not isinstance(request, CommitRequest):
        raise InvalidContent("Invalid commit request type")
    try:

        def descriptor(value):
            return None if value is None else replace(value)

        def mutation(value):
            payload = value.payload
            if isinstance(payload, TreePayload):
                payload = replace(
                    payload, entries=tuple(replace(e) for e in payload.entries)
                )
            elif payload is not None:
                payload = replace(payload)
            return replace(
                value,
                before=descriptor(value.before),
                after=descriptor(value.after),
                payload=payload,
            )

        checked = replace(
            request,
            expected=replace(
                request.expected,
                resources={
                    key: descriptor(value)
                    for key, value in request.expected.resources.items()
                },
            ),
            previous_receipt=None
            if request.previous_receipt is None
            else replace(request.previous_receipt),
            mutations=tuple(mutation(m) for m in request.mutations),
        )
    except (PayloadError, TypeError, AttributeError) as exc:
        raise InvalidContent("Invalid commit mutation structure or payload") from exc
    if checked != request:
        raise InvalidContent("Noncanonical commit model")
    if commit_request_digest(request) != request.mutation_digest:
        raise InvalidContent("Commit request digest mismatch")
    for mutation in request.mutations:
        if request.expected.resources.get(mutation.id) != mutation.before:
            raise InvalidContent("Mutation before descriptor differs from expected")


def encode_commit_request(request: CommitRequest) -> bytes:
    validate_commit_request(request)
    return _canonical(
        {
            **_request_body(request),
            "request_id": request.request_id,
            "mutation_digest": request.mutation_digest,
        }
    )


def _unique_object(pairs) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidContent("Duplicate commit field")
        result[key] = value
    return result


def decode_base64(encoded: object) -> bytes:
    """Accept one canonical ASCII representation, including zero pad bits."""
    if type(encoded) is not str:
        raise InvalidContent("Invalid commit Base64 type")
    try:
        decoded = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise InvalidContent("Invalid commit Base64") from exc
    if base64.b64encode(decoded).decode("ascii") != encoded:
        raise InvalidContent("Noncanonical commit Base64")
    return decoded


def _object(raw: object, fields: set[str]) -> dict:
    if type(raw) is not dict or set(raw) != fields:
        raise InvalidContent("Invalid commit fields")
    return raw


def _decode_descriptor(raw: object) -> ResourceDescriptor | None:
    if raw is None:
        return None
    value = _object(
        raw,
        {
            "fingerprint",
            "content_hash",
            "references",
            "mode_fingerprint",
        },
    )
    if type(value["references"]) is not list:
        raise InvalidContent("Invalid commit references")
    return ResourceDescriptor(
        value["fingerprint"],
        value["content_hash"],
        tuple(value["references"]),
        value["mode_fingerprint"],
    )


def _decode_snapshot(raw: object) -> RemoteSnapshot:
    value = _object(raw, {"sync_id", "revision", "resources"})
    if type(value["resources"]) is not dict:
        raise InvalidContent("Invalid commit resources")
    resources = {}
    for key, descriptor in value["resources"].items():
        decoded = _decode_descriptor(descriptor)
        if decoded is None:
            raise InvalidContent("Snapshot descriptor cannot be absent")
        resources[key] = decoded
    return RemoteSnapshot(value["sync_id"], value["revision"], resources)


def _decode_mutation(raw: object) -> ResourceMutation:
    value = _object(raw, {"id", "before", "after", "payload"})
    before, after = (
        _decode_descriptor(value["before"]),
        _decode_descriptor(value["after"]),
    )
    payload = None
    if value["payload"] is not None:
        if after is None or type(value["payload"]) is not str:
            raise InvalidContent("Invalid mutation payload presence")
        payload = decode_payload(decode_base64(value["payload"]), after.content_hash)
    return ResourceMutation(value["id"], before, after, payload)


def decode_commit_request(encoded: bytes) -> CommitRequest:
    """Reject malformed, noncanonical or forged requests without exposing content."""
    if type(encoded) is not bytes:
        raise InvalidContent("Commit encoding requires bytes")
    try:
        raw = _object(
            decode_state_json(encoded),
            {
                "version",
                "kind",
                "client_id",
                "request_id",
                "mutation_digest",
                "previous_receipt",
                "expected",
                "mutations",
            },
        )
        if type(raw["version"]) is not int or raw["version"] != COMMIT_ENCODING_VERSION:
            raise InvalidContent("Unsupported commit encoding version")
        if raw["kind"] != "aikito.commit-request":
            raise InvalidContent("Invalid commit encoding kind")
        cursor = None
        if raw["previous_receipt"] is not None:
            previous = _object(
                raw["previous_receipt"], {"request_id", "mutation_digest"}
            )
            cursor = ReceiptCursor(previous["request_id"], previous["mutation_digest"])
        if type(raw["mutations"]) is not list:
            raise InvalidContent("Invalid commit mutation batch")
        request = CommitRequest(
            raw["client_id"],
            raw["request_id"],
            cursor,
            _decode_snapshot(raw["expected"]),
            tuple(_decode_mutation(item) for item in raw["mutations"]),
            raw["mutation_digest"],
        )
        if encode_commit_request(request) != encoded:
            raise InvalidContent("Noncanonical commit encoding")
        return request
    except InvalidContent:
        raise
    except (
        PayloadError,
        ValueError,
        TypeError,
        KeyError,
        AttributeError,
        RecursionError,
    ) as exc:
        raise InvalidContent("Invalid commit encoding") from exc


def committed_snapshot(request: CommitRequest) -> RemoteSnapshot:
    """Predict this batch's accepted snapshot without fetching historical payloads."""
    validate_commit_request(request)
    resources = dict(request.expected.resources)
    for mutation in request.mutations:
        if mutation.after is None:
            resources.pop(mutation.id)
        else:
            resources[mutation.id] = mutation.after
    return RemoteSnapshot(
        request.expected.sync_id, request.expected.revision + 1, resources
    )


def build_commit_result(request: CommitRequest) -> CommitResult:
    """Build the receipt for an accepted batch; this does not publish anything."""
    snapshot = committed_snapshot(request)
    body = {
        "version": COMMIT_ENCODING_VERSION,
        "kind": "aikito.commit-result",
        "sync_id": snapshot.sync_id,
        "client_id": request.client_id,
        "request_id": request.request_id,
        "mutation_digest": request.mutation_digest,
        "accepted_revision": snapshot.revision,
        "resources": _resources(snapshot),
    }
    return CommitResult(
        snapshot.sync_id,
        request.client_id,
        request.request_id,
        request.mutation_digest,
        snapshot.revision,
        hashlib.sha256(_canonical(body)).hexdigest(),
    )


def validate_commit_result(request: CommitRequest, result: CommitResult) -> None:
    """An invalid receipt leaves the request unresolved; callers must retain pending."""
    if not isinstance(result, CommitResult) or replace(result) != build_commit_result(
        request
    ):
        raise InvalidContent("Commit result identity, revision or digest mismatch")


def encode_receipt(result: CommitResult) -> dict:
    """Persist a historical receipt without retaining its snapshot or payloads."""
    if not isinstance(result, CommitResult):
        raise InvalidContent("Invalid receipt type")
    result = replace(result)
    return {
        "version": COMMIT_ENCODING_VERSION,
        "sync_id": result.sync_id,
        "client_id": result.client_id,
        "request_id": result.request_id,
        "mutation_digest": result.mutation_digest,
        "accepted_revision": result.accepted_revision,
        "result_digest": result.result_digest,
    }


def decode_receipt(raw: object) -> CommitResult:
    value = _object(
        raw,
        {
            "version",
            "sync_id",
            "client_id",
            "request_id",
            "mutation_digest",
            "accepted_revision",
            "result_digest",
        },
    )
    if type(value["version"]) is not int or value["version"] != COMMIT_ENCODING_VERSION:
        raise InvalidContent("Unsupported receipt encoding version")
    return CommitResult(
        value["sync_id"],
        value["client_id"],
        value["request_id"],
        value["mutation_digest"],
        value["accepted_revision"],
        value["result_digest"],
    )


def decode_state_json(encoded: str | bytes) -> object:
    """Reject duplicate fields in persistent commit state as well as requests."""
    try:
        if type(encoded) is bytes:
            encoded = encoded.decode("utf-8")
        elif type(encoded) is not str:
            raise InvalidContent("Commit state JSON requires text or UTF-8 bytes")

        def invalid_constant(_):
            raise InvalidContent("Nonfinite commit state JSON number")

        raw = json.loads(
            encoded, object_pairs_hook=_unique_object, parse_constant=invalid_constant
        )
        pending = [(raw, 1)]
        while pending:
            value, depth = pending.pop()
            if isinstance(value, (dict, list)):
                if depth > MAX_COMMIT_JSON_DEPTH:
                    raise InvalidContent("Commit state JSON nesting limit exceeded")
                if isinstance(value, dict):
                    for key in value:
                        key.encode("utf-8")
                children = value.values() if isinstance(value, dict) else value
                pending.extend((child, depth + 1) for child in children)
            elif isinstance(value, str):
                value.encode("utf-8")
        return raw
    except InvalidContent:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise InvalidContent("Invalid commit state JSON") from exc
