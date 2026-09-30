"""Strict Remote Protocol v1 codec: canonical envelopes and fail-closed decoding."""

from __future__ import annotations

import json

import pytest
from test_workspace_remote_commit import mutation, request

from aikito.workspace.payload import FilePayload
from aikito.workspace.remote_protocol import (
    COMMIT_OUTCOME_UNKNOWN,
    ERROR_MESSAGES,
    PROTOCOL_ERROR,
    REMOTE_PROTOCOL_VERSION,
    STORE_UNAVAILABLE,
    Operation,
    ProtocolRequest,
    ProtocolResponse,
    decode_request,
    decode_response,
    encode_commit_request,
    encode_commit_response,
    encode_error,
    encode_fetch_request,
    encode_fetch_response,
    encode_read_request,
    encode_read_response,
    encode_recover_request,
    encode_recover_response,
    encode_request,
    encode_resolve_request,
    encode_resolve_response,
    encode_success,
)
from aikito.workspace.remote_store import ProtocolError, RemoteSnapshot
from aikito.workspace.remote_wire import (
    COMMIT_ENCODING_VERSION,
    build_commit_result,
    commit_request_digest,
)


def canonical(raw) -> bytes:
    return json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def envelope(**fields) -> dict:
    return {"protocol": REMOTE_PROTOCOL_VERSION, **fields}


# ---------------------------------------------------------------------------
# Round trips
# ---------------------------------------------------------------------------


def test_read_round_trip():
    snapshot = RemoteSnapshot("center", 3, {"memory:notes/a.md": mutation().after})
    assert decode_request(encode_read_request()) == ProtocolRequest(
        Operation.READ, None
    )
    response = decode_response(encode_read_response(snapshot))
    assert response.ok and response.operation == Operation.READ
    assert response.body == snapshot


def test_fetch_round_trip():
    expected = RemoteSnapshot("center", 3, {"memory:notes/a.md": mutation().after})
    payload = FilePayload(b"fetch me")
    response = decode_response(encode_fetch_response({"memory:notes/a.md": payload}))
    assert response.body == {"memory:notes/a.md": payload}

    # Request IDs are a set: sorted, deduplicated and canonical.
    encoded = encode_fetch_request(
        expected, ["memory:notes/b", "memory:notes/a", "memory:notes/a"]
    )
    decoded = decode_request(encoded)
    assert decoded.operation == Operation.FETCH
    assert decoded.body == (expected, ("memory:notes/a", "memory:notes/b"))
    assert encoded == canonical(
        envelope(
            operation=Operation.FETCH,
            body={
                "expected": json.loads(encode_read_response(expected))["body"],
                "ids": ["memory:notes/a", "memory:notes/b"],
            },
        )
    )


def test_commit_round_trip_and_identity():
    req = request()
    encoded = encode_commit_request(req)
    decoded = decode_request(encoded)
    assert decoded.operation == Operation.COMMIT
    assert decoded.body == req
    assert decoded.body.mutation_digest == commit_request_digest(req)

    result = build_commit_result(req)
    response = decode_response(encode_commit_response(result))
    assert response.body == result
    assert response.body.result_digest == result.result_digest


def test_resolve_round_trip():
    req = request()
    encoded = encode_resolve_request(
        req.expected.sync_id, req.client_id, req.request_id, req.mutation_digest
    )
    assert decode_request(encoded).body == (
        req.expected.sync_id,
        req.client_id,
        req.request_id,
        req.mutation_digest,
    )
    assert decode_response(encode_resolve_response(None)).body is None
    result = build_commit_result(req)
    assert decode_response(encode_resolve_response(result)).body == result


def test_recover_round_trip():
    assert decode_request(encode_recover_request()).body is None
    assert decode_response(encode_recover_response(True)).body is True


# ---------------------------------------------------------------------------
# Envelope and versioning
# ---------------------------------------------------------------------------


def test_protocol_and_commit_version_are_separate_fields():
    req = request()
    raw = json.loads(encode_commit_request(req))
    assert raw["protocol"] == REMOTE_PROTOCOL_VERSION
    assert "version" not in raw
    assert raw["body"]["version"] == COMMIT_ENCODING_VERSION

    with pytest.raises(ProtocolError):
        decode_request(canonical({**raw, "protocol": REMOTE_PROTOCOL_VERSION + 1}))
    with pytest.raises(ProtocolError):
        decode_request(canonical({**raw, "body": {**raw["body"], "version": 99}}))


def test_canonical_bytes_are_key_order_independent_for_input():
    # A valid but non-canonical envelope (unsorted keys) must be rejected.
    with pytest.raises(ProtocolError):
        decode_request(b'{"operation":"read","body":{},"protocol":1}')


def test_unknown_protocol_version_and_operation():
    with pytest.raises(ProtocolError):
        decode_request(canonical({"protocol": 2, "operation": "read", "body": {}}))
    with pytest.raises(ProtocolError):
        decode_request(canonical(envelope(operation="teleport", body={})))


def test_unknown_and_unexpected_fields():
    ok = encode_read_response(RemoteSnapshot("center", 0, {}))
    with pytest.raises(ProtocolError):
        decode_request(canonical({**json.loads(ok), "extra": 1}))
    with pytest.raises(ProtocolError):
        decode_response(canonical({**json.loads(ok), "extra": 1}))
    # A request is not a response.
    with pytest.raises(ProtocolError):
        decode_response(encode_read_request())


def test_duplicate_json_fields_rejected():
    forged = b'{"protocol":1,"operation":"read","operation":"read","body":{}}'
    with pytest.raises(ProtocolError):
        decode_request(forged)


def test_invalid_utf8_and_nonbytes_rejected():
    with pytest.raises(ProtocolError):
        decode_request(b'{"protocol":1,"operation":"read","body":{}}\xff')
    with pytest.raises(ProtocolError):
        decode_request('{"protocol":1,"operation":"read","body":{}}')


def test_excessive_nesting_rejected():
    nested: object = []
    for _ in range(200):
        nested = [nested]
    with pytest.raises(ProtocolError):
        decode_request(canonical(envelope(operation="read", body=nested)))


# ---------------------------------------------------------------------------
# Body strictness
# ---------------------------------------------------------------------------


def _snapshot_body(**overrides) -> dict:
    body = {
        "sync_id": "center",
        "revision": 1,
        "resources": {
            "memory:notes/a.md": {
                "fingerprint": "fp",
                "content_hash": "a" * 64,
                "references": [],
                "mode_fingerprint": None,
            }
        },
    }
    body.update(overrides)
    return body


def test_invalid_revision_type_rejected():
    with pytest.raises(ProtocolError):
        decode_response(encode_success(Operation.READ, _snapshot_body(revision="1")))


def test_malformed_descriptor_rejected():
    with pytest.raises(ProtocolError):
        decode_response(
            encode_success(
                Operation.READ,
                _snapshot_body(resources={"memory:notes/a.md": {"fingerprint": "fp"}}),
            )
        )
    with pytest.raises(ProtocolError):
        decode_response(
            encode_success(
                Operation.READ,
                _snapshot_body(
                    resources={
                        "memory:notes/a.md": {
                            "fingerprint": "fp",
                            "content_hash": "not-hex",
                            "references": [],
                            "mode_fingerprint": None,
                        }
                    }
                ),
            )
        )


def test_invalid_base64_and_malformed_payload_rejected():
    payload = FilePayload(b"content")
    good = json.loads(encode_fetch_response({"id": payload}))
    with pytest.raises(ProtocolError):
        decode_response(
            encode_success(
                Operation.FETCH,
                {"payloads": {"id": {"content_hash": "a" * 64, "data": "!!!"}}},
            )
        )
    # Canonical Base64 but wrong content hash.
    tampered = {
        "payloads": {
            "id": {
                "content_hash": "b" * 64,
                "data": good["body"]["payloads"]["id"]["data"],
            }
        }
    }
    with pytest.raises(ProtocolError):
        decode_response(encode_success(Operation.FETCH, tampered))


def test_forged_commit_digest_rejected():
    req = request()
    raw = json.loads(encode_commit_request(req))
    raw["body"]["mutation_digest"] = "1" * 64
    with pytest.raises(ProtocolError):
        decode_request(canonical(raw))


def test_malformed_result_digest_rejected():
    result = build_commit_result(request())
    raw = json.loads(encode_commit_response(result))
    raw["body"]["result_digest"] = "not-hex"
    with pytest.raises(ProtocolError):
        decode_response(canonical(raw))


def test_recover_requires_strict_boolean():
    with pytest.raises(ProtocolError):
        decode_response(encode_success(Operation.RECOVER, {"recovered": "yes"}))
    with pytest.raises(ProtocolError):
        decode_response(encode_success(Operation.RECOVER, {"recovered": 1}))


def test_empty_operation_body_must_be_empty_object():
    with pytest.raises(ProtocolError):
        decode_request(encode_request(Operation.READ, {"unexpected": True}))


# ---------------------------------------------------------------------------
# Error protocol
# ---------------------------------------------------------------------------


def test_error_envelope_round_trip():
    encoded = encode_error(Operation.COMMIT, STORE_UNAVAILABLE)
    decoded = decode_response(encoded)
    assert decoded == ProtocolResponse(
        Operation.COMMIT, False, error_code=STORE_UNAVAILABLE
    )
    assert json.loads(encoded)["error"]["message"] == ERROR_MESSAGES[STORE_UNAVAILABLE]


def test_error_operation_may_be_unidentified_only_on_error():
    decoded = decode_response(encode_error(None, PROTOCOL_ERROR))
    assert decoded.operation is None and decoded.error_code == PROTOCOL_ERROR
    with pytest.raises(ProtocolError):
        encode_error("teleport", PROTOCOL_ERROR)
    with pytest.raises(ProtocolError):
        decode_response(
            canonical(
                envelope(
                    operation="teleport",
                    ok=False,
                    error={
                        "code": PROTOCOL_ERROR,
                        "message": ERROR_MESSAGES[PROTOCOL_ERROR],
                    },
                )
            )
        )


def test_unknown_error_code_fails_closed():
    with pytest.raises(ProtocolError):
        encode_error(Operation.READ, "totally_made_up")
    with pytest.raises(ProtocolError):
        decode_response(
            canonical(
                envelope(
                    operation=Operation.READ,
                    ok=False,
                    error={"code": "totally_made_up", "message": "boom"},
                )
            )
        )


def test_noncanonical_error_message_rejected():
    with pytest.raises(ProtocolError):
        decode_response(
            canonical(
                envelope(
                    operation=Operation.READ,
                    ok=False,
                    error={"code": STORE_UNAVAILABLE, "message": "/tmp/secret"},
                )
            )
        )


def test_error_messages_are_path_free():
    for message in ERROR_MESSAGES.values():
        assert "/" not in message and "\\" not in message
        assert message


def test_commit_outcome_unknown_round_trip():
    decoded = decode_response(encode_error(Operation.COMMIT, COMMIT_OUTCOME_UNKNOWN))
    assert decoded.error_code == COMMIT_OUTCOME_UNKNOWN


def test_error_codec_rejects_mixed_shapes():
    # ok=false must not carry a body; ok=true must not carry an error.
    with pytest.raises(ProtocolError):
        decode_response(
            canonical(envelope(operation=Operation.READ, ok=False, body={}))
        )
    with pytest.raises(ProtocolError):
        decode_response(
            canonical(
                envelope(
                    operation=Operation.READ,
                    ok=True,
                    error={
                        "code": PROTOCOL_ERROR,
                        "message": ERROR_MESSAGES[PROTOCOL_ERROR],
                    },
                )
            )
        )
