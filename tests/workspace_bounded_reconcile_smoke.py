"""Synchronize a large project over multiple bounded serialized rounds."""

from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import patch

from aikito.workspace import remote_limits
from aikito.workspace.reconcile import run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_protocol import (
    Operation,
    decode_request,
    encode_fetch_response,
)
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.serialized_remote import SerializedRemoteStore
from workspace_loopback_remote import LoopbackTransport
from workspace_reconcile_smoke import _workspace


def exercise(base: Path) -> None:
    left, right = _workspace(base / "left"), _workspace(base / "right")
    backend = FilesystemRemote.create(base / "center")
    transport = LoopbackTransport(backend)
    exchanges = []

    def exchange(message):
        assert len(message) <= remote_limits.MAX_REMOTE_REQUEST_BYTES
        response = transport.exchange(message)
        assert len(response) <= remote_limits.MAX_REMOTE_RESPONSE_BYTES
        exchanges.append(decode_request(message).operation)
        return response

    remote = SerializedRemoteStore(exchange)
    for index in range(12):
        note = left / f"projects/demo/memory/notes/{index:02}.md"
        note.write_text(f"# Note {index}\n" + "x" * 2500, encoding="utf-8")
    with (
        patch.object(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 16 * 1024),
        patch.object(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 16 * 1024),
    ):
        first = run_reconciliation(left, remote, base / "home", dry_run=False)
        assert first.rounds > 1 and not first.stop_reason
        assert exchanges.count(Operation.COMMIT) == first.rounds
        snapshot = backend.read()
        assert (
            len(encode_fetch_response(backend.fetch(snapshot, snapshot.resources)))
            > remote_limits.MAX_REMOTE_RESPONSE_BYTES
        )
        exchanges.clear()
        second = run_reconciliation(right, remote, base / "home", dry_run=False)
        assert second.rounds > 1 and not second.conflicts and not second.deferred
        assert exchanges.count(Operation.FETCH) == second.rounds
        assert not load_replica_state(right)[0].unpaired_ids
    for index in range(12):
        relative = f"projects/demo/memory/notes/{index:02}.md"
        assert (left / relative).read_bytes() == (right / relative).read_bytes()
    (base / "checked.txt").write_text(
        "Bounded reconciliation passed\n", encoding="utf-8"
    )
    print("[SUCCESS] Bounded reconciliation passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    exercise(parser.parse_args().base.resolve())
