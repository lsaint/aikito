"""Portable store contract and filesystem-specific integrity defenses."""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace

import pytest

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE
from aikito.workspace.remote_store import (
    InvalidContent,
    RemoteSnapshot,
    SnapshotExpired,
)
from aikito.workspace.resources import value_fingerprint
from aikito.workspace.resource_state import PENDING_COMMIT_STATE
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_backend import files
from workspace_reconcile_smoke import _workspace
from workspace_remote_store_smoke import StoreFacade, exercise
from workspace_store_setup import publish_batch


@pytest.fixture(params=["filesystem", "memory"])
def protocol_store(request, tmp_path):
    return (
        FilesystemRemote.create(tmp_path / "center")
        if request.param == "filesystem"
        else InMemoryRemote()
    )


def stored_content(remote):
    snapshot = remote.read()
    return snapshot, remote.fetch(snapshot, snapshot.resources)


def mutation(name="one", data=b"one", before=None):
    payload = FilePayload(data)
    descriptor = ResourceDescriptor(
        hashlib.sha256(data).hexdigest(), payload_hash(payload)
    )
    return ResourceMutation(f"memory:notes/{name}.md", before, descriptor, payload)


def test_engine_uses_only_portable_interface(tmp_path):
    exercise(tmp_path)


def test_snapshot_and_fetch_are_immutable_and_readonly(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    m = mutation()
    publish_batch(remote, remote.read(), (m,))
    lock = (remote.root / REMOTE_STATE).parent / "remote.lock"
    lock.unlink()
    before = files(remote.root)
    snapshot = remote.read()
    payloads = remote.fetch(snapshot, [m.id, m.id])
    assert files(remote.root) == before
    assert payloads == {m.id: m.payload}
    with pytest.raises(TypeError):
        snapshot.resources[m.id] = m.after
    with pytest.raises(TypeError):
        payloads[m.id] = m.payload
    source = dict(snapshot.resources)
    copied = RemoteSnapshot("opaque identity", 0, source)
    source.clear()
    assert copied.resources == snapshot.resources


@pytest.mark.parametrize("mismatch", ["revision", "identity"])
def test_empty_fetch_validates_the_snapshot(tmp_path, mismatch):
    remote = FilesystemRemote.create(tmp_path / "center")
    old = remote.read()
    if mismatch == "revision":
        publish_batch(remote, old, [mutation()])
    else:
        old = replace(old, sync_id="another opaque center")
    before = files(remote.root)
    with pytest.raises(SnapshotExpired):
        remote.fetch(old, [])
    assert files(remote.root) == before


@pytest.mark.parametrize(
    "invalid", ["before", "duplicate", "fingerprint", "references", "credential"]
)
def test_entire_invalid_batch_leaves_content_and_revision_unchanged(tmp_path, invalid):
    remote = FilesystemRemote.create(tmp_path / "center")
    old = remote.read()
    first, second = mutation(), mutation("two")
    if invalid == "before":
        second = replace(second, before=second.after)
    elif invalid == "duplicate":
        second = first
    elif invalid == "fingerprint":
        second = replace(second, after=replace(second.after, fingerprint="wrong"))
    elif invalid == "references":
        second = replace(
            second, after=replace(second.after, references=("agent:missing",))
        )
    else:
        second = mutation("two", b'api_key = "abcdefghijklmnopqrstuv"\n')
    before = files(remote.root)
    with pytest.raises((InvalidContent, SnapshotExpired)):
        publish_batch(remote, old, [first, second])
    assert files(remote.root) == before
    assert remote.read() == old


def test_fetch_missing_content_and_corrupt_manifest_fail_without_writes(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    snapshot = publish_batch(remote, remote.read(), [mutation()])
    before = files(remote.root)
    with pytest.raises(InvalidContent, match="Missing requested"):
        remote.fetch(snapshot, ["memory:notes/missing.md"])
    assert files(remote.root) == before
    manifest = remote.root / REMOTE_STATE
    raw = json.loads(manifest.read_text())
    raw["payload_hashes"]["memory:notes/one.md"] = "corrupted"
    manifest.write_text(json.dumps(raw))
    before = files(remote.root)
    with pytest.raises(InvalidContent, match="integrity mismatch"):
        remote.fetch(snapshot, [])
    assert files(remote.root) == before


def test_legacy_manifest_gains_hashes_only_on_commit(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    manifest = remote.root / REMOTE_STATE
    raw = json.loads(manifest.read_text())
    raw.pop("payload_hashes")
    manifest.write_text(json.dumps(raw))
    before = files(remote.root)
    snapshot = remote.read()
    remote.fetch(snapshot, [])
    assert before == files(remote.root)
    publish_batch(remote, snapshot, [mutation()])
    assert json.loads(manifest.read_text())["payload_hashes"]


@pytest.mark.parametrize("relation", ["same", "inside", "ancestor"])
def test_backend_attachment_preserves_overlap_guard(tmp_path, relation):
    local = _workspace(tmp_path / "local")
    center = {"same": local, "inside": local / "skills/center", "ancestor": tmp_path}[
        relation
    ]
    store = StoreFacade(FilesystemRemote(center))
    with pytest.raises(WorkspaceReconcileError, match="must be separate"):
        build_reconcile_plan(local, store)


def test_client_rejects_corrupt_fetch_from_protocol_backend(tmp_path, protocol_store):
    local = _workspace(tmp_path / "local")
    remote = protocol_store
    publish_batch(remote, remote.read(), [mutation()])

    class CorruptStore(StoreFacade):
        def fetch(self, expected, ids):
            result = dict(super().fetch(expected, ids))
            if result:
                result[next(iter(result))] = FilePayload(b"corrupted")
            return result

    before = files(local), stored_content(remote)
    with pytest.raises(WorkspaceReconcileError, match="hash"):
        build_reconcile_plan(local, CorruptStore(remote))
    assert before == (files(local), stored_content(remote))


def test_client_blocks_credential_download_independently_of_backend(
    tmp_path, protocol_store
):
    local = _workspace(tmp_path / "local")
    remote = protocol_store
    secret = mutation("secret", b'api_key = "abcdefghijklmnopqrstuv"\n')

    class OpaqueStore(StoreFacade):
        def read(self):
            snapshot = super().read()
            return replace(snapshot, resources={secret.id: secret.after})

        def fetch(self, expected, ids):
            return {identity: secret.payload for identity in ids}

    before = files(local), stored_content(remote)
    plan = build_reconcile_plan(local, OpaqueStore(remote))
    item = next(item for item in plan.items if item.id == secret.id)
    assert item.action == "BLOCKED" and item.target is None
    assert "credential" in item.reason
    assert before == (files(local), stored_content(remote))


def test_download_is_staged_before_conditional_commit_and_local_base(
    tmp_path, protocol_store
):
    local = _workspace(tmp_path / "local")
    remote = protocol_store
    run_reconciliation(local, remote, tmp_path / "home", dry_run=False)
    publish_batch(remote, remote.read(), [mutation()])
    (local / "memory/notes/upload.md").write_text("upload")
    before = files(local)

    class RefusingStore(StoreFacade):
        def commit(self, request):
            current = files(local)
            assert current.pop(PENDING_COMMIT_STATE)
            assert current == before
            raise SnapshotExpired("Rejected staged application")

    with pytest.raises(WorkspaceReconcileError, match="Rejected staged"):
        run_reconciliation(
            local, RefusingStore(remote), tmp_path / "home", dry_run=False
        )
    assert files(local) == before


def test_client_rejects_invalid_download_references_on_both_stores(
    tmp_path, protocol_store
):
    local = _workspace(tmp_path / "local")
    payload = FilePayload(b'agents = ["missing"]\ncommand = "tool"\n')
    descriptor = ResourceDescriptor(
        value_fingerprint({"agents": ["missing"], "command": "tool"}),
        payload_hash(payload),
        ("agent:missing",),
    )

    class OpaqueStore(StoreFacade):
        def read(self):
            return replace(super().read(), resources={"mcp:orphan": descriptor})

        def fetch(self, expected, ids):
            return {identity: payload for identity in ids}

    before = files(local), stored_content(protocol_store)
    plan = build_reconcile_plan(local, OpaqueStore(protocol_store))
    assert plan.blocked
    assert "mcp:orphan" in {item.id for item in plan.conflicts}
    assert before == (files(local), stored_content(protocol_store))


def test_center_change_between_read_and_fetch_preserves_local_base(
    tmp_path, protocol_store
):
    local = _workspace(tmp_path / "local")
    home = tmp_path / "home"
    run_reconciliation(local, protocol_store, home, dry_run=False)
    publish_batch(protocol_store, protocol_store.read(), [mutation()])
    plan = build_reconcile_plan(local, protocol_store)
    before = files(local)

    class RacingStore(StoreFacade):
        def fetch(self, expected, ids):
            publish_batch(
                protocol_store, protocol_store.read(), [mutation("concurrent")]
            )
            return super().fetch(expected, ids)

    with pytest.raises(WorkspaceReconcileError, match="revision changed"):
        apply_reconcile_plan(plan, home, remote=RacingStore(protocol_store))
    assert files(local) == before
    assert protocol_store.read().revision == plan.revision + 1
