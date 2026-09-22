"""Unit tests for GlobalSyncPlan and BundledSkillRefreshPlan in workspace_sync.py."""

from __future__ import annotations

import io
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.cli import _run_workspace_sync
from aikito.templating import BUNDLED_SKILL_NAMES, bundled_skill_path
from aikito.workspace_sync import (
    BundledSkillRefreshPlan,
    GlobalSyncExecutionResult,
    build_bundled_refresh_plan,
    build_global_sync_plan,
    execute_bundled_refresh_plan,
    execute_global_sync_plan,
)


class GlobalSyncPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.ws = self.root / "workspace"
        self.home = self.root / "home"
        self.ws.mkdir()
        self.home.mkdir()

        (self.ws / "config.toml").write_text(
            '[workspace]\nversion = "1.0"\n', encoding="utf-8"
        )
        (self.ws / "skills.toml").write_text("skills = []\n", encoding="utf-8")
        (self.ws / "skills").mkdir()
        (self.ws / "global").mkdir()
        (self.ws / "global" / "AGENTS.md").write_text(
            "# Global Instructions\n", encoding="utf-8"
        )

        agents_toml = """
[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"
skills_path = ".claude/skills"
"""
        (self.ws / "agents.toml").write_text(agents_toml, encoding="utf-8")
        (self.ws / "subagents.toml").write_text("[subagents]\n", encoding="utf-8")
        (self.ws / "mcps").mkdir()

        # Copy bundled skills to make workspace clean initially
        for name in BUNDLED_SKILL_NAMES:
            shutil.copytree(bundled_skill_path(name), self.ws / "skills" / name)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_bundled_refresh_plan_noop_when_identical(self) -> None:
        """When workspace bundled skills match package, plan produces NOOP and replan_required=False."""
        plan = build_bundled_refresh_plan(self.ws, self.home)
        self.assertIsInstance(plan, BundledSkillRefreshPlan)
        self.assertTrue(plan.can_apply)
        self.assertFalse(plan.replan_required)
        self.assertEqual(plan.refreshed_names, ())
        self.assertTrue(all(op.action == "NOOP" for op in plan.operations))

    def test_bundled_refresh_plan_refresh_when_divergent(self) -> None:
        """When a bundled skill diverges, plan produces REFRESH and replan_required=True."""
        (self.ws / "skills" / "aikito" / "SKILL.md").write_text("custom\n", encoding="utf-8")
        plan = build_bundled_refresh_plan(self.ws, self.home)
        self.assertTrue(plan.can_apply)
        self.assertTrue(plan.replan_required)
        self.assertIn("aikito", plan.refreshed_names)
        aikito_op = next(op for op in plan.operations if op.skill_name == "aikito")
        self.assertEqual(aikito_op.action, "REFRESH")

    def test_execute_bundled_refresh_plan_dry_run_zero_write(self) -> None:
        """Dry-run bundled refresh plan creates no backups and makes no disk changes."""
        (self.ws / "skills" / "aikito" / "SKILL.md").write_text("custom\n", encoding="utf-8")
        plan = build_bundled_refresh_plan(self.ws, self.home)
        refreshed = execute_bundled_refresh_plan(plan, self.ws, self.home, dry_run=True)
        self.assertEqual(refreshed, ("aikito",))
        # Custom content preserved in dry run
        self.assertEqual(
            (self.ws / "skills" / "aikito" / "SKILL.md").read_text(encoding="utf-8"),
            "custom\n",
        )
        self.assertFalse((self.home / ".aikito").exists())

    def test_execute_bundled_refresh_plan_apply_refreshes_and_backs_up(self) -> None:
        """Applying bundled refresh plan updates skill from package and creates timestamped backup."""
        (self.ws / "skills" / "aikito" / "SKILL.md").write_text("custom\n", encoding="utf-8")
        plan = build_bundled_refresh_plan(self.ws, self.home)
        refreshed = execute_bundled_refresh_plan(plan, self.ws, self.home, dry_run=False)
        self.assertEqual(refreshed, ("aikito",))
        # Updated from package
        self.assertNotEqual(
            (self.ws / "skills" / "aikito" / "SKILL.md").read_text(encoding="utf-8"),
            "custom\n",
        )
        # Backup created
        backup_dir = self.home / ".aikito" / "backups"
        self.assertTrue(backup_dir.exists())
        self.assertGreater(len(list(backup_dir.glob("bundled-skills_*"))), 0)

    def test_build_global_sync_plan_missing_skills_toml(self) -> None:
        """Missing skills.toml produces non-applicable plan with GLOBAL_SKILLS_MISSING finding."""
        (self.ws / "skills.toml").unlink()
        plan = build_global_sync_plan(self.ws, self.home)
        self.assertFalse(plan.can_apply)
        self.assertIn("Global skills configuration not found", plan.error_message or "")
        self.assertTrue(any(f.code == "GLOBAL_SKILLS_MISSING" for f in plan.findings))

    def test_build_global_sync_plan_conflict_marker(self) -> None:
        """Conflict markers in skills.toml halt plan application."""
        (self.ws / "skills.toml").write_text(
            '<<<<<<< HEAD\nskills = ["a"]\n=======\nskills = ["b"]\n>>>>>>> branch\n',
            encoding="utf-8",
        )
        plan = build_global_sync_plan(self.ws, self.home)
        self.assertFalse(plan.can_apply)
        self.assertIn("Conflict markers detected", plan.error_message or "")
        self.assertTrue(any(f.code == "CONFLICT_MARKER" for f in plan.findings))

    def test_build_and_execute_global_sync_plan_clean(self) -> None:
        """Clean workspace produces applicable plan and successful execution result."""
        container_path = self.home / ".agents" / "skills"
        plan = build_global_sync_plan(
            self.ws, self.home, dry_run=True, container_path=container_path
        )
        self.assertTrue(plan.can_apply)
        self.assertFalse(plan.replan_required_after_apply)
        self.assertIsNotNone(plan.skill_plan)
        self.assertIsNotNone(plan.instruction_plan)

        result = execute_global_sync_plan(plan, self.ws, self.home, dry_run=True)
        self.assertIsInstance(result, GlobalSyncExecutionResult)
        self.assertTrue(result.success)
        self.assertFalse(result.replan_required)
        self.assertIsNotNone(result.skill_result)
        self.assertIsNotNone(result.instruction_result)

    def test_global_sync_plan_replan_required_propagates(self) -> None:
        """When bundled skill needs refresh, replan_required_after_apply is True in plan and result."""
        (self.ws / "skills" / "aikito" / "SKILL.md").write_text("custom\n", encoding="utf-8")
        container_path = self.home / ".agents" / "skills"
        plan = build_global_sync_plan(
            self.ws, self.home, dry_run=True, container_path=container_path
        )
        self.assertTrue(plan.can_apply)
        self.assertTrue(plan.replan_required_after_apply)

        result = execute_global_sync_plan(plan, self.ws, self.home, dry_run=True)
        self.assertTrue(result.success)
        self.assertTrue(result.replan_required)
        self.assertIn("aikito", result.refreshed_bundled)

    def test_workspace_sync_aborts_on_bundled_refresh_without_canonical_snapshots(self) -> None:
        """_run_workspace_sync invalidates plan when global sync requires replan, without canonical_snapshots."""
        # Cause bundled skill to need refresh
        (self.ws / "skills" / "aikito" / "SKILL.md").write_text("custom\n", encoding="utf-8")

        with patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"):
            # In apply mode (dry_run=False), refresh happens in global sync and triggers replan invalidation
            stderr_io = io.StringIO()
            with patch("sys.stderr", stderr_io):
                success = _run_workspace_sync(
                    self.ws,
                    self.home,
                    dry_run=False,
                    cached_project_batches={"p1": ("dummy_batch", {})},
                )
            self.assertFalse(success)
            self.assertIn("workspace sync plan invalidated", stderr_io.getvalue())
