"""Kill a filesystem store process at journal checkpoints and recover receipts."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

from aikito.workspace import transactions
from aikito.workspace.payload import encode_payload
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE
from aikito.workspace.remote_store import RecoveryRequired
from aikito.workspace.remote_wire import (
    build_commit_request,
    decode_commit_request,
    encode_commit_request,
    validate_commit_result,
)

CHECKPOINTS = ("journal", "resource", "manifest", "committed", "cleanup")


def crash_commit(base: Path, checkpoint: str) -> None:
    remote = FilesystemRemote(base / "center")
    request = decode_commit_request((base / "request.json").read_bytes())
    atomic, move, cleanup = transactions.atomic_text, os.replace, transactions._cleanup

    def write(path, text):
        atomic(path, text)
        if "workspace-transactions" in path.parts and path.name == "pending.json":
            phase = json.loads(text)["phase"]
            if (checkpoint == "journal" and phase == "pending") or (
                checkpoint == "committed" and phase == "committed"
            ):
                os._exit(73)

    def replace_file(src, dst):
        move(src, dst)
        if (
            checkpoint == "resource"
            and Path(dst) == remote.root / "memory/notes/receipt.md"
        ) or (checkpoint == "manifest" and Path(dst) == remote.root / REMOTE_STATE):
            os._exit(73)

    def clean(*args):
        if checkpoint == "cleanup":
            os._exit(73)
        cleanup(*args)

    transactions.atomic_text, os.replace, transactions._cleanup = (
        write,
        replace_file,
        clean,
    )
    remote.commit(request)
    raise AssertionError("Crash checkpoint was not reached")


def exercise(base: Path) -> None:
    for checkpoint in CHECKPOINTS:
        case = base / checkpoint
        remote = FilesystemRemote.create(case / "center")
        payload = FilePayload(b"# Durable receipt\r\n")
        mutation = ResourceMutation(
            "memory:notes/receipt.md",
            None,
            ResourceDescriptor(
                hashlib.sha256(payload.data).hexdigest(),
                payload_hash(payload),
                len(encode_payload(payload)),
            ),
            payload,
        )
        request = build_commit_request(
            "smoke-client", "smoke-request", remote.read(), (mutation,)
        )
        (case / "request.json").write_bytes(encode_commit_request(request))
        result = subprocess.run(
            [sys.executable, __file__, str(case), "--crash-at", checkpoint], check=False
        )
        assert result.returncode == 73
        reopened = FilesystemRemote(remote.root)
        args = (
            request.expected.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )
        try:
            reopened.resolve_commit(*args)
        except RecoveryRequired:
            pass
        else:
            raise AssertionError("Unrecovered journal returned a receipt outcome")
        assert reopened.recover()
        retained = checkpoint in {"committed", "cleanup"}
        assert reopened.read().revision == int(retained)
        assert (reopened.resolve_commit(*args) is not None) == retained
        receipt = reopened.commit(request)
        validate_commit_result(request, receipt)
        assert reopened.commit(request) == reopened.resolve_commit(*args) == receipt
        assert reopened.read().revision == 1
        assert (remote.root / "memory/notes/receipt.md").read_bytes() == payload.data
        (case / "checked.json").write_text(
            json.dumps(
                {
                    "checkpoint": checkpoint,
                    "accepted_revision": receipt.accepted_revision,
                }
            ),
            encoding="utf-8",
        )
    print("[SUCCESS] Filesystem receipt crash recovery passed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("--crash-at", choices=CHECKPOINTS)
    args = parser.parse_args()
    if args.crash_at:
        crash_commit(args.base.resolve(), args.crash_at)
    else:
        exercise(args.base.resolve())
