"""End-to-end CLI workflow test.

Migrated from inline CI shell assertions into a reproducible, cross-platform
pytest test suite.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.smoke]


class TestCliWorkflowE2E:
    @pytest.fixture(autouse=True)
    def setup_env(self, tmp_path: Path) -> None:
        self.home = tmp_path / "home"
        self.workspace = tmp_path / "aikito-ws"
        self.project_dir = tmp_path / "example-project"

        self.home.mkdir(parents=True)
        self.project_dir.mkdir(parents=True)

        # Mock installed agent directories
        for marker in (
            ".codex",
            ".claude",
            ".copilot/agents",
            ".dsh",
            ".grok/rules",
            ".gemini/antigravity-cli",
            ".config/opencode",
            ".pi/agent",
        ):
            (self.home / marker).mkdir(parents=True)

        self.env = dict(
            os.environ,
            HOME=str(self.home),
            USERPROFILE=str(self.home),
            AIKITO_DIR=str(self.workspace),
            AIKITO_NO_UPDATE_NOTIFIER="1",
        )

    def run_cli(
        self, *args: str, check: bool = True, cwd: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        cmd = [sys.executable, "-m", "aikito", *args]
        res = subprocess.run(cmd, cwd=cwd, env=self.env, capture_output=True, text=True)
        if check:
            assert res.returncode == 0, (
                f"CLI command failed ({res.returncode}): {' '.join(args)}\n"
                f"STDOUT:\n{res.stdout}\n"
                f"STDERR:\n{res.stderr}"
            )
        return res

    def test_complete_cli_lifecycle(self) -> None:
        # 1. Init workspace
        init_res = self.run_cli("init", "workspace", str(self.workspace))
        assert (
            "Run 'aikito sync'" in init_res.stdout or "[CONNECTED]" in init_res.stdout
        )

        for expected in ("config.toml", "layout.toml", ".gitattributes", "skills.toml"):
            assert (self.workspace / expected).is_file(), f"Missing {expected}"

        for expected_dir in ("agents", "subagents", "skills", "mcps", "projects"):
            assert (self.workspace / expected_dir).is_dir(), (
                f"Missing dir {expected_dir}"
            )

        assert (self.workspace / "skills/aikito/SKILL.md").is_file()
        assert (self.workspace / "skills/durable-memory/SKILL.md").is_file()

        # Connect idempotency
        connect_res = self.run_cli("init", "workspace", str(self.workspace))
        assert "[CONNECTED]" in connect_res.stdout

        # 2. Global Sync
        sync_res = self.run_cli("sync", "global")
        assert sync_res.returncode == 0

        # Verify symlinks created in home
        assert (self.home / ".agents/skills/aikito").exists()
        assert (self.home / ".agents/skills/durable-memory").exists()

        # 3. Project Initialization
        self.run_cli("init", "project", "example", str(self.project_dir))
        assert (self.workspace / "projects/example/agent.toml").is_file()
        assert (self.workspace / "projects/example/AGENTS.md").is_file()

        # 4. Resource Add (Skill, Subagent, MCP)
        self.run_cli("add", "skill", "ci-skill", "--description", "CI testing skill")
        assert (self.workspace / "skills/ci-skill/SKILL.md").is_file()

        self.run_cli(
            "add", "subagent", "ci-reviewer", "--description", "CI code reviewer"
        )
        assert (self.workspace / "subagents/ci-reviewer.md").is_file()

        self.run_cli(
            "add",
            "mcp",
            "ci-tools",
            "--url",
            "https://example.com/mcp",
            "--agents",
            "codex,claude-code",
        )
        assert (self.workspace / "mcps/ci-tools.toml").is_file()

        # 5. Sync resources
        all_sync = self.run_cli("sync")
        assert all_sync.returncode == 0

        # 6. Status and Inspection
        status_res = self.run_cli("status")
        assert "Skills" in status_res.stdout
        assert "MCP" in status_res.stdout
        assert "Sub" in status_res.stdout

        # 7. Doctor & Git Conflict Marker Detection
        doctor_clean = self.run_cli("doctor")
        assert doctor_clean.returncode == 0

        # Test conflict marker protection
        conflict_note = self.workspace / "memory/notes/conflict-test.md"
        conflict_note.parent.mkdir(parents=True, exist_ok=True)
        conflict_note.write_text(
            "# Conflicted Note\n<<<<<<< HEAD\nversion A\n=======\nversion B\n>>>>>>> branch\n",
            encoding="utf-8",
        )

        doctor_conflict = self.run_cli("doctor", "--no-color", check=False)
        assert (
            "Git conflict marker detected" in doctor_conflict.stdout
            or "Git conflict marker detected" in doctor_conflict.stderr
        )

        # Doctor --fix must refuse to alter conflicted content
        before_content = conflict_note.read_text(encoding="utf-8")
        self.run_cli("doctor", "--fix", "--no-color", check=False)
        assert conflict_note.read_text(encoding="utf-8") == before_content

        # Clean up conflict note
        conflict_note.unlink()

        # 8. Inbox Inspection
        inbox_dir = self.workspace / "inbox"
        inbox_dir.mkdir(parents=True, exist_ok=True)
        inbox_show = self.run_cli("show", "inbox")
        assert inbox_show.returncode == 0

        test_note = inbox_dir / "sample-note.md"
        test_note.write_text("# Sample Inbox Note\nContent\n", encoding="utf-8")

        inbox_list = self.run_cli("show", "inbox")
        assert "sample-note" in inbox_list.stdout

        inbox_view = self.run_cli("show", "inbox", "sample-note")
        assert "Sample Inbox Note" in inbox_view.stdout

        test_note.unlink()

    def test_sync_conflict_blocking(self) -> None:
        self.run_cli("init", "workspace", str(self.workspace))
        self.run_cli("init", "project", "example", str(self.project_dir))

        example_agents = self.workspace / "projects/example/AGENTS.md"
        example_agents.write_text(
            "# Conflicted instructions\n<<<<<<< HEAD\nrule A\n=======\nrule B\n>>>>>>> branch\n",
            encoding="utf-8",
        )

        # sync project must fail and report conflict
        res_fail = self.run_cli("sync", "project", "example", check=False)
        assert res_fail.returncode != 0
        assert (
            "Git conflict marker detected" in res_fail.stderr
            or "Git conflict marker detected" in res_fail.stdout
        )

        # --force must not bypass conflict
        res_force = self.run_cli("sync", "project", "example", "--force", check=False)
        assert res_force.returncode != 0
        assert (
            "Git conflict marker detected" in res_force.stderr
            or "Git conflict marker detected" in res_force.stdout
        )

        # Unrelated global sync must succeed
        res_global = self.run_cli("sync", "global")
        assert res_global.returncode == 0

    def test_copy_mode_drift_and_force(self) -> None:
        self.run_cli("init", "workspace", str(self.workspace))
        self.run_cli("init", "project", "example", str(self.project_dir))
        self.run_cli(
            "add",
            "skill",
            "copy-skill",
            "--description",
            "Copy test skill",
            "--project",
            "example",
        )

        # Switch to copy mode
        agent_toml = self.workspace / "projects/example/agent.toml"
        agent_toml.write_text(
            agent_toml.read_text(encoding="utf-8").replace(
                'sync_mode = "link"', 'sync_mode = "copy"'
            ),
            encoding="utf-8",
        )

        # Dry run preview
        res_dry = self.run_cli("sync", "project", "example", "--dry-run")
        assert "[DRY RUN COPY]" in res_dry.stdout

        # Real sync
        self.run_cli("sync", "project", "example")
        runtime_skill = self.project_dir / ".agents/skills/copy-skill"
        assert runtime_skill.is_dir()
        assert not runtime_skill.is_symlink()
        skill_file = runtime_skill / "SKILL.md"
        assert skill_file.is_file()

        # Idempotent repeated sync
        res_repeat = self.run_cli("sync", "project", "example")
        assert res_repeat.returncode == 0

        # Local drift
        skill_file.write_text("local drifted content\n", encoding="utf-8")
        res_drift = self.run_cli("sync", "project", "example", check=False)
        assert res_drift.returncode != 0
        assert "drifted" in res_drift.stderr or "drifted" in res_drift.stdout

        # Overwrite drift with --force
        res_force = self.run_cli("sync", "project", "example", "--force")
        assert res_force.returncode == 0
        assert skill_file.read_text(encoding="utf-8") != "local drifted content\n"

        # Deselection preservation
        self.run_cli("rm", "skill", "copy-skill", "--project", "example")
        self.run_cli("sync", "project", "example")
        assert runtime_skill.is_dir()
        assert skill_file.is_file()

    def test_cwd_centric_interaction(self) -> None:
        self.run_cli("init", "workspace", str(self.workspace))
        self.run_cli("init", "project", "example", str(self.project_dir))

        # 1. Inside project directory: sync project auto-detects from CWD
        res_sync = self.run_cli("sync", "project", cwd=self.project_dir)
        assert (
            "[aikito] Target project: 'example' (detected from cwd)" in res_sync.stdout
        )

        # 2. Inside project directory: show project auto-detects from CWD
        res_show = self.run_cli("show", "project", cwd=self.project_dir)
        assert (
            "[aikito] Target project: 'example' (detected from cwd)" in res_show.stdout
        )

        # 3. Outside project directory: sync project without name fails
        res_outside = self.run_cli("sync", "project", cwd=self.home, check=False)
        assert res_outside.returncode != 0
        assert (
            "Current directory is not inside a registered project" in res_outside.stderr
        )

    def test_completion_generation_and_candidates(self) -> None:
        self.run_cli("init", "workspace", str(self.workspace))

        # Shell completion script generation
        for sh in ("zsh", "bash", "fish"):
            res = self.run_cli("completion", sh)
            assert res.returncode == 0
            assert len(res.stdout) > 50

        # Dynamic candidates
        for target in ("skills", "projects", "memories", "inbox-completions"):
            res = self.run_cli("completion", "candidates", target)
            assert res.returncode == 0

    def test_adopt_preflight_and_apply(self) -> None:
        self.run_cli("init", "workspace", str(self.workspace))

        # Create mock agent subagent
        agent_dir = self.home / ".claude/agents"
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "ci-adopt.md").write_text(
            "---\nname: ci-adopt\ndescription: CI adoption smoke test\n---\nReview adoption behavior.\n",
            encoding="utf-8",
        )

        # Dry run with verbose
        res_dry = self.run_cli("adopt", "--dry-run", "--verbose")
        assert "Safe to apply" in res_dry.stdout or "Safe to apply" in res_dry.stderr
        assert not (self.workspace / "subagents/ci-adopt.md").exists()

        # Dry run with skip
        res_skip = self.run_cli(
            "adopt", "--dry-run", "--verbose", "--skip", "subagent/ci-adopt"
        )
        assert (
            "[SKIP] subagent/ci-adopt" in res_skip.stdout
            or "[SKIP] subagent/ci-adopt" in res_skip.stderr
        )

        # Real apply
        res_apply = self.run_cli("adopt")
        assert res_apply.returncode == 0
        adopted = self.workspace / "subagents/ci-adopt.md"
        assert adopted.is_file()
        assert "CI adoption smoke test" in adopted.read_text(encoding="utf-8")

    def test_diff_drilldown_isolation_and_paths(self, tmp_path: Path) -> None:
        fixture_dir = (tmp_path / "diff-drilldown").resolve()
        tests_dir = Path(__file__).resolve().parent
        if str(tests_dir) not in sys.path:
            sys.path.insert(0, str(tests_dir))
        from diff_drilldown_fixture import create_fixture

        create_fixture(fixture_dir)
        env = dict(self.env, AIKITO_DIR=str(fixture_dir))

        def run_diff(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [sys.executable, "-m", "aikito", "diff", *args],
                env=env,
                capture_output=True,
                text=True,
                check=True,
            )

        # 1. diff project demo shows checkouts
        res_proj = run_diff("project", "demo")
        assert f"Checkout: {fixture_dir / 'first'}" in res_proj.stdout
        assert f"Checkout: {fixture_dir / 'second'}" in res_proj.stdout

        # 2. diff project demo example ./scripts/check.py shows file diffs
        res_file = run_diff("project", "demo", "example", "./scripts/check.py")
        assert "-local first" in res_file.stdout
        assert "-local second" in res_file.stdout

        # 3. diff project demo example scripts\check.py (backslash path)
        res_backslash = run_diff("project", "demo", "example", r"scripts\check.py")
        assert res_file.stdout == res_backslash.stdout

        # 4. global diff after removing broken toml
        (fixture_dir / "mcps/broken.toml").unlink()
        res_global = run_diff()
        assert f"Checkout: {fixture_dir / 'first'}" in res_global.stdout
        assert f"Checkout: {fixture_dir / 'second'}" in res_global.stdout
