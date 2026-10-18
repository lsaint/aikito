"""Exercise fail-closed format handling through authenticated CLI calls."""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from workspace_remote_cli_smoke import run_cli
from workspace_store_setup import publish_batch

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
)
from aikito.workspace.remote_protocol import Operation, decode_request
from aikito.workspace.resource_state import (
    PENDING_COMMIT_STATE,
    REMOTE_BINDING_STATE,
    WORKER_LOCK_STATE,
    WORKER_LOG_STATE,
    WORKER_PID_STATE,
    WORKER_STATUS_STATE,
    WORKER_STOP_STATE,
)

TOKEN_ENV = "AIKITO_FORMAT_SMOKE_TOKEN"
TOKEN = "format-smoke-test-token"


def files(root: Path) -> dict[str, bytes]:
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in root.rglob("*")
        if p.is_file()
        and p.relative_to(root).as_posix()
        not in {
            WORKER_LOCK_STATE,
            WORKER_PID_STATE,
            WORKER_STATUS_STATE,
            WORKER_LOG_STATE,
            WORKER_STOP_STATE,
        }
    }


def main(base: Path) -> None:
    for case in ("plaintext", "mixed", "unknown", "orphan", "future"):
        root = base / case
        local = _workspace(root / "local")
        fresh = _workspace(root / "fresh")
        home = root / "home"
        home.mkdir(parents=True)
        note = local / "memory/notes/retained.md"
        note.write_text("Retained local content\n", encoding="utf-8")
        backend = InMemoryRemote("a" * 32)
        env = {TOKEN_ENV: TOKEN}
        with HTTPRemoteServer(backend, token=TOKEN) as server:
            bound = run_cli(
                ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
                workspace=local,
                home=home,
                env_vars=env,
            )
            assert bound.returncode == 0, bound.stderr
            payload = FilePayload(('{"format":"' + case + '"}').encode())
            identity = {
                "orphan": "e1:tag:digest",
                "future": "format:encrypted-v2",
                "unknown": "format:unknown",
                "plaintext": "global-memory:notes/legacy.md",
                "mixed": "global-memory:notes/mixed.md",
            }[case]
            descriptor = ResourceDescriptor(
                hashlib.sha256(payload.data).hexdigest(),
                payload_hash(payload),
                len(encode_payload(payload)),
            )
            publish_batch(
                backend,
                backend.read(),
                [ResourceMutation(identity, None, descriptor, payload)],
            )
            before = files(local)
            fresh_before = files(fresh)
            remote_before = backend.read()
            for label, args, workspace in (
                (
                    "bind",
                    ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
                    fresh,
                ),
                ("preview", ["remote", "sync", "--dry-run"], local),
                ("apply", ["remote", "sync", "--apply"], local),
                ("watch", ["remote", "watch", "--once"], local),
            ):
                start = len(server.requests)
                result = run_cli(args, workspace=workspace, home=home, env_vars=env)
                assert result.returncode != 0, (case, label, result.stdout)
                assert "encrypted_store_unsupported" in result.stderr, result.stderr
                assert TOKEN not in result.stdout + result.stderr
                assert identity not in result.stdout + result.stderr
                operations = [
                    decode_request(raw).operation for raw in server.requests[start:]
                ]
                assert operations and set(operations) <= {Operation.READ}, operations
                (root / f"{label}.err").write_text(result.stderr, encoding="utf-8")
                assert backend.read() == remote_before
                assert files(local) == before
                assert files(fresh) == fresh_before
            assert not (fresh / REMOTE_BINDING_STATE).exists()
            assert not (local / PENDING_COMMIT_STATE).exists()
    (base / "checked.txt").write_text(
        "Encrypted format CLI guards passed\n", encoding="utf-8"
    )
    print("[SUCCESS] Encrypted format CLI guards passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
