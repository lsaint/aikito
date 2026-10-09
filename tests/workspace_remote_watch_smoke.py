"""End-to-end CLI smoke test for 'aikito remote watch'."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from aikito.workspace.remote import FilesystemRemote

TOKEN = "remote-watch-smoke-secret"
TOKEN_ENV = "AIKITO_REMOTE_WATCH_SMOKE_TOKEN"


def run_cli(
    args: list[str],
    *,
    workspace: Path,
    home: Path,
    env_vars: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["AIKITO_DIR"] = str(workspace)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if env_vars:
        env.update(env_vars)

    return subprocess.run(
        [sys.executable, "-m", "aikito", *args],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(workspace),
    )


def spawn_cli(
    args: list[str],
    *,
    workspace: Path,
    home: Path,
    env_vars: dict[str, str] | None = None,
) -> subprocess.Popen[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["AIKITO_DIR"] = str(workspace)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if env_vars:
        env.update(env_vars)

    return subprocess.Popen(
        [sys.executable, "-m", "aikito", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
        cwd=str(workspace),
    )


def run_smoke(base: Path) -> None:
    ws_a = _workspace(base / "ws_a")
    ws_b = _workspace(base / "ws_b")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    backend = FilesystemRemote.create(base / "center")

    env_auth = {TOKEN_ENV: TOKEN}

    with HTTPRemoteServer(backend, token=TOKEN) as server:
        # 1. Bind Workspace A and B
        res_a = run_cli(
            ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res_a.returncode == 0, f"Bind A failed: {res_a.stderr}"

        res_b = run_cli(
            ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res_b.returncode == 0, f"Bind B failed: {res_b.stderr}"

        # 2. Workspace A initial apply
        res_init = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res_init.returncode == 0, f"Init apply failed: {res_init.stderr}"

        # 3. Workspace B apply
        res_init_b = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res_init_b.returncode == 0, f"Init apply B failed: {res_init_b.stderr}"

        # 4. Start continuous watch on Workspace B
        watch_proc = spawn_cli(
            [
                "remote",
                "watch",
                "--poll-interval",
                "0.5",
                "--debounce-ms",
                "100",
                "--verbose",
            ],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )

        try:
            # Allow watch process to start and acquire lock
            time.sleep(0.6)

            # 5. Second watch on Workspace B must be rejected immediately
            res_second_watch = run_cli(
                ["remote", "watch", "--once"],
                workspace=ws_b,
                home=home,
                env_vars=env_auth,
            )
            assert res_second_watch.returncode == 1, (
                "Expected second watch to fail with exit code 1"
            )
            assert (
                "Another auto-sync worker is already running" in res_second_watch.stderr
            )

            # 6. Workspace A creates a note and applies
            smoke_note = ws_a / "memory/notes/watch-smoke.md"
            smoke_note.write_text(
                "Continuous watch propagation test\n", encoding="utf-8"
            )

            res_upload = run_cli(
                ["remote", "sync", "--apply"],
                workspace=ws_a,
                home=home,
                env_vars=env_auth,
            )
            assert res_upload.returncode == 0, (
                f"Upload note failed: {res_upload.stderr}"
            )

            # 7. Workspace B watch process should automatically pull within a few seconds
            target_on_b = ws_b / "memory/notes/watch-smoke.md"
            deadline = time.time() + 6.0
            synced = False
            while time.time() < deadline:
                if target_on_b.exists():
                    synced = True
                    break
                time.sleep(0.2)

            assert synced, (
                "Watch process on WS B did not synchronize watch-smoke.md within deadline"
            )
            content_b = target_on_b.read_text(encoding="utf-8")
            assert content_b == "Continuous watch propagation test\n", (
                f"Unexpected content: {content_b}"
            )

            # 8. Workspace B creates a note; watch process detects and uploads
            b_note = ws_b / "memory/notes/from-b.md"
            b_note.write_text("Created on B\n", encoding="utf-8")

            # Wait for watcher debounce and upload
            time.sleep(1.5)

            # 9. Workspace A runs watch --once to verify it receives B's note
            res_pull_a = run_cli(
                ["remote", "watch", "--once"],
                workspace=ws_a,
                home=home,
                env_vars=env_auth,
            )
            assert res_pull_a.returncode == 0, (
                f"Pull on A failed: {res_pull_a.stderr}\n{res_pull_a.stdout}"
            )
            target_on_a = ws_a / "memory/notes/from-b.md"
            assert target_on_a.exists(), "Note from B was not received on A"
            assert target_on_a.read_text(encoding="utf-8") == "Created on B\n"

        finally:
            # Terminate background watch process
            watch_proc.terminate()
            try:
                watch_proc.wait(timeout=3.0)
            except subprocess.TimeoutExpired:
                watch_proc.kill()
                watch_proc.wait()

    print("[SUCCESS] Remote watch smoke test passed successfully")


def main(base: Path | None = None) -> None:
    if base is None:
        target = Path(sys.argv[1]) if len(sys.argv) > 1 else None
    else:
        target = base

    if target is None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            run_smoke(Path(tmp_dir))
    else:
        target.mkdir(parents=True, exist_ok=True)
        run_smoke(target)


if __name__ == "__main__":
    main()
