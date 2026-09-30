"""No disk shortcuts or client semantics inside the memory store."""

from __future__ import annotations

import builtins
import os
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.remote_store import InvalidContent
from workspace_memory_remote import InMemoryRemote


def opaque_mutation(identity="opaque/ID:../does-not-name-a-path"):
    payload = FilePayload(b'api_key = "abcdefghijklmnopqrstuv"\n')
    descriptor = ResourceDescriptor(
        "opaque logical fingerprint",
        payload_hash(payload),
        ("opaque missing reference",),
    )
    return ResourceMutation(identity, None, descriptor, payload)


def test_store_operations_never_access_disk_or_make_staging(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("Memory center attempted filesystem access")

    for module, name in (
        (builtins, "open"),
        (os, "open"),
        (os, "mkdir"),
        (os, "replace"),
        (tempfile, "TemporaryDirectory"),
        (tempfile, "mkdtemp"),
        (tempfile, "mkstemp"),
        (Path, "open"),
        (Path, "mkdir"),
        (Path, "stat"),
        (Path, "lstat"),
        (Path, "read_bytes"),
        (Path, "write_bytes"),
        (Path, "read_text"),
        (Path, "write_text"),
    ):
        monkeypatch.setattr(module, name, forbidden)
    remote = InMemoryRemote("opaque sync identity")
    remote.validate_replica(Path("unused-local"))
    initial = remote.read()
    item = opaque_mutation()
    snapshot = remote.commit(initial, [item])
    assert remote.fetch(snapshot, [item.id]) == {item.id: item.payload}
    assert remote.commit(snapshot, []) == snapshot
    final = remote.commit(snapshot, [ResourceMutation(item.id, item.after, None, None)])
    assert final.resources == {} and remote.recover() is False
    assert not hasattr(remote, "root")
    assert all(type(data) is bytes for data in remote._payloads.values())


@pytest.mark.parametrize("operation", ["read", "fetch", "commit"])
@pytest.mark.parametrize("corruption", ["missing", "bytes"])
def test_corrupt_stored_content_is_rejected_without_state_change(operation, corruption):
    remote = InMemoryRemote()
    item = opaque_mutation()
    snapshot = remote.commit(remote.read(), [item])
    if corruption == "missing":
        remote._payloads.pop(item.id)
    else:
        remote._payloads[item.id] = b"corrupted encoded payload"
    before = dict(remote._payloads)
    with pytest.raises(InvalidContent, match="integrity"):
        if operation == "read":
            remote.read()
        else:
            getattr(remote, operation)(snapshot, [])
    assert remote._snapshot == snapshot and remote._payloads == before


def test_old_payload_and_snapshot_remain_independent_after_publication():
    remote = InMemoryRemote()
    item = opaque_mutation()
    snapshot = remote.commit(remote.read(), [item])
    old_payloads = remote.fetch(snapshot, [item.id])
    replacement = FilePayload(b"new opaque content")
    descriptor = replace(item.after, content_hash=payload_hash(replacement))
    remote.commit(
        snapshot, [ResourceMutation(item.id, item.after, descriptor, replacement)]
    )
    assert old_payloads[item.id] == item.payload
    assert snapshot.resources[item.id] == item.after
