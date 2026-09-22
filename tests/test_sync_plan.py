"""Tests for SyncPlan and stdout independence invariant (INV-APP-03)."""

from __future__ import annotations

import sys
import unittest
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from aikito.sync_plan import SyncPlan
from aikito.workspace_sync import (
    WorkspaceSyncPlan,
    build_workspace_sync_plan,
)


class SyncPlanIndependenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.td = TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.ws = self.root / "ws"
        self.home = self.root / "home"
        self.home.mkdir(parents=True)
        self.ws.mkdir(parents=True)

        # Minimal valid workspace
        (self.ws / "skills.toml").write_text("skills = []\n", encoding="utf-8")
        (self.ws / "agents.toml").write_text("[agents]\n", encoding="utf-8")
        (self.ws / "subagents.toml").write_text("[subagents]\n", encoding="utf-8")
        (self.ws / "mcps").mkdir()
        (self.ws / "skills").mkdir()
        (self.ws / "global").mkdir()
        (self.ws / "global" / "AGENTS.md").write_text(
            "# Global Rules\n", encoding="utf-8"
        )

        # 1 active project
        p_act = self.ws / "projects" / "active_proj"
        p_act.mkdir(parents=True)
        c_act = self.root / "active_checkout"
        c_act.mkdir()
        (p_act / "agent.toml").write_text(
            f'path = "{c_act}"\nskills = []\n', encoding="utf-8"
        )

        # 1 offline project
        p_off = self.ws / "projects" / "offline_proj"
        p_off.mkdir(parents=True)
        (p_off / "agent.toml").write_text(
            'path = "/nonexistent/path"\nskills = []\n', encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_sync_plan_is_workspace_sync_plan(self) -> None:
        """INV-APP-03: SyncPlan is the presentation-tier alias of WorkspaceSyncPlan."""
        self.assertIs(SyncPlan, WorkspaceSyncPlan)

    def test_stdout_stderr_pollution_has_zero_effect_on_plan(self) -> None:
        """INV-APP-03: Arbitrary output in stdout/stderr does not influence plan decisions or counts."""
        # 1. Baseline plan build
        baseline_plan = build_workspace_sync_plan(self.ws, home=self.home)
        baseline_metrics = (
            baseline_plan.changes,
            baseline_plan.unchanged,
            baseline_plan.offline,
            len(baseline_plan.warnings),
            len(baseline_plan.conflicts),
            len(baseline_plan.errors),
            baseline_plan.can_apply,
        )

        # 2. Build plan while stdout/stderr are polluted with conflicting legacy markers
        stdout_trap = StringIO()
        stderr_trap = StringIO()
        with patch("sys.stdout", stdout_trap), patch("sys.stderr", stderr_trap):
            print("[CONFLICT] fake unmanaged target", file=sys.stderr)
            print("[ERROR] fake fatal error", file=sys.stderr)
            print("[CREATE] /fake/path -> /dest", file=sys.stdout)
            print("[WARN] fake warning", file=sys.stdout)
            print("[DRY RUN LINK] fake", file=sys.stdout)
            polluted_plan = build_workspace_sync_plan(self.ws, home=self.home)

        polluted_metrics = (
            polluted_plan.changes,
            polluted_plan.unchanged,
            polluted_plan.offline,
            len(polluted_plan.warnings),
            len(polluted_plan.conflicts),
            len(polluted_plan.errors),
            polluted_plan.can_apply,
        )

        # Plan metrics MUST be 100% identical and independent of printed output
        self.assertEqual(baseline_metrics, polluted_metrics)
        self.assertTrue(polluted_plan.can_apply)
        self.assertEqual(polluted_plan.offline, 1)
        self.assertEqual(len(polluted_plan.conflicts), 0)
        self.assertEqual(len(polluted_plan.errors), 0)

    def test_render_presentation_concise_and_verbose(self) -> None:
        """INV-APP-03: Output rendering formats structured domain items directly."""
        plan = build_workspace_sync_plan(self.ws, home=self.home)

        # Concise rendering
        concise = plan.render(verbose=False)
        self.assertIn("Sync plan", concise)
        self.assertIn("Offline:   1", concise)
        self.assertIn("Safe to apply", concise)
        self.assertNotIn("Details", concise)

        # Verbose rendering
        verbose = plan.render(verbose=True)
        self.assertIn("Details", verbose)
        self.assertIn("offline on this host", verbose)
        self.assertIn("offline_proj", verbose)


if __name__ == "__main__":
    unittest.main()
