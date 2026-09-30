"""Protocol hardening: frozen wire v1, object isolation and path-free bytes."""

from __future__ import annotations

from test_workspace_remote_commit import mutation
from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.reconcile import run_reconciliation
from aikito.workspace.remote_protocol import (
    Operation,
    decode_request,
    encode_read_request,
    encode_recover_request,
)
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.serialized_remote import SerializedRemoteStore


def test_wire_v1_envelope_bytes_are_frozen():
    # A frozen golden shape guards compatibility for the internal v1 protocol.
    assert encode_read_request() == b'{"body":{},"operation":"read","protocol":1}'
    assert encode_recover_request() == (
        b'{"body":{},"operation":"recover","protocol":1}'
    )
    assert decode_request(encode_read_request()).operation == Operation.READ


def test_client_object_mutation_after_send_does_not_change_server():
    backend = InMemoryRemote("center")
    adapter = SerializedRemoteStore(LoopbackTransport(backend).exchange)
    payload = FilePayload(b"original")
    item = ResourceMutation(
        "id", None, ResourceDescriptor("fp", payload_hash(payload)), payload
    )
    adapter.commit(build_commit_request("client", "r1", backend.read(), (item,)))
    object.__setattr__(item, "id", "changed")
    object.__setattr__(item, "payload", FilePayload(b"changed"))
    snapshot = backend.read()
    assert backend.fetch(snapshot, ["id"]) == {"id": FilePayload(b"original")}


def test_backend_change_after_response_does_not_change_client_read():
    backend = InMemoryRemote("center")
    adapter = SerializedRemoteStore(LoopbackTransport(backend).exchange)
    snapshot = adapter.read()
    adapter.commit(build_commit_request("client", "r1", backend.read(), (mutation(),)))
    assert snapshot.revision == 0 and not snapshot.resources
    assert adapter.read().revision == 1


def test_reconciliation_wire_bytes_never_carry_local_paths(tmp_path):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    transport = LoopbackTransport(InMemoryRemote("center"))
    remote = SerializedRemoteStore(transport.exchange)
    write(local, "memory/notes/secret.md", "content")
    run_reconciliation(local, remote, home, dry_run=False)
    wire = b"".join(transport.requests + transport.responses)
    assert str(local).encode() not in wire
    assert str(home).encode() not in wire
    assert str(tmp_path).encode() not in wire
