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


if __name__ == "__main__":
    unittest.main()
