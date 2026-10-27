"""Real CLI coverage: remote -> workspace -> approved runtime, on all platforms."""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_encrypted_setup import initialize_cli
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from workspace_remote_watch_smoke import run_cli

from aikito.bundled_skills import refresh_bundled_skills
from aikito.skill_state import get_binding_hash, get_skill_state_dir

TOKEN_ENV = "AIKITO_PROJECT_APPLY_SMOKE_TOKEN"
TOKEN = "project-apply-smoke-secret"


def run_smoke(base: Path) -> None:
    start = time.monotonic()
    home = base / "home"
    home.mkdir(parents=True)
    left = _workspace(base / "left")
    right = _workspace(base / "right")
    copy = base / "copy-checkout"
    link = base / "link-checkout"
    stranger = base / "unapproved-checkout"
    for checkout in (copy, link, stranger):
        checkout.mkdir()
    for ws in (left, right):
        (ws / "global/AGENTS.md").write_text("# Global\n", encoding="utf-8")
        for name, checkout, mode in (("demo", copy, "copy"), ("linked", link, "link")):
            definition = ws / "projects" / name
            (definition / "memory/notes").mkdir(parents=True, exist_ok=True)
            (definition / "AGENTS.md").write_text("# Project\n", encoding="utf-8")
            (definition / "agent.toml").write_text(
                f'name = "{name}"\npath = {json.dumps(str(checkout))}\nskills = ["example"]\nsync_mode = "{mode}"\n',
                encoding="utf-8",
            )
        skill = ws / "skills/example"
        skill.mkdir()
        (skill / "SKILL.md").write_text("v1", encoding="utf-8")
        refresh_bundled_skills(ws, home)
    auth = {TOKEN_ENV: TOKEN}

    def cli(ws: Path, *args: str, code: int = 0) -> str:
        result = run_cli(list(args), workspace=ws, home=home, env_vars=auth)
        assert result.returncode == code, (
            f"{args}: {result.returncode}\n{result.stdout}\n{result.stderr}"
        )
        return result.stdout

    with HTTPRemoteServer(
        InMemoryRemote("bb223344556677889900aabbccddeeff"), token=TOKEN
    ) as server:
        for ws in (left, right):
            cli(
                ws, "remote", "bind", "--endpoint", server.url, "--token-env", TOKEN_ENV
            )
        initialize_cli(run_cli, left, home, auth)
        cli(left, "remote", "sync", "--apply")
        cli(right, "remote", "sync", "--apply")
        assert not (copy / ".agents").exists()
        initial_status = run_cli(["status"], workspace=right, home=home, env_vars=auth)
        assert initial_status.returncode == 0, initial_status.stderr
        assert "[NOTICE] Automatic application" in initial_status.stderr
        assert "Run 'aikito sync' once" in initial_status.stderr
        assert "Automatic application" not in initial_status.stdout
        assert "! unbound" not in initial_status.stdout.lower()
        cli(right, "sync")
        target = copy / ".agents/skills/example/SKILL.md"
        link_target = link / ".agents/skills/example"
        assert target.read_text(encoding="utf-8") == "v1"
        assert link_target.is_symlink()

        # A foreign path received from the remote must not inherit local approval.
        config_left = left / "projects/demo/agent.toml"
        config_left.write_text(
            config_left.read_text().replace(
                f"path = {json.dumps(str(copy))}",
                f"paths = [{json.dumps(str(copy))}, {json.dumps(str(stranger))}]",
            ),
            encoding="utf-8",
        )
        (left / "skills/example/SKILL.md").write_text("v2", encoding="utf-8")
        cli(left, "remote", "sync", "--apply")
        cli(right, "remote", "watch", "--once")
        assert target.read_text(encoding="utf-8") == "v2"
        assert (link_target / "SKILL.md").read_text(encoding="utf-8") == "v2"
        assert not (stranger / ".agents").exists()
        status = run_cli(["status"], workspace=right, home=home, env_vars=auth)
        assert status.returncode == 0, status.stderr
        assert "[NOTICE] Automatic application" in status.stderr
        assert "Automatic application" not in status.stdout
        assert "! unbound" not in status.stdout.lower(), status.stdout
        detail = cli(right, "show", "project", "demo")
        assert "Sync trigger:" in detail and "UNAPPROVED" in detail
        summary = cli(right, "remote", "status")
        assert (
            "Checkouts:" in summary
            and "2 synced" in summary
            and "1 unapproved" in summary
        )
        assert "last results; service stopped" in summary
        (base / "checkout-status.txt").write_text(summary, encoding="utf-8")

        # Keep later status assertions focused on the approved checkout.
        config_right = right / "projects/demo/agent.toml"
        config_right.write_text(
            config_right.read_text().replace(
                f"paths = [{json.dumps(str(copy))}, {json.dumps(str(stranger))}]",
                f"path = {json.dumps(str(copy))}",
            ),
            encoding="utf-8",
        )
        # UPDATE must be visible before manual sync restores health.
        source = right / "skills/example/SKILL.md"
        source.write_text("v3", encoding="utf-8")
        assert "update" in cli(right, "status").lower()
        cli(right, "sync", "project", "demo", str(copy))
        assert target.read_text(encoding="utf-8") == "v3"
        source.write_text("v4", encoding="utf-8")
        cli(right, "remote", "sync", "--dry-run")
        assert target.read_text(encoding="utf-8") == "v3"
        cli(right, "remote", "sync", "--apply")
        assert target.read_text(encoding="utf-8") == "v4"

        # Preserve local edits and report project failure separately from remote success.
        target.write_text("local edit", encoding="utf-8")
        source.write_text("v5", encoding="utf-8")
        output = cli(right, "remote", "sync", "--apply", code=2)
        assert "REMOTE_SYNC_OK" in output and "PROJECT_APPLY_BLOCKED" in output
        assert target.read_text(encoding="utf-8") == "local edit"
        cli(right, "remote", "watch", "--once")
        summary = cli(right, "remote", "status")
        assert "1 blocked" in summary and "1 synced" in summary
        report = cli(right, "doctor", "--json", code=1)
        findings = [
            finding
            for section in json.loads(report)["sections"]
            for finding in section["findings"]
        ]
        assert any(
            "blocked" in finding.get("reason", "") and str(copy) in finding["reason"]
            for finding in findings
        )
        (base / "blocked-doctor.json").write_text(report, encoding="utf-8")
        # Repair only the checkout; a local watcher pass must apply the pending update.
        target.write_text("v4", encoding="utf-8")
        cli(right, "remote", "watch", "--once")
        assert target.read_text(encoding="utf-8") == "v5"
        (base / "recovered.txt").write_text(target.read_text(), encoding="utf-8")
        cli(right, "sync", "project", "demo", str(copy))
        source.write_text("v6", encoding="utf-8")
        config = right / "projects/demo/agent.toml"
        config.write_text(
            config.read_text() + 'sync_trigger = "manual"\n', encoding="utf-8"
        )
        cli(right, "remote", "sync", "--apply")
        assert target.read_text(encoding="utf-8") == "v5"
        config.write_text(
            config.read_text().replace(
                'sync_trigger = "manual"', 'sync_trigger = "typo"'
            ),
            encoding="utf-8",
        )
        assert "invalid config" in cli(right, "status").lower()
        config.write_text(
            config.read_text().replace(
                'sync_trigger = "typo"', 'sync_trigger = "auto"'
            ),
            encoding="utf-8",
        )

        # The same directory name now refers to a different physical checkout.
        copy.rename(base / "old-copy-checkout")
        copy.mkdir()
        cli(right, "remote", "watch", "--once")
        assert not (copy / ".agents").exists()
        cli(right, "sync", "project", "demo", str(copy))
        assert target.read_text(encoding="utf-8") == "v6"

        approval = (
            get_skill_state_dir(home)
            / f"{get_binding_hash(right, 'demo', copy)}.approval"
        )
        assert approval.exists()
        config.unlink()
        for fix in (False, True):
            args = ["doctor", "--json", *(["--fix"] if fix else [])]
            report = run_cli(args, workspace=right, home=home, env_vars=auth)
            assert report.returncode in (0, 1), report.stderr
            assert report.stdout.lstrip().startswith("{"), (
                report.stdout,
                report.stderr,
            )
            document = json.loads(report.stdout)
            if not fix:
                findings = [
                    finding
                    for section in document["sections"]
                    for finding in section["findings"]
                ]
                assert any(
                    finding.get("source") == str(approval)
                    and finding.get("code") == "local-state.stale"
                    for finding in findings
                )
                assert approval.exists()
            else:
                assert not approval.exists()
        assert target.read_text(encoding="utf-8") == "v6"
        (base / "approval-cleanup.json").write_text(
            json.dumps({"removed": str(approval)}), encoding="utf-8"
        )
    print(f"Project auto apply smoke passed in {time.monotonic() - start:.2f}s")


if __name__ == "__main__":
    run_smoke(Path(sys.argv[1]).resolve())
