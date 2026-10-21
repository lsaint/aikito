"""End-to-end CLI smoke test for 'aikito remote bind' and 'aikito remote sync'."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from workspace_memory_remote import InMemoryRemote
from workspace_encrypted_setup import initialize_cli
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
    backend = InMemoryRemote("11223344556677889900aabbccddeeff")

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
        assert "[SUCCESS] Synchronization connected." in res.stdout
        assert "aikito remote key initialize" in res.stdout
        assert (ws_a / REMOTE_BINDING_STATE).exists(), (
            "Binding state file missing in ws_a"
        )

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
        assert (ws_b / REMOTE_BINDING_STATE).exists(), (
            "Binding state file missing in ws_b"
        )

        initialize_cli(run_cli, ws_a, home, env_auth)

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
        # Empty directories are not part of a skill, so they must not sync.
        smoke_skill = ws_a / "skills/smoke-skill"
        (smoke_skill / "empty").mkdir(parents=True)
        (smoke_skill / "SKILL.md").write_text("# Smoke skill\n", encoding="utf-8")

        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_a,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Upload note failed: {res.stderr}\n{res.stdout}"
        assert "Applied:   2" in res.stdout
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
        assert not (ws_b / "memory/notes/smoke-idea.md").exists(), (
            "Dry-run should not write file"
        )

        # 7. Workspace B: apply sync
        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Apply B failed: {res.stderr}\n{res.stdout}"
        assert "Remote sync result" in res.stdout
        assert (ws_b / "memory/notes/smoke-idea.md").exists(), (
            "File should exist after apply"
        )
        content = (ws_b / "memory/notes/smoke-idea.md").read_text(encoding="utf-8")
        assert content == "Durable smoke test idea\n", f"Unexpected content: {content}"
        assert (ws_b / "skills/smoke-skill/SKILL.md").is_file()
        assert not (ws_b / "skills/smoke-skill/empty").exists()

        # 7b. Adding an empty directory does not create a remote revision
        revision = backend.read().revision
        (ws_b / "skills/smoke-skill/other-empty").mkdir()
        res = run_cli(
            ["remote", "sync", "--apply"],
            workspace=ws_b,
            home=home,
            env_vars=env_auth,
        )
        assert res.returncode == 0, f"Resync B failed: {res.stderr}\n{res.stdout}"
        assert backend.read().revision == revision, "Empty directory was synced"

        # Replacing shared TOML fields must retain each original logical value.
        for content in (
            "[memory]\nstale_days = 14\nretention = 50\n",
            "[memory]\nstale_days = 30\nretention = 90\n",
        ):
            (ws_a / "config.toml").write_text(content, encoding="utf-8")
            res = run_cli(
                ["remote", "sync", "--apply"],
                workspace=ws_a,
                home=home,
                env_vars=env_auth,
            )
            assert res.returncode == 0, res.stderr
        records = [
            json.loads(path.read_text())
            for path in (ws_a / ".local/sync-conflicts").glob("**/resource.json")
        ]
        old_fields = {
            record["id"]: record["payload"]
            for record in records
            if record["id"].startswith("config:memory.")
        }
        assert old_fields["config:memory.stale_days"]["path"] == [
            "memory",
            "stale_days",
        ]
        assert old_fields["config:memory.stale_days"]["text"] == "value = 14\n"
        assert old_fields["config:memory.retention"]["text"] == "value = 50\n"
        (base / "archive-fields-checked.txt").write_text(
            "Shared fields remained recoverable\n", encoding="utf-8"
        )

        # 8. Workspace B: sync with invalid token fails with clear message
        res_bad_auth = run_cli(
            ["remote", "sync", "--dry-run"],
            workspace=ws_b,
            home=home,
            env_vars={TOKEN_ENV: "invalid-token"},
        )
        assert res_bad_auth.returncode == 1, "Expected sync with bad token to fail"
        assert "Remote authentication failed (HTTP 401)" in res_bad_auth.stderr

    (base / "encrypted-manifest.json").write_text(
        json.dumps(sorted(backend.read().resources))
    )
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
