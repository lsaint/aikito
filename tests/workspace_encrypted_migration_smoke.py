"""Independent CLI owners, loopback socket loss, fencing and exact rollback."""

import os
import subprocess
import sys
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.encrypted_migration import MIGRATION_STATE
from aikito.workspace.encrypted_remote import decode_recovery_code
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_protocol import decode_request

TOKEN = "migration-smoke-only-token"


def main(base):
    base.mkdir(parents=True)
    backend = InMemoryRemote("11223344556677889900aabbccddeeff")
    workspaces, environments = [], []
    for index in range(2):
        home, workspace, config = (
            base / f"{name}-{index}" for name in ("home", "workspace", "config")
        )
        home.mkdir()
        workspaces.append(workspace)
        environments.append(
            {
                **os.environ,
                "HOME": str(home),
                "USERPROFILE": str(home),
                "XDG_CONFIG_HOME": str(config),
                "APPDATA": str(config),
                "AIKITO_DIR": str(workspace),
                "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
                "AIKITO_REMOTE_TOKEN": TOKEN,
            }
        )

    def command(index, args, *, test_driver=False, input=None):
        executable = (
            [
                sys.executable,
                str(Path(__file__).with_name("workspace_migration_cli.py")),
            ]
            if test_driver
            else [sys.executable, "-m", "aikito"]
        )
        return subprocess.run(
            [*executable, *args],
            env=environments[index],
            input=input,
            text=True,
            capture_output=True,
            timeout=60,
        )

    def migrate(index, *args, driver=True):
        return command(
            index,
            ["remote", "key", "migrate", "--workspace", str(workspaces[index]), *args],
            test_driver=driver,
        )

    def baseline(index):
        root = workspaces[index]
        (root / ".gitignore").write_text(".local/\n")
        for args in (
            ("init",),
            ("config", "user.name", "Migration Smoke"),
            ("config", "user.email", "smoke@example.invalid"),
            ("add", "."),
            ("commit", "-m", "test: capture migration baseline"),
        ):
            subprocess.run(
                ["git", "-C", str(root), *args],
                capture_output=True,
                check=True,
                env=environments[index],
            )

    for index in range(2):
        result = command(index, ["init", "workspace", str(workspaces[index])])
        assert result.returncode == 0, result.stderr
    note = workspaces[0] / "memory/notes/migrate.md"
    note.write_text("private migration smoke payload\n")
    with HTTPRemoteServer(backend, token=TOKEN) as server:
        result = command(
            0, ["remote", "bind", server.url, "--workspace", str(workspaces[0])]
        )
        assert result.returncode == 0, result.stderr
        result = command(
            0, ["remote", "sync", "--apply", "--workspace", str(workspaces[0])]
        )
        assert result.returncode == 0, result.stderr
        baseline(0)
        original = backend.read()
        payloads = backend.fetch(original, original.resources)
        requests = server.request_count
        gated = migrate(0, "--apply", driver=False)
        assert (
            gated.returncode == 1
            and "encrypted_migration_release_required" in gated.stderr
        )
        assert server.request_count == requests
        preview = migrate(0, "--dry-run", driver=False)
        assert preview.returncode == 0, preview.stderr
        assert (
            backend.read() == original
            and not (workspaces[0] / MIGRATION_STATE).exists()
        )
        started = migrate(
            0, "--apply", "--max-rounds", "1", "--recovery-output-fd", "1"
        )
        assert started.returncode == 2, started.stderr
        code = started.stdout.strip()
        decode_recovery_code(code)
        assert FORMAT_RESOURCE_ID in backend.read().resources
        guarded = command(
            0, ["remote", "sync", "--apply", "--workspace", str(workspaces[0])]
        )
        assert guarded.returncode == 1
        for fault in ("drop_after_request", "drop_after_handler"):
            server.arm(fault, operation="commit")
            failed = migrate(0, "--apply", "--max-rounds", "1")
            assert failed.returncode == 1
            journal = (workspaces[0] / MIGRATION_STATE).read_bytes()
            assert (
                b"private migration smoke payload" not in journal
                and code.encode() not in journal
            )
            commits = [
                raw
                for raw in server.requests
                if decode_request(raw).operation == "commit"
            ]
            uncertain = commits[-1]
            resumed = migrate(0, "--apply", "--max-rounds", "1")
            assert resumed.returncode in (0, 2), resumed.stderr
            commits = [
                raw
                for raw in server.requests
                if decode_request(raw).operation == "commit"
            ]
            assert commits.count(uncertain) == (
                2 if fault == "drop_after_request" else 1
            )
        joined = command(
            1,
            [
                "remote",
                "key",
                "recover",
                "--endpoint",
                server.url,
                "--workspace",
                str(workspaces[1]),
                "--recovery-fd",
                "0",
            ],
            input=code + "\n",
        )
        assert joined.returncode == 0, joined.stderr
        baseline(1)
        claimed = migrate(1, "--apply", "--takeover", "--max-rounds", "1")
        assert claimed.returncode == 2 and "epoch=2" in claimed.stderr, claimed.stderr
        before = backend.read()
        fenced = migrate(0, "--apply")
        assert fenced.returncode == 1 and "encrypted_migration_fenced" in fenced.stderr
        assert backend.read() == before
        complete = migrate(1, "--apply")
        assert complete.returncode == 0, complete.stderr
        assert all(
            k == FORMAT_RESOURCE_ID or k.startswith("e1:")
            for k in backend.read().resources
        )
        assert not (workspaces[1] / MIGRATION_STATE).exists()
        rollback = migrate(1, "--apply", "--rollback")
        assert rollback.returncode == 0, rollback.stderr
        restored = backend.read()
        assert dict(restored.resources) == dict(original.resources)
        assert backend.fetch(restored, restored.resources) == payloads
        assert note.read_text() == "private migration smoke payload\n"
        assert not (workspaces[1] / MIGRATION_STATE).exists()
    (base / "checked.txt").write_text(
        "Migration, takeover, exact retries and rollback verified\n"
    )
    print("[SUCCESS] Migration, takeover, exact retries and rollback passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
