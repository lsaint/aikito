"""Reject oversized commits before pending and block oversized single resources."""

from __future__ import annotations

import argparse
from pathlib import Path

from workspace_loopback_remote import LoopbackTransport
from workspace_reconcile_smoke import _workspace

from aikito.workspace import remote_limits
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.serialized_remote import SerializedRemoteStore


def exercise(base: Path) -> None:
    local = _workspace(base / "local")
    home = base / "home"
    backend = FilesystemRemote.create(base / "center")
    transport = LoopbackTransport(backend)
    commits = []

    def exchange(message):
        if decode_request(message).operation == Operation.COMMIT:
            commits.append(message)
        return transport.exchange(message)

    remote = SerializedRemoteStore(exchange)
    run_reconciliation(local, remote, home, dry_run=False)
    commits.clear()
    previous_state = load_replica_state(local)
    previous_snapshot = backend.read()
    note = local / "memory/notes/capacity.md"
    # The payload fits, but Base64 and the full protocol envelope do not.
    note.write_bytes(b"x" * (12 * 1024))
    original_limit = remote_limits.MAX_REMOTE_REQUEST_BYTES
    remote_limits.MAX_REMOTE_REQUEST_BYTES = 16 * 1024
    try:
        result = run_reconciliation(local, remote, home, dry_run=False)
        item = next(
            item for item in result.items if item.id == "memory:notes/capacity.md"
        )
        assert item.action == "BLOCKED" and "wire budget" in item.reason
        assert commits == []
        assert PendingCommitStore(local, home).load() is None
        assert load_replica_state(local) == previous_state
        assert backend.read() == previous_snapshot
        assert note.read_bytes() == b"x" * (12 * 1024)
        note.write_bytes(b"smaller\n")
        run_reconciliation(local, remote, home, dry_run=False)
        assert len(commits) == 1
        assert PendingCommitStore(local, home).load() is None
        assert (backend.root / "memory/notes/capacity.md").read_bytes() == b"smaller\n"
    finally:
        remote_limits.MAX_REMOTE_REQUEST_BYTES = original_limit
    # A single oversized resource blocks only itself; neighbors still sync.
    original_resource_limit = remote_limits.MAX_RESOURCE_PAYLOAD_BYTES
    remote_limits.MAX_RESOURCE_PAYLOAD_BYTES = 4096
    try:
        (local / "memory/notes/oversized.md").write_bytes(b"x" * 4096)
        (local / "memory/notes/neighbor.md").write_bytes(b"neighbor\n")
        plan = run_reconciliation(local, remote, home, dry_run=False)
        item = next(i for i in plan.items if i.id == "memory:notes/oversized.md")
        assert item.action == "BLOCKED" and "size limit" in item.reason
        assert not (backend.root / "memory/notes/oversized.md").exists()
        assert (backend.root / "memory/notes/neighbor.md").read_bytes() == (
            b"neighbor\n"
        )
        assert PendingCommitStore(local, home).load() is None
    finally:
        remote_limits.MAX_RESOURCE_PAYLOAD_BYTES = original_resource_limit
    (base / "checked.txt").write_text(
        "Commit capacity checks passed\n", encoding="utf-8"
    )
    print("[SUCCESS] Commit capacity checks passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    exercise(parser.parse_args().base.resolve())
