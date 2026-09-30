"""Storage guarantees shared by filesystem and test-only memory stores."""

from __future__ import annotations

import hashlib
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    ResourceDescriptor,
    ResourceMutation,
    TomlPayload,
    payload_hash,
)
from aikito.workspace.resources import value_fingerprint
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_store import InvalidContent, SnapshotExpired
from aikito.workspace.toml_render import TomlValue
from workspace_memory_remote import InMemoryRemote
from workspace_store_setup import publish_batch


@pytest.fixture(params=["filesystem", "memory"])
def store(request, tmp_path):
    return (
        FilesystemRemote.create(tmp_path / "center")
        if request.param == "filesystem"
        else InMemoryRemote()
    )


def mutation(name="one", data=b"one", before=None):
    payload = FilePayload(data)
    return ResourceMutation(
        f"memory:notes/{name}.md",
        before,
        ResourceDescriptor(hashlib.sha256(data).hexdigest(), payload_hash(payload)),
        payload,
    )


def checkpoint(store):
    snapshot = store.read()
    return snapshot, store.fetch(snapshot, snapshot.resources)


def test_revision_presence_update_delete_and_empty_batch(store):
    initial = store.read()
    with pytest.raises(InvalidContent):
        publish_batch(store, initial, [])
    one, two = mutation(), mutation("two")
    snapshot = publish_batch(store, initial, [one, two])
    assert snapshot.revision == initial.revision + 1
    updated = mutation(data=b"changed", before=one.after)
    deleted = ResourceMutation(two.id, two.after, None, None)
    final = publish_batch(store, snapshot, [updated, deleted])
    assert final.revision == snapshot.revision + 1
    assert store.fetch(final, [one.id]) == {one.id: updated.payload}
    assert two.id not in final.resources
    assert initial.resources == {}
    assert snapshot.resources[one.id] == one.after


@pytest.mark.parametrize("mismatch", ["revision", "identity", "descriptor"])
def test_stale_empty_fetch_does_not_change_state(store, mismatch):
    expected = store.read()
    if mismatch == "revision":
        publish_batch(store, expected, [mutation()])
    elif mismatch == "identity":
        expected = replace(expected, sync_id="unrelated opaque identity")
    else:
        expected = replace(expected, resources={mutation().id: mutation().after})
    before = checkpoint(store)
    with pytest.raises(SnapshotExpired):
        store.fetch(expected, [])
    assert checkpoint(store) == before


def test_stale_nonempty_fetch_and_missing_batch_have_no_partial_success(store):
    initial = store.read()
    one = mutation()
    snapshot = publish_batch(store, initial, [one])
    before = checkpoint(store)
    with pytest.raises(SnapshotExpired):
        store.fetch(initial, [one.id])
    with pytest.raises(InvalidContent):
        store.fetch(snapshot, [one.id, "memory:notes/missing.md"])
    assert store.fetch(snapshot, [one.id, one.id]) == {one.id: one.payload}
    assert checkpoint(store) == before


@pytest.mark.parametrize("invalid", ["duplicate", "before", "hash"])
def test_invalid_whole_batch_preserves_revision_and_payloads(store, invalid):
    snapshot = store.read()
    first, second = mutation(), mutation("two")
    if invalid == "duplicate":
        second = first
    elif invalid == "before":
        second = replace(second, before=second.after)
    else:
        # Model an untrusted transport bypassing client-side construction checks.
        object.__setattr__(second, "payload", FilePayload(b"corrupted"))
    before = checkpoint(store)
    with pytest.raises((InvalidContent, SnapshotExpired)):
        publish_batch(store, snapshot, [first, second])
    assert checkpoint(store) == before


def test_two_writers_cannot_publish_the_same_revision(store):
    snapshot = store.read()
    ready = threading.Barrier(2)

    def submit(name):
        ready.wait(timeout=10)
        try:
            return publish_batch(store, snapshot, [mutation(name)])
        except SnapshotExpired:
            return None

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(submit, ("one", "two")))
    assert sum(result is not None for result in results) == 1
    final = store.read()
    assert final.revision == snapshot.revision + 1
    assert len(final.resources) == 1
    assert len(store.fetch(final, final.resources)) == 1


def test_caller_and_returned_values_cannot_mutate_storage(store):
    field = TomlValue(("feature",), [1, {"nested": [True]}])
    payload = TomlPayload.from_value(field)
    descriptor = ResourceDescriptor(
        value_fingerprint(field.value), payload_hash(payload)
    )
    batch = [ResourceMutation("config:feature", None, descriptor, payload)]
    snapshot = publish_batch(store, store.read(), batch)
    batch.clear()
    field.value[1]["nested"].clear()
    fetched = store.fetch(snapshot, ["config:feature"])
    value = fetched["config:feature"].field().value
    value[1]["nested"].clear()
    assert store.fetch(snapshot, ["config:feature"])[
        "config:feature"
    ].field().value == [1, {"nested": [True]}]
    with pytest.raises(TypeError):
        fetched["config:feature"] = payload
    with pytest.raises(TypeError):
        snapshot.resources["config:feature"] = descriptor
    assert store.read() == snapshot and store.read() is not snapshot


def test_empty_fingerprint_is_presence_not_absence(store):
    payload = MemberPayload()
    descriptor = ResourceDescriptor("", payload_hash(payload))
    identity = "project:demo"
    snapshot = publish_batch(
        store, store.read(), [ResourceMutation(identity, None, descriptor, payload)]
    )
    assert store.fetch(snapshot, [identity]) == {identity: payload}
    before = checkpoint(store)
    with pytest.raises(InvalidContent):
        publish_batch(
            store, snapshot, [ResourceMutation(identity, None, descriptor, payload)]
        )
    assert checkpoint(store) == before
    final = publish_batch(
        store, snapshot, [ResourceMutation(identity, descriptor, None, None)]
    )
    assert identity not in final.resources
