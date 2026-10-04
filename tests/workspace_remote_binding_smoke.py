"""Authenticated binding, restart recovery and private state safety over sockets."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_access import RemoteBindingError
from aikito.workspace.remote_binding import (
    RemoteAuth,
    load_remote_binding,
    remove_remote_binding,
)
from aikito.workspace.remote_factory import bind_remote, open_bound_remote
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.resource_state import REMOTE_BINDING_STATE, PENDING_COMMIT_STATE
from aikito.workspace.transactions import WorkspaceCoreError

TOKEN = "binding-smoke-secret"
AUTH = RemoteAuth("bearer_env", "AIKITO_REMOTE_TEST_TOKEN")
ENV = {AUTH.env: TOKEN}


def main(base: Path):
    local, home = _workspace(base / "local"), base / "home"
    backend = FilesystemRemote.create(base / "center")
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        binding = bind_remote(local, server.url, AUTH, home=home, environ=ENV)
        path = local / REMOTE_BINDING_STATE
        assert TOKEN not in path.read_text(encoding="utf-8")
        assert load_remote_binding(local) == binding
        if os.name != "nt":
            assert path.stat().st_mode & 0o777 == 0o600

        def run(environ=ENV):
            return run_reconciliation(
                local, open_bound_remote(local, environ=environ), home, dry_run=False
            )

        run()
        note = local / "memory/notes/binding.md"
        note.write_text("bound remote recovery\n", encoding="utf-8")
        before = backend.read().revision
        # Reject only this commit after planning has succeeded.
        server.arm("reject_403", operation=Operation.COMMIT)
        try:
            run()
        except WorkspaceReconcileError as exc:
            assert "authorization denied" in str(exc)
        else:
            raise AssertionError("Rejected commit succeeded")
        pending = PendingCommitStore(local, home).load()
        assert pending is not None
        pending_path = local / PENDING_COMMIT_STATE
        original = pending_path.read_bytes()
        assert backend.read().revision == before
        for environ in ({}, {AUTH.env: "wrong-smoke-token"}):
            try:
                run(environ)
            except (RemoteBindingError, WorkspaceReconcileError):
                pass
            else:
                raise AssertionError("Unavailable credential succeeded")
            assert pending_path.read_bytes() == original
            assert backend.read().revision == before
        try:
            remove_remote_binding(local, home=home)
        except RemoteBindingError:
            pass
        else:
            raise AssertionError("Pending binding was removed")
        # Leave evidence for CI file assertions after successful recovery.
        (base / "auth-checked.txt").write_text(
            "Pending preserved; revision unchanged\n", encoding="utf-8"
        )
        run()
        assert backend.read().revision == before + 1
        assert not pending_path.exists()
        assert (
            backend.root / "memory/notes/binding.md"
        ).read_bytes() == note.read_bytes()
        note.write_text("lost response recovered\n", encoding="utf-8")
        before = backend.read().revision
        server.arm("drop_after_handler", operation=Operation.COMMIT)
        try:
            run()
        except WorkspaceReconcileError:
            pass
        else:
            raise AssertionError("Lost response succeeded locally")
        pending = PendingCommitStore(local, home).load()
        identity = pending.request.request_id
        assert backend.read().revision == before + 1
        run()
        assert not pending_path.exists()
        assert backend.read().revision == before + 1
        commits = [
            decode_request(value).body
            for value in server.requests
            if decode_request(value).operation == Operation.COMMIT
        ]
        assert sum(item.request_id == identity for item in commits) == 1
        # Atomic state loading does not follow a replacement symlink.
        unsafe = base / "unsafe"
        unsafe.mkdir()
        link = unsafe / ".local"
        try:
            link.symlink_to(local / ".local", target_is_directory=True)
        except OSError:
            if os.name != "nt":
                raise
        else:
            try:
                load_remote_binding(unsafe)
            except WorkspaceCoreError:
                pass
            else:
                raise AssertionError("Unsafe state symlink followed")
        # Unbinding preserves replica identity, resources and remote contents.
        replica = load_replica_state(local)
        remove_remote_binding(local, home=home)
        assert load_replica_state(local) == replica
        assert note.exists()
        bind_remote(local, server.url, AUTH, home=home, environ=ENV)
        assert all(TOKEN.encode() not in value for value in server.requests)
    (base / "checked.txt").write_text("Binding checks passed\n", encoding="utf-8")
    print("[SUCCESS] Remote binding and recovery checks passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
