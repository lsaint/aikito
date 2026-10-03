"""Persist, complete and clean up pending in three independent processes."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from aikito.workspace.payload import encode_payload
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.replica_state import ReplicaState, load_replica_state
from aikito.workspace.resource_state import local_resource_for_id
from workspace_reconcile_smoke import _workspace


def exercise(base: Path, stage: str) -> None:
    local, home = base / "local", base / "home"
    store = PendingCommitStore(local, home)
    if stage == "prepare":
        _workspace(local)
        remote = FilesystemRemote.create(base / "center")
        payload = FilePayload(b"# Accepted upload\r\n")
        mutation = ResourceMutation(
            "memory:notes/pending.md",
            None,
            ResourceDescriptor(
                hashlib.sha256(payload.data).hexdigest(),
                payload_hash(payload),
                len(encode_payload(payload)),
            ),
            payload,
        )
        (local / "memory/notes/pending.md").write_bytes(payload.data)
        request = build_commit_request(
            "a" * 32, "stable-pending-request", remote.read(), (mutation,)
        )
        pending = store.persist(
            request,
            safe_resource_ids=(mutation.id,),
            excluded_resource_ids=("memory:notes/blocked.md",),
        )
        remote.commit(pending.request)
        (local / "memory/notes/pending.md").write_bytes(b"# Local edit after upload\n")
    elif stage == "complete":
        pending = store.load()
        assert pending is not None
        remote = FilesystemRemote(base / "center")
        request = pending.request
        receipt = remote.resolve_commit(
            request.expected.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )
        assert receipt is not None
        mutation = request.mutations[0]
        resource = local_resource_for_id(mutation.id, mutation.after.fingerprint)
        state = ReplicaState(
            receipt.sync_id,
            receipt.client_id,
            receipt.accepted_revision,
            {resource.id: resource},
        )
        store.complete(pending, receipt, state)
        assert store.load() == pending
    else:
        # This process does not open or query the remote at all.
        pending = store.load()
        assert pending is not None
        state, _ = load_replica_state(local)
        assert pending.completed_by(state)
        store.clear(pending)
        assert store.load() is None
    assert (
        local / "memory/notes/pending.md"
    ).read_bytes() == b"# Local edit after upload\n"
    (base / f"{stage}-checked.json").write_text(
        json.dumps(
            {"stage": stage, "replica_id": load_replica_state(local)[0].replica_id}
        ),
        encoding="utf-8",
    )
    print(f"[SUCCESS] Pending {stage} checks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument(
        "--stage", choices=("prepare", "complete", "clear"), default="prepare"
    )
    args = parser.parse_args()
    exercise(args.base.resolve(), args.stage)
