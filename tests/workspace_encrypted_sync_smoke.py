"""Real HTTP, independent devices, CAS races and stable ciphertext retries."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.encrypted_remote import decode_recovery_code
from aikito.workspace.encrypted_sync import INITIALIZATION_STATE
from aikito.workspace.pending_commit import PendingCommit
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_protocol import decode_request, encode_commit_request
from aikito.workspace.resource_state import PENDING_COMMIT_STATE


class RacingRemote(InMemoryRemote):
    def __init__(self):
        super().__init__("11223344556677889900aabbccddeeff")
        self.barrier = threading.Barrier(2)

    def commit(self, request):
        if (
            len(request.mutations) == 1
            and request.mutations[0].id == FORMAT_RESOURCE_ID
        ):
            self.barrier.wait(timeout=30)
        return super().commit(request)


def main(base: Path):
    base.mkdir(parents=True)
    backend = RacingRemote()
    homes, workspaces, envs = [], [], []
    for index in range(3):
        home = base / f"home-{index}"
        workspace = base / f"workspace-{index}"
        config = base / f"config-{index}"
        home.mkdir()
        env = {
            **os.environ,
            "HOME": str(home),
            "USERPROFILE": str(home),
            "XDG_CONFIG_HOME": str(config),
            "APPDATA": str(config),
            "AIKITO_DIR": str(workspace),
            "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
            "AIKITO_REMOTE_TOKEN": "encrypted-sync-smoke-token",
        }
        homes.append(home)
        workspaces.append(workspace)
        envs.append(env)

    def command(index, args, input=None):
        return subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=envs[index],
            input=input,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

    def key_sync(index, mode="--apply"):
        return command(
            index,
            ["remote", "key", "sync", mode, "--workspace", str(workspaces[index])],
        )

    for index in range(3):
        result = command(index, ["init", "workspace", str(workspaces[index])])
        assert result.returncode == 0, result.stderr
    with HTTPRemoteServer(backend, token="encrypted-sync-smoke-token") as server:
        for index in (0, 1):
            result = command(
                index,
                ["remote", "bind", server.url, "--workspace", str(workspaces[index])],
            )
            assert result.returncode == 0, result.stderr
        processes = [
            subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "aikito",
                    "remote",
                    "key",
                    "initialize",
                    "--workspace",
                    str(workspaces[index]),
                    "--recovery-output-fd",
                    "1",
                ],
                env=envs[index],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            for index in (0, 1)
        ]
        outcomes = [process.communicate(timeout=60) for process in processes]
        assert sorted(process.returncode for process in processes) == [0, 1], [
            stderr for _, stderr in outcomes
        ]
        winner = next(
            index for index, process in enumerate(processes) if process.returncode == 0
        )
        code = outcomes[winner][0].strip()
        decode_recovery_code(code)
        snapshot = backend.read()
        assert set(snapshot.resources) == {FORMAT_RESOURCE_ID}
        assert all(
            not (workspace / INITIALIZATION_STATE).exists() for workspace in workspaces
        )
        # A fresh device was never bound while this Store was plaintext.
        joined = command(
            2,
            [
                "remote",
                "key",
                "recover",
                "--endpoint",
                server.url,
                "--workspace",
                str(workspaces[2]),
                "--recovery-fd",
                "0",
            ],
            code,
        )
        assert joined.returncode == 0, joined.stderr
        a, b = workspaces[winner], workspaces[2]
        note = Path("memory/notes/e2ee.md")
        (a / note).parent.mkdir(parents=True, exist_ok=True)
        (a / note).write_text("Encrypted first device content\n")
        preview_before = backend.read()
        preview = key_sync(winner, "--dry-run")
        assert preview.returncode == 0 and backend.read() == preview_before
        assert not (a / ".local/state/aikito/encrypted-discovery.json").exists()
        first = key_sync(winner)
        assert first.returncode == 0, first.stderr
        second = key_sync(2)
        assert second.returncode == 0, second.stderr
        assert (b / note).read_text() == "Encrypted first device content\n"
        unchanged = backend.read()
        assert key_sync(winner).returncode == 0 and backend.read() == unchanged
        for fault in ("drop_after_request", "drop_after_handler"):
            (a / note).write_text(f"Encrypted {fault} content\n")
            server.arm(fault, operation="commit")
            failed = key_sync(winner)
            assert failed.returncode != 0 and (a / PENDING_COMMIT_STATE).exists()
            # Set env for the local test-only inspection of the pending file.
            pending_path = a / PENDING_COMMIT_STATE
            pending = PendingCommit.decode(pending_path.read_bytes())
            exact = encode_commit_request(pending.request)
            assert pending.vault_id and code.encode() not in pending_path.read_bytes()
            assert (
                f"Encrypted {fault} content".encode() not in pending_path.read_bytes()
            )
            assert key_sync(winner).returncode == 0
            assert not pending_path.exists()
            count = sum(request == exact for request in server.requests)
            assert count == (2 if fault == "drop_after_request" else 1)
            assert key_sync(2).returncode == 0
            assert (b / note).read_text() == f"Encrypted {fault} content\n"
        # Late local changes win, retaining the replaced authenticated payload.
        (a / note).write_text("Earlier accepted encrypted edit\n")
        assert key_sync(winner).returncode == 0
        (b / note).write_text("Later encrypted edit\n")
        assert key_sync(2).returncode == 0
        assert key_sync(winner).returncode == 0
        assert (a / note).read_text() == "Later encrypted edit\n"
        assert any(
            path.read_text(encoding="utf-8") == "Earlier accepted encrypted edit\n"
            for path in (b / ".local/sync-conflicts").rglob("*.md")
        )
        (a / note).unlink()
        assert key_sync(winner).returncode == 0 and key_sync(2).returncode == 0
        assert not (b / note).exists()
        for raw in server.requests:
            request = decode_request(raw)
            if request.operation == "commit":
                for mutation in request.body.mutations:
                    assert mutation.id == FORMAT_RESOURCE_ID or mutation.id.startswith(
                        "e1:"
                    )
                assert (
                    b"memory:notes/e2ee.md" not in raw
                    and b"Encrypted first device content" not in raw
                )
                assert code.encode() not in raw
        guarded = command(
            winner, ["remote", "sync", "--dry-run", "--workspace", str(a)]
        )
        assert (
            guarded.returncode != 0 and "encrypted_store_unsupported" in guarded.stderr
        )
    (base / "checked.txt").write_text(
        "Encrypted synchronization, independent initialization CAS and retries passed\n"
    )
    print(
        "[SUCCESS] Encrypted synchronization, independent initialization CAS and retries passed"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
