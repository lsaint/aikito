"""Exercise reconciliation through a real bytes boundary and assert artifacts."""

from __future__ import annotations

import sys
from pathlib import Path

from workspace_loopback_remote import FaultyLoopback, LoopbackTransport
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace
from workspace_serialized_attachment import FilesystemSerializedRemote

from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.resource_state import PENDING_COMMIT_STATE
from aikito.workspace.remote_store import InvalidContent


def main(base: Path) -> None:
    a, b = _workspace(base / "left"), _workspace(base / "right")
    home = base / "home"
    backend = FilesystemRemote.create(base / "center")
    transport = LoopbackTransport(backend)
    remote = FilesystemSerializedRemote(transport.exchange, backend.root)

    # Same-machine attachment checks never send physical paths over the wire.
    for overlapping in (backend.root, base, backend.root / "replica"):
        try:
            remote.validate_replica(overlapping)
        except InvalidContent:
            pass
        else:
            raise AssertionError("Overlapping filesystem attachment was accepted")
    assert not transport.requests

    for local in (a, b):
        run_reconciliation(local, remote, home, dry_run=False)

    write(a, "memory/notes/serialized-smoke.md", "first")
    write(a, "projects/demo/memory/notes/serialized-smoke.md", "first")
    run_reconciliation(a, remote, home, dry_run=False)
    run_reconciliation(b, remote, home, dry_run=False)
    assert (b / "memory/notes/serialized-smoke.md").read_text() == "first"
    assert (b / "projects/demo/memory/notes/serialized-smoke.md").read_text() == "first"
    assert transport.requests and transport.responses
    assert all(type(item) is bytes for item in transport.requests + transport.responses)

    # Drop one committed response at the bytes layer, then recover by identity.
    lossy = FaultyLoopback(backend, "lost")
    lossy.arm()
    recovery = FilesystemSerializedRemote(lossy.exchange, backend.root)
    write(a, "memory/notes/serialized-recovery.md", "recovered")
    try:
        run_reconciliation(a, recovery, home, dry_run=False)
    except WorkspaceReconcileError:
        pass
    else:
        raise AssertionError("Lost commit response must not be treated as success")
    assert "memory:notes/serialized-recovery.md" in backend.read().resources
    assert (a / PENDING_COMMIT_STATE).is_file()
    assert PendingCommitStore(a, home).load() is not None

    run_reconciliation(a, recovery, home, dry_run=False)
    assert not (a / PENDING_COMMIT_STATE).exists()
    run_reconciliation(b, remote, home, dry_run=False)
    assert (b / "memory/notes/serialized-recovery.md").read_text() == "recovered"


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Serialized RemoteStore boundary checks passed")
