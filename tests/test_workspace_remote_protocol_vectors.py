"""Frozen operation, error and malformed-input bytes stay reproducible."""

import hashlib
import json

from generate_remote_protocol_vectors import DEFAULT_DIRECTORY, generate_vectors

from aikito.workspace.remote_protocol import (
    ERROR_CODES,
    OPERATIONS,
    decode_request,
    decode_response,
)


def test_vectors_match_regeneration():
    assert {
        path.name: path.read_bytes() for path in DEFAULT_DIRECTORY.iterdir()
    } == generate_vectors()


def test_vector_coverage_and_digest_preimages():
    manifest = json.loads((DEFAULT_DIRECTORY / "manifest.json").read_bytes())
    operations = set()
    for pair in manifest["operations"]:
        request = decode_request((DEFAULT_DIRECTORY / pair["request"]).read_bytes())
        response = decode_response((DEFAULT_DIRECTORY / pair["response"]).read_bytes())
        assert response.ok and response.operation == request.operation
        operations.add(request.operation)
    assert operations == OPERATIONS
    assert {entry["code"] for entry in manifest["errors"]} == ERROR_CODES
    for entry in manifest["malformed"]:
        assert (
            decode_response(
                (DEFAULT_DIRECTORY / entry["response"]).read_bytes()
            ).error_code
            == "protocol_error"
        )
    for entry in manifest["digests"]:
        assert (
            hashlib.sha256(
                (DEFAULT_DIRECTORY / entry["preimage"]).read_bytes()
            ).hexdigest()
            == entry["sha256"]
        )
    wire = (DEFAULT_DIRECTORY / "commit.request.bin").read_bytes()
    assert "<>&\u2028\u2029".encode() in wire
    assert b"\\u2028" not in wire and b"\\u003c" not in wire
