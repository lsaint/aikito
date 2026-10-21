"""End-to-end CLI smoke test for 'aikito remote start / stop / status'."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from workspace_memory_remote import InMemoryRemote
from workspace_encrypted_setup import initialize_cli

TOKEN = "remote-service-smoke-secret"
TOKEN_ENV = "AIKITO_REMOTE_SERVICE_SMOKE_TOKEN"


def _make_smoke_bin(base: Path) -> Path:
    bin_dir = base / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    src_dir = Path(__file__).resolve().parents[1] / "src"
    if sys.platform == "win32":
        bat_file = bin_dir / "aikito.cmd"
        bat_file.write_text(
            f'@echo off\nset "PYTHONPATH={src_dir}"\n"{sys.executable}" -m aikito %*\n',
            encoding="utf-8",
        )
    else:
        sh_file = bin_dir / "aikito"
        sh_file.write_text(
            f'#!/bin/sh\nexport PYTHONPATH="{src_dir}"\nexec "{sys.executable}" -m aikito "$@"\n',
            encoding="utf-8",
        )
        sh_file.chmod(0o755)
    return bin_dir


def run_cli(
    args: list[str],
    *,
    workspace: Path,
    home: Path,
    bin_dir: Path | None = None,
    env_vars: dict[str, str] | None = None,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["AIKITO_DIR"] = str(workspace)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if bin_dir is None and (home.parent / "bin").is_dir():
        bin_dir = home.parent / "bin"
    if bin_dir:
        env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    if env_vars:
        env.update(env_vars)

    return subprocess.run(
        [sys.executable, "-m", "aikito", *args],
        capture_output=True,
        text=True,
        input=input_text,
        env=env,
        cwd=str(workspace),
    )


def run_smoke(base: Path) -> None:
    _make_smoke_bin(base)
    ws_a = _workspace(base / "ws_a")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    backend = InMemoryRemote("11223344556677889900aabbccddeeff")

    with HTTPRemoteServer(backend, token=TOKEN) as server:
        # 1. Secure credential storage test via CLI
        res_cred_path = run_cli(
            ["remote", "credential", "path"],
            workspace=ws_a,
            home=home,
        )
        assert res_cred_path.returncode == 0
        cred_file = Path(res_cred_path.stdout.strip())
        assert not str(cred_file).startswith(str(ws_a)), (
            "Credentials must reside outside workspace"
        )

        res_cred_set = run_cli(
            ["remote", "credential", "set", TOKEN_ENV, "--stdin"],
            workspace=ws_a,
            home=home,
            input_text=TOKEN,
        )
        assert res_cred_set.returncode == 0, (
            f"Credential set failed: {res_cred_set.stderr}"
        )
        assert cred_file.exists(), "Credentials file was not created"

        # Verify credentials list does not expose token
        res_cred_list = run_cli(
            ["remote", "credential", "list"],
            workspace=ws_a,
            home=home,
        )
        assert res_cred_list.returncode == 0
        assert TOKEN_ENV in res_cred_list.stdout
        assert TOKEN not in res_cred_list.stdout

        # 2. Bind workspace without ambient env var (using stored credential)
        res_bind = run_cli(
            ["remote", "bind", "--endpoint", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_a,
            home=home,
        )
        assert res_bind.returncode == 0, f"Bind failed: {res_bind.stderr}"

        initialize_cli(run_cli, ws_a, home)

        # 3. Check status before service start
        res_stat_init = run_cli(
            ["remote", "status", "--json", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_stat_init.returncode == 0
        stat_init = json.loads(res_stat_init.stdout)
        assert stat_init["service"]["running"] is False
        assert stat_init["binding"]["endpoint"] == server.url
        assert stat_init["credential_source"] == "credentials_file"

        # 4. Start background service in process mode
        res_start = run_cli(
            ["remote", "start", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_start.returncode == 0, f"Start failed: {res_start.stderr}"
        assert "[SUCCESS] Started background auto-sync service" in res_start.stdout

        # Wait until the background service acquires lock and records status with PID.
        # Fixed sleeps race interpreter and process startup on slower CI runners.
        deadline = time.time() + 6.0
        while time.time() < deadline:
            res_stat_json = run_cli(
                ["remote", "status", "--json", "--mode", "process"],
                workspace=ws_a,
                home=home,
            )
            if res_stat_json.returncode == 0:
                stat_data = json.loads(res_stat_json.stdout)
                if (
                    stat_data["service"]["running"]
                    and stat_data["service"]["pid"] is not None
                ):
                    break
            time.sleep(0.1)
        else:
            assert res_stat_json.returncode == 0, (
                f"JSON status check failed: {res_stat_json.stderr}"
            )
            assert stat_data["service"]["running"] is True
            assert stat_data["service"]["pid"] is not None

        # 5. Check text status after start (running)
        res_stat_running = run_cli(
            ["remote", "status", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_stat_running.returncode == 0
        assert f"(PID: {stat_data['service']['pid']})" in res_stat_running.stdout
        assert "Service:         stopped" not in res_stat_running.stdout
        assert "Log file:" in res_stat_running.stdout
        assert "Recent activity:" in res_stat_running.stdout

        # 6. Start service again (idempotent test)
        res_start_again = run_cli(
            ["remote", "start", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_start_again.returncode == 0
        assert "[INFO] Auto-sync service is already running" in res_start_again.stdout

        # 7. Make a local edit and verify background auto-sync pushes change
        note_file = ws_a / "memory/notes/smoke-idea.md"
        note_file.write_text("Created during service smoke test\n", encoding="utf-8")

        # Wait up to 15 seconds for background worker to sync
        synced = False
        deadline = time.monotonic() + 15.0
        while time.monotonic() < deadline:
            res_chk = run_cli(
                ["remote", "status", "--json", "--mode", "process"],
                workspace=ws_a,
                home=home,
            )
            if res_chk.returncode == 0:
                data = json.loads(res_chk.stdout)
                if (data.get("last_revision") or 0) > 0:
                    synced = True
                    break
            time.sleep(0.5)

        assert synced, (
            "Background service failed to sync local modification within timeout"
        )

        # Verify text status includes Log file and Recent activity
        res_stat_detail = run_cli(
            ["remote", "status", "--mode", "process", "--tail", "5"],
            workspace=ws_a,
            home=home,
        )
        assert res_stat_detail.returncode == 0
        assert "Log file:" in res_stat_detail.stdout
        assert "Recent activity:" in res_stat_detail.stdout

        # 8. Restart background service while running
        res_restart = run_cli(
            ["remote", "restart", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_restart.returncode == 0, f"Restart failed: {res_restart.stderr}"
        assert "[SUCCESS] Restarted background auto-sync service" in res_restart.stdout

        # Verify service is still running after restart
        res_stat_restarted = run_cli(
            ["remote", "status", "--json", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_stat_restarted.returncode == 0
        stat_restarted = json.loads(res_stat_restarted.stdout)
        assert stat_restarted["service"]["running"] is True

        # 9. Stop background service
        res_stop = run_cli(
            ["remote", "stop", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_stop.returncode == 0, f"Stop failed: {res_stop.stderr}"
        assert "[SUCCESS] Stopped auto-sync service" in res_stop.stdout

        # 9. Verify status is stopped
        deadline = time.time() + 6.0
        while time.time() < deadline:
            res_stat_stopped = run_cli(
                ["remote", "status", "--json", "--mode", "process"],
                workspace=ws_a,
                home=home,
            )
            if res_stat_stopped.returncode == 0:
                stat_stopped = json.loads(res_stat_stopped.stdout)
                if not stat_stopped["service"]["running"]:
                    break
            time.sleep(0.1)
        assert res_stat_stopped.returncode == 0
        assert stat_stopped["service"]["running"] is False

        # 10. Stop again when not running
        res_stop_again = run_cli(
            ["remote", "stop", "--mode", "process"],
            workspace=ws_a,
            home=home,
        )
        assert res_stop_again.returncode == 0
        assert "[INFO] Auto-sync service is not running" in res_stop_again.stdout

    (base / "encrypted-manifest.json").write_text(
        json.dumps(sorted(backend.read().resources))
    )
    print("[SUCCESS] Remote service smoke test passed successfully")


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
