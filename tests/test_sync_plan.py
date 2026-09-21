import unittest

from aikito.sync_plan import SyncPlan, capture_sync_plan


class SyncPlanTest(unittest.TestCase):
    def test_default_render_is_concise_but_keeps_attention_items(self) -> None:
        plan = SyncPlan(
            stdout=(
                "[DRY RUN LINK] /workspace/skill -> /home/.agents/skills/skill\n"
                "[OK] codex instructions\n"
                "[SKIP] pi not detected: /home/.pi\n"
                "[WARN] codex/server: missing credential TOKEN\n"
            ),
            stderr="[CONFLICT] unmanaged target: /home/.codex/AGENTS.md\n",
            can_apply=False,
        )

        rendered = plan.render()

        self.assertIn("Changes:   1", rendered)
        self.assertIn("Unchanged: 1", rendered)
        self.assertIn("Offline:   1", rendered)
        self.assertIn("missing credential TOKEN", rendered)
        self.assertIn("unmanaged target", rendered)
        self.assertIn("Blocked; no changes were made", rendered)
        self.assertNotIn("/workspace/skill ->", rendered)
        self.assertNotIn("pi not detected", rendered)

    def test_verbose_render_includes_complete_details(self) -> None:
        plan = SyncPlan(
            stdout="[DRY RUN LINK] /source -> /target\n",
            stderr="",
            can_apply=True,
        )

        rendered = plan.render(verbose=True)

        self.assertIn("Safe to apply", rendered)
        self.assertIn("Details", rendered)
        self.assertIn("/source -> /target", rendered)

    def test_capture_redirects_preview_output(self) -> None:
        def preview() -> bool:
            print("[OK] ready")
            return True

        plan = capture_sync_plan(preview)

        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.unchanged, 1)

    def test_structured_plans_drive_changes_and_conflicts(self) -> None:
        from unittest.mock import MagicMock
        from aikito.subagent import SubagentPlan
        from aikito.config_runtime import ConfigOperation, ConfigTarget
        from aikito.mcp import MCPPlan, MCPOperation, MCPConfigTarget

        sub_op = ConfigOperation(
            target=ConfigTarget(path=None, logical_identity="claude/verifier", agent="claude"),
            action="CREATE",
            reason="new subagent",
            is_authorized=True,
        )
        sub_plan = SubagentPlan(
            operations=(sub_op,),
            file_plans=(),
        )

        mcp_op = MCPOperation(
            target=MCPConfigTarget(path=None, logical_identity="myserver", agent="claude"),
            action="CONFLICT",
            reason="external drift",
            is_authorized=False,
        )
        mcp_plan = MCPPlan(
            operations=(mcp_op,),
            file_plans=(),
            state_snapshot_hash="dummy",
        )

        plan = SyncPlan(
            stdout="preview text",
            stderr="",
            can_apply=True,
            subagent_plan=sub_plan,
            mcp_plan=mcp_plan,
        )

        self.assertFalse(plan.can_apply)
        self.assertEqual(plan.changes, 1)  # 1 from subagent
        self.assertEqual(len(plan.conflicts), 1)
        self.assertIn("claude/myserver: external drift", plan.conflicts[0])

    def test_capture_evaluates_plan_lambdas_after_preview(self) -> None:
        from unittest.mock import MagicMock
        holder = {"sub": None, "mcp": None}

        def preview() -> bool:
            sub = MagicMock()
            sub.can_apply = True
            sub.operations = ()
            mcp = MagicMock()
            mcp.can_apply = False
            mcp.operations = ()
            mcp.changes_count = 0
            holder["sub"] = sub
            holder["mcp"] = mcp
            return True

        plan = capture_sync_plan(
            preview,
            subagent_plan_fn=lambda: holder["sub"],
            mcp_plan_fn=lambda: holder["mcp"],
        )

        self.assertFalse(plan.can_apply)
        self.assertIs(plan.subagent_plan, holder["sub"])
        self.assertIs(plan.mcp_plan, holder["mcp"])

    def test_mcp_stdout_dry_run_does_not_double_count(self) -> None:
        """Custom agent MCP dry run in stdout must not double count changes when mcp_plan is present."""
        from aikito.mcp import MCPPlan, MCPOperation, MCPConfigTarget

        mcp_op = MCPOperation(
            target=MCPConfigTarget(path=None, logical_identity="srv", agent="custom"),
            action="CREATE",
            reason="new server",
            is_authorized=True,
        )
        mcp_plan = MCPPlan(
            operations=(mcp_op,),
            file_plans=(),
            state_snapshot_hash="dummy",
        )

        plan = SyncPlan(
            stdout="[DRY-RUN] custom/srv: would create entry\n",
            stderr="",
            can_apply=True,
            mcp_plan=mcp_plan,
        )

        # Must be exactly 1, not 2!
        self.assertEqual(plan.changes, 1)


if __name__ == "__main__":
    unittest.main()
