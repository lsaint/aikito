"""Real HTTP, independent devices, CAS races and stable ciphertext retries."""

from __future__ import annotations

import json
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
    external = os.environ.get("AIKITO_ENCRYPTED_TEST_ENDPOINT")
    if external:
        from aikito.workspace.http_transport import HTTPTransport
        from aikito.workspace.serialized_remote import SerializedRemoteStore

        backend = SerializedRemoteStore(
            HTTPTransport(
                external, authorization="Bearer encrypted-sync-smoke-token"
            ).exchange
        )
    else:
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
            ["remote", "sync", mode, "--workspace", str(workspaces[index])],
        )

    for index in range(3):
        result = command(index, ["init", "workspace", str(workspaces[index])])
        assert result.returncode == 0, result.stderr
    with HTTPRemoteServer(backend, token="encrypted-sync-smoke-token") as server:
        for index in (0, 1):
            result = command(
                index,
                [
                    "remote",
                    "bind",
                    "--endpoint",
                    server.url,
                    "--workspace",
                    str(workspaces[index]),
                ],
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
                "bind",
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
        for relative, text in {
            "agents/custom.toml": '[agents.custom]\nvalue = "portable"\n',
            "mcps/docs.toml": 'agents = ["custom"]\ncommand = "docs"\n',
            "subagents/review.md": '---\ndescription: "Review"\nagents: ["custom"]\n---\nReview\n',
        }.items():
            (a / relative).write_text(text)
        note = Path("memory/notes/e2ee.md")
        (a / note).parent.mkdir(parents=True, exist_ok=True)
        (a / note).write_text('password = "test-only-credential-1234567890"\n')
        skill = Path("skills/join-example")
        (a / skill).mkdir(parents=True)
        (a / skill / "SKILL.md").write_text("# Current skill\n")
        preview_before = backend.read()
        preview = key_sync(winner, "--dry-run")
        assert preview.returncode == 0, preview.stderr
        assert "Remote sync plan" in preview.stdout
        assert "Reading and decrypting" not in preview.stdout
        assert "\033[" not in preview.stdout
        (base / "progress-checked.txt").write_text(
            "Redirected sync preview contains only the final plan\n"
        )
        assert preview.returncode == 0 and backend.read() == preview_before
        assert not (a / ".local/state/aikito/encrypted-discovery.json").exists()
        first = key_sync(winner)
        assert first.returncode == 0, first.stderr
        # A stale Git checkout must not upload old files on its first connection.
        stale = Path("memory/notes/join-stale.md")
        (b / stale).write_text("Retired local note\n")
        (b / note).write_text("Stale local version\n")
        (b / skill / "agents").mkdir(parents=True)
        (b / skill / "SKILL.md").write_text("# Current skill\n")
        (b / skill / "agents/openai.yaml").write_text("Retired skill file\n")
        before_join = backend.read()
        join_preview = key_sync(2, "--dry-run")
        assert join_preview.returncode == 0, join_preview.stderr
        assert "[LOCAL BACKUP]" in join_preview.stdout
        assert not (b / ".local/sync-join").exists()
        assert (b / stale).read_text() == "Retired local note\n"
        (base / "join-preview.out").write_text(join_preview.stdout)
        second = key_sync(2)
        assert second.returncode == 0, second.stderr
        assert "Local differences saved" in second.stdout
        (base / "join-apply.out").write_text(second.stdout)
        assert backend.read() == before_join
        assert not (b / stale).exists()
        assert not (b / skill / "agents/openai.yaml").exists()
        backups = b / ".local/sync-join"
        saved = list(backups.rglob("join-stale.md"))
        assert len(saved) == 1 and saved[0].read_text() == "Retired local note\n"
        index_path = next(backups.glob("*/index.json"))
        index = json.loads(index_path.read_text())
        entry = index["resources"]["global-memory:notes/join-stale.md"]
        assert index_path.parent / entry["path"] == saved[0]
        assert str(index_path.parent) in second.stdout
        assert str(index_path) in second.stdout
        assert str(saved[0]) in second.stdout
        (base / "join-paths-checked.txt").write_text(
            "Sync output includes existing absolute archive, index and resource paths\n"
        )
        assert "T" in index_path.parent.name and "Z-" in index_path.parent.name
        assert not list(backups.rglob("resource.json"))
        (base / "join-index.json").write_bytes(index_path.read_bytes())
        assert any(
            p.read_text() == "Retired skill file\n"
            for p in backups.rglob("openai.yaml")
        )
        (base / "join-backup.md").write_bytes(saved[0].read_bytes())
        # Copying a reviewed backup back is an ordinary local creation.
        (b / "memory/notes/after-join.md").write_bytes(saved[0].read_bytes())
        assert key_sync(2).returncode == 0 and key_sync(winner).returncode == 0
        assert (a / "memory/notes/after-join.md").read_text() == "Retired local note\n"
        (base / "join-checked.txt").write_text(
            "Safe first connection and two-way sync passed\n"
        )
        assert (
            b / note
        ).read_text() == 'password = "test-only-credential-1234567890"\n'
        (base / "credential-checked.txt").write_text(
            "Credential content synchronized only as ciphertext\n"
        )
        assert (b / "agents/custom.toml").is_file()
        assert (
            b / "mcps/docs.toml"
        ).read_text() == 'agents = ["custom"]\ncommand = "docs"\n'
        assert 'agents: ["custom"]' in (b / "subagents/review.md").read_text()
        revision_with_references = backend.read().revision
        assert key_sync(2).returncode == 0
        assert backend.read().revision == revision_with_references
        (base / "references-checked.txt").write_text(
            "Dynamic Agent references survived independent sync processes\n"
        )
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
                    b"global-memory:notes/e2ee.md" not in raw
                    and b'password = "test-only-credential-1234567890"' not in raw
                )
                assert code.encode() not in raw
        guarded = command(
            winner, ["remote", "sync", "--dry-run", "--workspace", str(a)]
        )
        assert guarded.returncode == 0
    (base / "checked.txt").write_text(
        "Encrypted synchronization, independent initialization CAS and retries passed\n"
    )
    print(
        "[SUCCESS] Encrypted synchronization, independent initialization CAS and retries passed"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
