"""End-to-end CLI smoke test for 'aikito remote bind' and 'aikito remote sync'."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.resource_state import REMOTE_BINDING_STATE

TOKEN = "remote-cli-smoke-secret"
TOKEN_ENV = "AIKITO_REMOTE_CLI_SMOKE_TOKEN"


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


def run_smoke(base: Path) -> None:
    ws_a = _workspace(base / "ws_a")
    ws_b = _workspace(base / "ws_b")
    home = base / "home"
    home.mkdir(parents=True, exist_ok=True)
    backend = FilesystemRemote.create(base / "center")

    env_auth = {TOKEN_ENV: TOKEN}

    with HTTPRemoteServer(backend, token=TOKEN) as server:
        # 1. Bind Workspace A
        res = run_cli(
            ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Bind A failed: {res.stderr}\n{res.stdout}"
        assert "[SUCCESS] Bound workspace to remote store" in res.stdout
        assert "Run 'aikito remote sync --dry-run' to preview" in res.stdout
        assert (ws_a / REMOTE_BINDING_STATE).exists(), "Binding state file missing in ws_a"

        # 2. Re-bind Workspace A should be rejected
        res_rebind = run_cli(
            ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res_rebind.returncode == 1, "Expected re-binding to fail"
        assert "Remote binding already exists" in res_rebind.stderr

        # 3. Bind Workspace B
        res = run_cli(
            ["remote", "bind", server.url, "--token-env", TOKEN_ENV],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Bind B failed: {res.stderr}\n{res.stdout}"
        assert (ws_b / REMOTE_BINDING_STATE).exists(), "Binding state file missing in ws_b"

        # 4. Workspace A: initial apply to push default resources
        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Apply A failed: {res.stderr}\n{res.stdout}"
        assert "Remote sync result" in res.stdout
        assert "Remaining:" not in res.stdout

        # 5. Workspace A: create a new note
        smoke_note = ws_a / "memory/notes/smoke-idea.md"
        smoke_note.write_text("Durable smoke test idea\n", encoding="utf-8")

        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Upload note failed: {res.stderr}\n{res.stdout}"
        assert "Applied:   1" in res.stdout
        assert "Remaining:" not in res.stdout

        # 6. Workspace B: preview sync with dry-run
        res = run_cli(
            ["remote", "sync", "--dry-run"],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Dry-run B failed: {res.stderr}\n{res.stdout}"
        assert "Remote sync plan" in res.stdout
        assert not (ws_b / "memory/notes/smoke-idea.md").exists(), "Dry-run should not write file"

        # 7. Workspace B: apply sync
        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Apply B failed: {res.stderr}\n{res.stdout}"
        assert "Remote sync result" in res.stdout
        assert (ws_b / "memory/notes/smoke-idea.md").exists(), "File should exist after apply"
        content = (ws_b / "memory/notes/smoke-idea.md").read_text(encoding="utf-8")
        assert content == "Durable smoke test idea\n", f"Unexpected content: {content}"

        # 8. Workspace B: sync with invalid token fails with clear message
        res_bad_auth = run_cli(
            ["remote", "sync", "--dry-run"],
            workspace=ws_b,
            home=home,
            env_vars={TOKEN_ENV: "invalid-token"},
        )
        assert res_bad_auth.returncode == 1, "Expected sync with bad token to fail"
        assert "Remote authentication failed (HTTP 401)" in res_bad_auth.stderr

    print("[SUCCESS] Remote CLI smoke test passed successfully")


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
