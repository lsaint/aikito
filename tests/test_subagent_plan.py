"""Unit tests for Subagent pure planner (SubagentPlan and build_subagent_plan).

Verifies:
- INV-SUB-01: Header Marker as Subagent Ownership Evidence Scoped to Target Node
- INV-SUB-02: Explicit Target Authorization for Overwrite via Force
- INV-SUB-03: Prune Authorization Strictly Bound to Managed Orphans
- INV-SUB-04: DSH Shared Configuration Multi-Subagent Write-Once Aggregation
- INV-SUB-05: Unified Agent Availability Resolution via AgentRegistry
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aikito.subagent import (
    SubagentConfigError,
    SubagentExecutionResult,
    SubagentPlan,
    build_subagent_plan,
    execute_subagent_plan,
)


class SubagentPlanTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.ws = self.root / "workspace"
        self.home = self.root / "home"
        self.ws.mkdir()
        self.home.mkdir()

        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".config" / "opencode").mkdir(parents=True)
        (self.home / ".dsh").mkdir(parents=True)

        self.agents_toml = """
[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"

[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"

[agents.opencode]
display_name = "OpenCode"
instruction_path = ".config/opencode/AGENTS.md"

[agents.opencode.subagents]
config_path = ".config/opencode/agents"
config_format = "opencode_markdown"

[agents.dsh]
display_name = "DeepSeek Harness"
instruction_path = ".dsh/INSTRUCTIONS.md"

[agents.dsh.subagents]
config_path = ".dsh/cordis.patch.yml"
config_format = "dsh_cordis_subagent"
"""
        (self.ws / "agents.toml").write_text(self.agents_toml.strip(), encoding="utf-8")

        (self.ws / "subagents").mkdir()
        (self.ws / "subagents.toml").write_text(
            "[subagents.verifier]\n"
            'description = "Verifier agent"\n'
            'agents = ["claude-code", "opencode", "dsh"]\n'
            "[subagents.reviewer]\n"
            'description = "Reviewer agent"\n'
            'agents = ["dsh"]\n',
            encoding="utf-8",
        )
        (self.ws / "subagents" / "verifier.md").write_text(
            "Verify all changes.\n", encoding="utf-8"
        )
        (self.ws / "subagents" / "reviewer.md").write_text(
            "Review all changes.\n", encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_pure_planner_zero_writes(self) -> None:
        """Planning performs zero writes to the workspace or home directories."""
        plan = build_subagent_plan(self.ws, self.home)
        self.assertIsInstance(plan, SubagentPlan)
        self.assertTrue(plan.can_apply)
        self.assertGreater(plan.changes_count, 0)

        claude_target = self.home / ".claude" / "agents" / "verifier.md"
        dsh_target = self.home / ".dsh" / "cordis.patch.yml"
        self.assertFalse(claude_target.exists())
        self.assertFalse(dsh_target.exists())

    def test_force_authorization_is_target_specific(self) -> None:
        """INV-SUB-02: --force <agent>/<subagent> only authorizes that exact target."""
        claude_agents = self.home / ".claude" / "agents"
        claude_agents.mkdir(parents=True)
        (claude_agents / "verifier.md").write_text(
            "Unmanaged custom content\n", encoding="utf-8"
        )

        opencode_agents = self.home / ".config" / "opencode" / "agents"
        opencode_agents.mkdir(parents=True)
        (opencode_agents / "verifier.md").write_text(
            "Unmanaged custom content\n", encoding="utf-8"
        )

        # 1. Without force: both are unauthorized conflicts, can_apply is False
        plan_no_force = build_subagent_plan(self.ws, self.home)
        self.assertFalse(plan_no_force.can_apply)
        self.assertEqual(plan_no_force.conflicts_count, 2)

        # 2. Authorize only claude-code/verifier
        plan_force_claude = build_subagent_plan(
            self.ws, self.home, force_targets=["claude-code/verifier"]
        )
        # opencode/verifier is still conflicting and unauthorized
        self.assertFalse(plan_force_claude.can_apply)
        self.assertEqual(plan_force_claude.conflicts_count, 1)

        claude_op = [
            op
            for op in plan_force_claude.operations
            if op.target.agent == "claude-code"
            and op.target.logical_identity == "verifier"
        ][0]
        self.assertTrue(claude_op.is_authorized)
        self.assertTrue(claude_op.requires_force)

        opencode_op = [
            op
            for op in plan_force_claude.operations
            if op.target.agent == "opencode"
            and op.target.logical_identity == "verifier"
        ][0]
        self.assertFalse(opencode_op.is_authorized)
        self.assertEqual(opencode_op.action, "CONFLICT")

        # 3. Invalid force format raises SubagentConfigError
        with self.assertRaises(SubagentConfigError):
            build_subagent_plan(self.ws, self.home, force_targets=["invalid-target"])

    def test_prune_managed_orphans_only(self) -> None:
        """INV-SUB-03: Prune authorizes removal of managed orphans, never unmanaged."""
        claude_agents = self.home / ".claude" / "agents"
        claude_agents.mkdir(parents=True)

        managed_orphan = claude_agents / "legacy.md"
        managed_orphan.write_text(
            "<!-- generated by aikito from subagents/legacy.md - edits will be overwritten -->\n"
            "Legacy\n",
            encoding="utf-8",
        )
        unmanaged_file = claude_agents / "user_notes.md"
        unmanaged_file.write_text("Do not delete\n", encoding="utf-8")

        # Without prune
        plan_no_prune = build_subagent_plan(self.ws, self.home, prune=False)
        orphan_ops = [op for op in plan_no_prune.operations if op.action == "ORPHAN"]
        self.assertEqual(len(orphan_ops), 1)
        self.assertEqual(orphan_ops[0].target.logical_identity, "legacy")
        self.assertFalse(orphan_ops[0].is_authorized)

        # With prune
        plan_prune = build_subagent_plan(self.ws, self.home, prune=True)
        remove_ops = [op for op in plan_prune.operations if op.action == "REMOVE"]
        self.assertEqual(len(remove_ops), 1)
        self.assertEqual(remove_ops[0].target.logical_identity, "legacy")
        self.assertTrue(remove_ops[0].is_authorized)

        # unmanaged_file is never planned for prune/remove
        all_planned_identities = {
            op.target.logical_identity for op in plan_prune.operations
        }
        self.assertNotIn("user_notes", all_planned_identities)

    def test_dsh_shared_file_aggregated_into_single_file_plan(self) -> None:
        """INV-SUB-04: Multiple subagents for DSH cordis.patch.yml are aggregated into one FileMutationPlan."""
        plan = build_subagent_plan(self.ws, self.home)

        dsh_ops = [op for op in plan.operations if op.target.agent == "dsh"]
        # verifier and reviewer
        self.assertEqual(len(dsh_ops), 2)

        dsh_file_plans = [
            fp
            for fp in plan.file_plans
            if fp.path == self.home / ".dsh" / "cordis.patch.yml"
        ]
        self.assertEqual(len(dsh_file_plans), 1)
        fp = dsh_file_plans[0]
        self.assertEqual(len(fp.operations), 2)
        self.assertEqual(fp.format, "dsh_cordis_subagent")

    def test_execute_subagent_plan_creates_files_and_reports_result(self) -> None:
        """INV-SUB-06: Execution returns structured SubagentExecutionResult and writes files."""
        plan = build_subagent_plan(self.ws, self.home)
        self.assertTrue(plan.can_apply)

        res = execute_subagent_plan(plan, self.home)
        self.assertIsInstance(res, SubagentExecutionResult)
        self.assertTrue(res.success)
        self.assertGreater(res.applied_count, 0)
        self.assertEqual(res.failed_count, 0)
        self.assertEqual(len(res.failed_files), 0)

        # Check files created on disk
        claude_target = self.home / ".claude" / "agents" / "verifier.md"
        self.assertTrue(claude_target.exists())
        self.assertIn("Verify all changes", claude_target.read_text(encoding="utf-8"))

        dsh_target = self.home / ".dsh" / "cordis.patch.yml"
        self.assertTrue(dsh_target.exists())
        dsh_text = dsh_target.read_text(encoding="utf-8")
        self.assertIn("aikito-subagent-verifier", dsh_text)
        self.assertIn("aikito-subagent-reviewer", dsh_text)

    def test_execute_subagent_plan_stale_detection(self) -> None:
        """INV-CFG-04: External change between plan and execute halts with stale plan error."""
        dsh_target = self.home / ".dsh" / "cordis.patch.yml"
        dsh_target.write_text("initial: true\n", encoding="utf-8")

        plan = build_subagent_plan(self.ws, self.home)

        # External modification occurs before apply
        dsh_target.write_text("tampered: true\n", encoding="utf-8")

        res = execute_subagent_plan(plan, self.home)
        self.assertFalse(res.success)
        self.assertIn("stale", (res.error_message or "").lower())
        self.assertIn(dsh_target, res.failed_files)
