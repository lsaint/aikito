"""Run receipt-aware reconciliation across independent client processes."""

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
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_store import CommitOutcomeUnknown
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.replica_state import load_replica_state
from workspace_reconcile_smoke import _workspace


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--stage", required=True, choices=("send", "resume"))
    args = parser.parse_args()
    root = args.root.resolve()
    local, home = root / "local", root / "home"
    if args.stage == "send":
        _workspace(local)
        remote = FilesystemRemote.create(root / "center")
        payload = FilePayload(b"planned download")
        mutation = ResourceMutation(
            "memory:notes/download.md",
            None,
            ResourceDescriptor(
                hashlib.sha256(payload.data).hexdigest(),
                payload_hash(payload),
                len(encode_payload(payload)),
            ),
            payload,
        )
        remote.commit(
            build_commit_request("other-client", "seed", remote.read(), (mutation,))
        )
        (local / "memory/notes/upload.md").write_text("original upload")
        commit = remote.commit

        def lose_response(request):
            result = commit(request)
            assert result.accepted_revision == 2
            raise CommitOutcomeUnknown("accepted response lost")

        remote.commit = lose_response
        try:
            run_reconciliation(local, remote, home, dry_run=False)
        except WorkspaceReconcileError as exc:
            assert "accepted response lost" in str(exc)
        else:
            raise AssertionError("Expected unknown outcome")
        store = PendingCommitStore(local, home)
        pending = store.load()
        assert pending is not None
        assert load_replica_state(local)[0].base == {}
        (root / "request-id.txt").write_text(pending.request.request_id)
        (local / "memory/notes/upload.md").write_text("new user upload")
        (local / "memory/notes/download.md").write_text("user edit after response loss")
    else:
        remote = FilesystemRemote(root / "center")
        plan = run_reconciliation(local, remote, home, dry_run=False)
        assert {item.id for item in plan.conflicts} == {"memory:notes/download.md"}
        assert (
            local / "memory/notes/download.md"
        ).read_text() == "user edit after response loss"
        assert (remote.root / "memory/notes/upload.md").read_text() == "new user upload"
        state, _ = load_replica_state(local)
        assert "memory:notes/download.md" not in state.base
        assert state.receipt_cursor.request_id != (root / "request-id.txt").read_text()
        assert state.revision == remote.read().revision == 3
        assert PendingCommitStore(local, home).load() is None
        (root / "checked.json").write_text(
            json.dumps({"revision": state.revision, "conflicts": 1})
        )


if __name__ == "__main__":
    main()
