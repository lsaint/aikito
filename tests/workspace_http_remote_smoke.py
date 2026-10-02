"""Run portable reconciliation acceptance and lost-response recovery over HTTP."""

from __future__ import annotations

import sys
import os
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_acceptance import exercise_behavior
from workspace_reconcile_backend import StoreBackend

from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.serialized_remote import SerializedRemoteStore
from aikito.workspace.skill_metadata import (
    read_executable_metadata,
    write_executable_metadata,
)


class HTTPBackend(StoreBackend):
    def checkpoint(self):
        snapshot = self.remote.read()
        return snapshot, self.remote.fetch(snapshot, tuple(snapshot.resources))


def main(base: Path):
    backend = FilesystemRemote.create(base / "center")
    with HTTPRemoteServer(backend) as server:
        driver = HTTPBackend(SerializedRemoteStore(HTTPTransport(server.url).exchange))
        exercise_behavior(base, driver)
        local, other, home = base / "moved-left", base / "right", base / "home"
        note = local / "memory/notes/http-recovery.md"
        note.write_text("recovered over HTTP\n", encoding="utf-8")
        revision = driver.revision()
        server.arm("drop_after_handler", operation=Operation.COMMIT)
        try:
            driver.run(local, home)
        except WorkspaceReconcileError:
            pass
        else:
            raise AssertionError("Lost HTTP commit response was accepted locally")
        pending = PendingCommitStore(local, home).load()
        assert pending is not None
        identity = pending.request.request_id
        assert driver.revision() == revision + 1
        driver.run(local, home)
        assert PendingCommitStore(local, home).load() is None
        assert driver.revision() == revision + 1
        commits = [
            decode_request(value).body
            for value in server.requests
            if decode_request(value).operation == Operation.COMMIT
        ]
        assert sum(req.request_id == identity for req in commits) == 1
        driver.run(other, home)
        assert (
            other / "memory/notes/http-recovery.md"
        ).read_bytes() == note.read_bytes()
        script = local / "skills/example/run.sh"
        if os.name == "nt":
            write_executable_metadata(
                script.parent / ".aikito-executable.json", ["run.sh"]
            )
        else:
            script.chmod(script.stat().st_mode | 0o111)
        driver.run(local, home)
        driver.run(other, home)
        received = other / "skills/example/run.sh"
        if os.name == "nt":
            assert "run.sh" in read_executable_metadata(
                received.parent / ".aikito-executable.json"
            )
        else:
            assert received.stat().st_mode & 0o111
    (base / "checked.txt").write_text("HTTP acceptance passed\n", encoding="utf-8")
    print("[SUCCESS] HTTP reconciliation and recovery checks passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
