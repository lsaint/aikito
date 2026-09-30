"""Malformed wire input and same-machine attachment regression checks."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest
from test_workspace_remote_commit import mutation
from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_backend import SerializedFilesystemBackend
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace
from workspace_serialized_attachment import FilesystemSerializedRemote

from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_protocol import (
    PROTOCOL_ERROR,
    Operation,
    RemoteProtocolHandler,
    decode_request,
    decode_response,
    encode_commit_request,
    encode_error,
    encode_request,
    encode_resolve_request,
    encode_success,
)
from aikito.workspace.remote_store import (
    CommitOutcomeUnknown,
    InvalidContent,
    ProtocolError,
    RemoteSnapshot,
)
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.serialized_remote import SerializedRemoteStore


def canonical(raw):
    return json.dumps(raw, sort_keys=True, separators=(",", ":")).encode()


@pytest.mark.parametrize("operation", [[], {}, None, True, 1])
@pytest.mark.parametrize("version", [1, 99])
def test_handler_rejects_invalid_operation_without_dispatch(operation, version):
    class UndispatchedStore:
        def read(self):
            raise AssertionError("Malformed input must not dispatch")

    message = canonical({"protocol": version, "operation": operation, "body": {}})
    with pytest.raises(ProtocolError):
        decode_request(message)
    response = decode_response(
        RemoteProtocolHandler(UndispatchedStore()).handle(message)
    )
    assert response.error_code == PROTOCOL_ERROR
    assert response.operation is None


@pytest.mark.parametrize("invalid", [[], {}, None, True, 1])
@pytest.mark.parametrize("field", ["operation", "code"])
def test_malformed_commit_response_is_unknown(invalid, field):
    store = InMemoryRemote("center")
    request = build_commit_request("client", "request", store.read(), (mutation(),))
    if field == "operation":
        raw = {"protocol": 1, "operation": invalid, "ok": True, "body": {}}
    else:
        raw = {
            "protocol": 1,
            "operation": "commit",
            "ok": False,
            "error": {"code": invalid, "message": "invalid"},
        }
    message = canonical(raw)
    with pytest.raises(ProtocolError):
        decode_response(message)
    adapter = SerializedRemoteStore(lambda _: message)
    with pytest.raises(CommitOutcomeUnknown):
        adapter.commit(request)


@pytest.mark.parametrize("invalid", [[], {}, True, 1])
def test_error_response_rejects_invalid_operation(invalid):
    raw = json.loads(encode_error(None, PROTOCOL_ERROR))
    raw["operation"] = invalid
    with pytest.raises(ProtocolError):
        decode_response(canonical(raw))


@pytest.mark.parametrize("invalid", [[], {}, None, True, 1])
def test_envelope_encoders_reject_invalid_types(invalid):
    for encode in (encode_request, encode_success):
        with pytest.raises(ProtocolError):
            encode(invalid, {})
    with pytest.raises(ProtocolError):
        encode_error(Operation.READ, invalid)


@pytest.mark.parametrize("depth", [65, 10000])
def test_base64_payload_nesting_is_rejected_on_fetch_and_commit(depth):
    payload = b"[" * depth + b"0" + b"]" * depth
    digest = hashlib.sha256(payload).hexdigest()
    encoded = base64.b64encode(payload).decode()
    message = encode_success(
        Operation.FETCH, {"payloads": {"id": {"content_hash": digest, "data": encoded}}}
    )
    with pytest.raises(ProtocolError) as exc:
        decode_response(message)
    if depth == 65:
        assert isinstance(exc.value.__cause__, InvalidContent)
        assert "nesting limit" in str(exc.value.__cause__)

    backend = InMemoryRemote("center")
    request = build_commit_request("client", "request", backend.read(), (mutation(),))
    raw = json.loads(encode_commit_request(request))
    raw["body"]["mutations"][0]["payload"] = {"content_hash": digest, "data": encoded}
    raw["body"]["mutations"][0]["after"]["content_hash"] = digest
    response = decode_response(RemoteProtocolHandler(backend).handle(canonical(raw)))
    assert response.error_code == PROTOCOL_ERROR
    assert backend.read() == RemoteSnapshot("center", 0, {})


@pytest.mark.parametrize("index", range(4))
def test_resolve_encoder_rejects_invalid_identity_before_exchange(index):
    args = ["center", "client", "request", "a" * 64]
    args[index] = ""
    with pytest.raises(ProtocolError):
        encode_resolve_request(*args)
    calls = []
    adapter = SerializedRemoteStore(lambda message: calls.append(message))
    with pytest.raises(ProtocolError):
        adapter.resolve_commit(*args)
    assert not calls


@pytest.mark.parametrize("relationship", ["same", "center-inside", "replica-inside"])
def test_filesystem_attachment_rejects_overlap_without_exchange(tmp_path, relationship):
    center = tmp_path / "center"
    local = {
        "same": center,
        "center-inside": tmp_path,
        "replica-inside": center / "replica",
    }[relationship]
    calls = []
    remote = FilesystemSerializedRemote(lambda message: calls.append(message), center)
    with pytest.raises(InvalidContent, match="must be separate"):
        remote.validate_replica(local)
    assert not calls


def test_filesystem_backend_assembly_rejects_nested_center(tmp_path):
    local = _workspace(tmp_path / "local")
    backend = SerializedFilesystemBackend(local / ".local/center")
    with pytest.raises(WorkspaceReconcileError, match="must be separate"):
        backend.plan(local)


def test_filesystem_attachment_checks_are_local_and_path_free(tmp_path):
    local = _workspace(tmp_path / "local")
    backend = FilesystemRemote.create(tmp_path / "center")
    transport = LoopbackTransport(backend)
    remote = FilesystemSerializedRemote(transport.exchange, backend.root)
    remote.validate_replica(local)
    assert not transport.requests
    build_reconcile_plan(local, remote)
    wire = b"".join(transport.requests + transport.responses)
    assert str(local).encode() not in wire
    assert str(backend.root).encode() not in wire


def test_missing_receipt_retries_unknown_delivery_with_original_request(tmp_path):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    backend = InMemoryRemote("center")
    transport = LoopbackTransport(backend)
    remote = SerializedRemoteStore(transport.exchange)
    run_reconciliation(local, remote, home, dry_run=False)
    write(local, "memory/notes/retry.md", "retry")
    attempts = []

    def uncertain_exchange(message):
        if decode_request(message).operation == Operation.COMMIT:
            attempts.append(message)
            if len(attempts) == 1:
                raise RuntimeError("Delivery could not be confirmed")
        return transport.exchange(message)

    remote = SerializedRemoteStore(uncertain_exchange)
    with pytest.raises(WorkspaceReconcileError) as exc:
        run_reconciliation(local, remote, home, dry_run=False)
    assert isinstance(exc.value.__cause__, CommitOutcomeUnknown)
    pending = PendingCommitStore(local, home).load()
    assert pending is not None
    request = pending.request
    assert (
        remote.resolve_commit(
            request.expected.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )
        is None
    )
    run_reconciliation(local, remote, home, dry_run=False)
    assert attempts == [attempts[0], attempts[0]]
    assert PendingCommitStore(local, home).load() is None
