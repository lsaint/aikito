"""Unit tests for instruction batching, pure planning, and plan execution."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest import TestCase

from aikito.agents import Target
from aikito.compat import safe_symlink
from aikito.instructions import (
    InstructionBatch,
    build_global_instruction_batch,
    build_project_instruction_batch,
    execute_instruction_plan,
    plan_instructions,
)
from aikito.project_sync import build_project_sync_batch
from layout_helpers import write_agents


class InstructionBatchAndPlanTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.co = self.root / "checkout"
        self.home.mkdir()
        self.ws.mkdir()
        self.co.mkdir()

        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".agents" / "skills").mkdir(parents=True)

        write_agents(
            self.ws,
            "[agents.codex]\n"
            'display_name = "Codex"\n'
            'instruction_path = ".codex/AGENTS.md"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            "[agents.claude-code]\n"
            'display_name = "Claude Code"\n'
            'instruction_path = ".claude/CLAUDE.md"\n'
            'project_instruction_path = ".claude/CLAUDE.md"\n'
            'skills_path = ".claude/skills"\n',
        )

        (self.ws / "global").mkdir()
        self.global_agents_md = self.ws / "global" / "AGENTS.md"
        self.global_agents_md.write_text("# Global instructions\n", encoding="utf-8")

        self.proj_dir = self.ws / "projects" / "demo"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = []\n',
            encoding="utf-8",
        )
        self.proj_agents_md = self.proj_dir / "AGENTS.md"
        self.proj_agents_md.write_text("# Project instructions\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_build_global_instruction_batch_attributes(self) -> None:
        batch = build_global_instruction_batch(self.ws, self.home)
        self.assertEqual(batch.scope, "global")
        self.assertEqual(batch.canonical_source, self.global_agents_md)
        self.assertTrue(batch.enabled)
        self.assertEqual(batch.resource_count, 1)
        self.assertEqual(batch.target_count, 2)
        self.assertEqual(batch.consumer_count, 2)

    def test_build_project_instruction_batch_enabled_and_disabled(self) -> None:
        # Non-empty canonical: enabled
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertEqual(batch.scope, "project")
        self.assertTrue(batch.enabled)
        self.assertEqual(batch.target_count, 2)

        # Empty canonical: disabled
        self.proj_agents_md.write_text("", encoding="utf-8")
        batch_empty = build_project_instruction_batch(
            self.ws, "demo", self.co, self.home
        )
        self.assertFalse(batch_empty.enabled)

    def test_plan_global_instructions_missing_source_causes_conflict(self) -> None:
        self.global_agents_md.unlink()
        batch = build_global_instruction_batch(self.ws, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(all(op.rule_id == "INV-TR-02" for op in plan.conflicts))

    def test_plan_global_instructions_create_and_noop(self) -> None:
        batch = build_global_instruction_batch(self.ws, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.planned_change_count, 2)
        self.assertTrue(all(op.action == "CREATE" for op in plan.operations))

        # Execute plan
        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertEqual(res.applied_count, 2)

        # Plan again: should be NOOP
        plan2 = plan_instructions(batch, self.home)
        self.assertTrue(plan2.can_apply)
        self.assertEqual(plan2.planned_change_count, 0)
        self.assertEqual(plan2.noop_count, 2)

    def test_plan_global_instructions_regular_file_conflict(self) -> None:
        target_file = self.home / ".codex" / "AGENTS.md"
        target_file.write_text("local file", encoding="utf-8")

        batch = build_global_instruction_batch(self.ws, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertFalse(plan.can_apply)
        conflicts = [op for op in plan.conflicts if op.target_path == target_file]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].rule_id, "INV-INST-03")

    def test_plan_global_instructions_wrong_symlink_conflict(self) -> None:
        target_link = self.home / ".codex" / "AGENTS.md"
        other = self.root / "other.md"
        other.write_text("other", encoding="utf-8")
        safe_symlink(other, target_link)

        batch = build_global_instruction_batch(self.ws, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertFalse(plan.can_apply)
        conflicts = [op for op in plan.conflicts if op.target_path == target_link]
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(conflicts[0].rule_id, "INV-INST-06")

    def test_plan_project_instructions_empty_unlinks_exact_owned(self) -> None:
        # First create the link
        target_link = self.co / "AGENTS.md"
        safe_symlink(self.proj_agents_md.resolve(), target_link)

        # Empty the canonical instructions
        self.proj_agents_md.write_text("", encoding="utf-8")
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertFalse(batch.enabled)

        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        unlink_ops = [op for op in plan.operations if op.target_path == target_link]
        self.assertEqual(len(unlink_ops), 1)
        self.assertEqual(unlink_ops[0].action, "UNLINK")
        self.assertEqual(unlink_ops[0].rule_id, "INV-INST-08")

        # Execute unlink
        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertFalse(target_link.exists())
        self.assertFalse(target_link.is_symlink())

    def test_plan_project_instructions_empty_preserves_regular_file(self) -> None:
        regular_file = self.co / "AGENTS.md"
        regular_file.write_text("user content", encoding="utf-8")

        self.proj_agents_md.write_text("", encoding="utf-8")
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)

        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        file_ops = [op for op in plan.operations if op.target_path == regular_file]
        self.assertEqual(len(file_ops), 1)
        self.assertEqual(file_ops[0].action, "NOOP")
        self.assertEqual(file_ops[0].rule_id, "INV-INST-10")

        # Execute: file remains untouched
        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertTrue(regular_file.is_file())
        self.assertEqual(regular_file.read_text(encoding="utf-8"), "user content")

    def test_plan_project_instructions_empty_preserves_foreign_symlink(self) -> None:
        foreign_target = self.root / "foreign.md"
        foreign_target.write_text("foreign", encoding="utf-8")
        target_link = self.co / "AGENTS.md"
        safe_symlink(foreign_target, target_link)

        self.proj_agents_md.write_text("", encoding="utf-8")
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)

        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        link_ops = [op for op in plan.operations if op.target_path == target_link]
        self.assertEqual(len(link_ops), 1)
        self.assertEqual(link_ops[0].action, "NOOP")
        self.assertEqual(link_ops[0].rule_id, "INV-INST-08")

        # Execute: foreign link remains untouched
        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertTrue(target_link.is_symlink())
        self.assertEqual(target_link.resolve(strict=False), foreign_target.resolve())

    def test_legacy_agents_instruction_target_unlinked_when_exact_owned(self) -> None:
        legacy_path = self.co / ".agents" / "AGENTS.md"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        safe_symlink(self.proj_agents_md.resolve(), legacy_path)

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertTrue(any(t.path == legacy_path for t in batch.stale_targets))

        plan = plan_instructions(batch, self.home)
        stale_ops = [op for op in plan.operations if op.target_path == legacy_path]
        self.assertEqual(len(stale_ops), 1)
        self.assertEqual(stale_ops[0].action, "UNLINK")
        self.assertEqual(stale_ops[0].rule_id, "INV-INST-09")

        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertFalse(legacy_path.exists())

    def test_offline_checkout_instructions_planned_as_skip(self) -> None:
        batch = build_project_instruction_batch(
            self.ws, "demo", self.co, self.home, is_offline=True
        )
        plan = plan_instructions(batch, self.home, is_offline=True)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.skip_count, 2)
        self.assertTrue(all(op.rule_id == "INV-INST-11" for op in plan.operations))

    def test_execute_dry_run_zero_writes(self) -> None:
        batch = build_global_instruction_batch(self.ws, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertEqual(plan.planned_change_count, 2)

        res = execute_instruction_plan(plan, self.home, dry_run=True)
        self.assertTrue(res.success)
        self.assertEqual(res.applied_count, 0)

        # Verify nothing was written
        codex_target = self.home / ".codex" / "AGENTS.md"
        self.assertFalse(codex_target.exists())

    def test_multi_checkout_and_offline_batch_planning(self) -> None:
        co2 = self.root / "checkout2"
        co2.mkdir()
        offline_co = self.root / "offline_checkout"

        batch = build_project_instruction_batch(
            self.ws,
            "demo",
            checkout=[self.co, co2],
            home=self.home,
            offline_checkouts=[offline_co],
        )
        self.assertEqual(len(batch.targets), 6)  # 2 targets * 3 checkouts

        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.planned_change_count, 4)
        self.assertEqual(plan.skip_count, 2)
        offline_ops = [
            op
            for op in plan.operations
            if offline_co in op.target_path.parents or op.target_path == offline_co
        ]
        self.assertEqual(len(offline_ops), 2)
        self.assertTrue(
            all(
                op.action == "SKIP" and op.rule_id == "INV-INST-11"
                for op in offline_ops
            )
        )

    def test_build_project_sync_batch_blocks_on_instruction_conflict(self) -> None:
        conflict_file = self.co / "AGENTS.md"
        conflict_file.write_text("existing unmanaged content", encoding="utf-8")

        data = {
            "name": "demo",
            "paths": [str(self.co)],
            "skills": [],
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertFalse(batch.can_apply)
        self.assertIsNotNone(batch.instruction_plan)
        self.assertTrue(batch.instruction_plan.has_conflicts)
        self.assertFalse(
            any(
                "Pre-existing regular instruction file" in err
                for err in batch.preflight_findings
            )
        )
        self.assertTrue(
            any(
                "Pre-existing regular instruction file" in finding.message
                for finding in batch.observe().findings
            )
        )

    def test_global_instructions_repeated_sync_is_noop_and_preserves_mtime(
        self,
    ) -> None:
        batch = build_global_instruction_batch(self.ws, self.home)
        plan1 = plan_instructions(batch, self.home)
        res1 = execute_instruction_plan(plan1, self.home)
        self.assertTrue(res1.success)
        self.assertGreater(res1.applied_count, 0)

        target_file = self.home / ".codex" / "AGENTS.md"
        self.assertTrue(target_file.is_symlink())
        mtime_before = target_file.lstat().st_mtime_ns
        readlink_before = os.readlink(target_file)

        # Second sync
        plan2 = plan_instructions(batch, self.home)
        self.assertEqual(plan2.planned_change_count, 0)
        self.assertGreater(plan2.noop_count, 0)
        res2 = execute_instruction_plan(plan2, self.home)
        self.assertTrue(res2.success)
        self.assertEqual(res2.applied_count, 0)

        self.assertEqual(target_file.lstat().st_mtime_ns, mtime_before)
        self.assertEqual(os.readlink(target_file), readlink_before)

    def test_project_instructions_repeated_sync_is_noop_and_preserves_mtime(
        self,
    ) -> None:
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan1 = plan_instructions(batch, self.home)
        res1 = execute_instruction_plan(plan1, self.home)
        self.assertTrue(res1.success)
        self.assertGreater(res1.applied_count, 0)

        target_file = self.co / "AGENTS.md"
        self.assertTrue(target_file.is_symlink())
        mtime_before = target_file.lstat().st_mtime_ns
        readlink_before = os.readlink(target_file)

        # Second sync
        plan2 = plan_instructions(batch, self.home)
        self.assertEqual(plan2.planned_change_count, 0)
        self.assertGreater(plan2.noop_count, 0)
        res2 = execute_instruction_plan(plan2, self.home)
        self.assertTrue(res2.success)
        self.assertEqual(res2.applied_count, 0)

        self.assertEqual(target_file.lstat().st_mtime_ns, mtime_before)
        self.assertEqual(os.readlink(target_file), readlink_before)

    def test_empty_canonical_cleanup_repeated_sync_is_noop(self) -> None:
        self.proj_agents_md.write_text("", encoding="utf-8")
        target_link = self.co / "AGENTS.md"
        safe_symlink(self.proj_agents_md.resolve(), target_link)

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan1 = plan_instructions(batch, self.home)
        res1 = execute_instruction_plan(plan1, self.home)
        self.assertTrue(res1.success)
        self.assertEqual(res1.applied_count, 1)
        self.assertFalse(target_link.exists())

        # Second sync with empty canonical
        plan2 = plan_instructions(batch, self.home)
        self.assertEqual(plan2.planned_change_count, 0)
        res2 = execute_instruction_plan(plan2, self.home)
        self.assertTrue(res2.success)
        self.assertEqual(res2.applied_count, 0)

    def test_project_dry_run_with_empty_canonical_does_not_unlink(self) -> None:
        self.proj_agents_md.write_text("", encoding="utf-8")
        target_link = self.co / "AGENTS.md"
        safe_symlink(self.proj_agents_md.resolve(), target_link)

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        res = execute_instruction_plan(plan, self.home, dry_run=True)
        self.assertTrue(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertTrue(target_link.is_symlink())

    def test_legacy_agents_instruction_target_unlinked_when_formal_target_also_points_to_canonical(
        self,
    ) -> None:
        # Both formal AGENTS.md and legacy .agents/AGENTS.md point to canonical
        formal_target = self.co / "AGENTS.md"
        safe_symlink(self.proj_agents_md.resolve(), formal_target)

        legacy_path = self.co / ".agents" / "AGENTS.md"
        legacy_path.parent.mkdir(parents=True, exist_ok=True)
        safe_symlink(self.proj_agents_md.resolve(), legacy_path)

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        self.assertTrue(any(t.path == legacy_path for t in batch.stale_targets))

        plan = plan_instructions(batch, self.home)
        formal_ops = [op for op in plan.operations if op.target_path == formal_target]
        stale_ops = [op for op in plan.operations if op.target_path == legacy_path]

        self.assertEqual(len(formal_ops), 1)
        self.assertEqual(formal_ops[0].action, "NOOP")
        self.assertEqual(len(stale_ops), 1)
        self.assertEqual(stale_ops[0].action, "UNLINK")
        self.assertEqual(stale_ops[0].rule_id, "INV-INST-09")

        res = execute_instruction_plan(plan, self.home)
        self.assertTrue(res.success)
        self.assertFalse(legacy_path.exists())
        self.assertTrue(formal_target.is_symlink())

    def test_stale_plan_rejected_when_canonical_cleared_after_planning(self) -> None:
        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        self.assertGreater(plan.planned_change_count, 0)

        # Clear canonical between plan and execute
        self.proj_agents_md.write_text("", encoding="utf-8")

        res = execute_instruction_plan(plan, self.home)
        self.assertFalse(res.success)
        self.assertIn("stale plan", res.error_message or "")

        # Target should not have been created
        self.assertFalse((self.co / "AGENTS.md").exists())

    def test_apply_project_sync_batch_blocks_skill_writes_on_instruction_conflict(
        self,
    ) -> None:
        from aikito.project_sync import apply_project_sync_batch

        # Create a skill in workspace
        skill_dir = self.ws / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Demo Skill\n", encoding="utf-8")

        # Conflict on instruction target
        conflict_file = self.co / "AGENTS.md"
        conflict_file.write_text("existing unmanaged content", encoding="utf-8")

        data = {
            "name": "demo",
            "paths": [str(self.co)],
            "skills": ["demo-skill"],
        }
        batch = build_project_sync_batch(self.ws, self.home, "demo", data)
        self.assertFalse(batch.can_apply)

        # Apply must fail without writing skills
        result = apply_project_sync_batch(batch, data, self.home)
        self.assertFalse(result.is_success)
        self.assertEqual(len(result.applied_ops), 0)

        # Verify no skill was written
        skill_runtime = self.co / ".agents" / "skills" / "demo-skill"
        self.assertFalse(skill_runtime.exists())

        # Skill segment must reflect zero failed operations (skills were not executed)
        self.assertEqual(len(result.failed_ops), 0)
        self.assertTrue(result.skill_result.is_success)
        self.assertIsNotNone(result.instruction_result)
        self.assertFalse(result.instruction_result.success)
        self.assertGreater(result.instruction_result.conflict_count, 0)

    def test_noop_plan_fails_preflight_if_target_replaced_with_external_symlink(
        self,
    ) -> None:
        target = self.co / "AGENTS.md"
        safe_symlink(self.proj_agents_md.resolve(), target)

        batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
        plan = plan_instructions(batch, self.home)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.noop_count, 1)

        # Before execution: replace valid canonical symlink with external symlink
        external_file = self.root / "external.md"
        external_file.write_text("external content", encoding="utf-8")
        target.unlink()
        safe_symlink(external_file, target)

        res = execute_instruction_plan(plan, self.home)
        self.assertFalse(res.success)
        self.assertIn("stale plan", res.error_message or "")

    def test_shared_path_fails_preflight_if_target_replaced_with_symlink(self) -> None:
        # Same-object target: target path is the canonical file
        batch = InstructionBatch(
            scope="project",
            canonical_source=self.proj_agents_md,
            targets=(
                Target(
                    kind="instruction_link",
                    scope="project",
                    path=self.proj_agents_md,
                    canonical_source=self.proj_agents_md,
                    consumers=("test",),
                    consumer_display_names=("Test",),
                ),
            ),
            enabled=True,
        )
        plan = plan_instructions(batch, self.home)
        self.assertEqual(plan.same_object_count, 1)

        # Before execution: replace canonical file with a symlink
        self.proj_agents_md.unlink()
        external_file = self.root / "external.md"
        external_file.write_text("external content", encoding="utf-8")
        safe_symlink(external_file, self.proj_agents_md)

        res = execute_instruction_plan(plan, self.home)
        self.assertFalse(res.success)
        self.assertIn("stale plan", res.error_message or "")

    def test_build_project_instruction_batch_deduplicates_symlink_alias_checkouts(
        self,
    ) -> None:
        # Create a symlink alias to the same checkout directory
        alias_co = self.root / "checkout-alias"
        safe_symlink(self.co.resolve(), alias_co)

        batch = build_project_instruction_batch(
            self.ws, "demo", checkout=[self.co, alias_co], home=self.home
        )
        single_batch = build_project_instruction_batch(
            self.ws, "demo", checkout=self.co, home=self.home
        )
        # Should deduplicate targets across checkout aliases so count equals single checkout
        self.assertEqual(len(batch.targets), len(single_batch.targets))
        self.assertEqual(len(batch.targets), 2)

    def test_case_insensitive_legacy_agents_instruction_target_not_unlinked(
        self,
    ) -> None:
        from unittest.mock import patch

        formal_target = self.co / ".agents" / "agents.md"
        formal_target.parent.mkdir(parents=True, exist_ok=True)
        safe_symlink(self.proj_agents_md.resolve(), formal_target)

        # Configure agent with lowercase instruction target
        write_agents(
            self.ws,
            "[agents.codex]\n"
            'display_name = "Codex"\n'
            'project_instruction_path = ".agents/agents.md"\n',
        )

        with patch("aikito.compat.is_directory_case_sensitive", return_value=False):
            batch = build_project_instruction_batch(self.ws, "demo", self.co, self.home)
            # .agents/AGENTS.md must be recognized as the formal target and not treated as stale
            self.assertFalse(
                any(t.path.name.upper() == "AGENTS.MD" for t in batch.stale_targets)
            )

            plan = plan_instructions(batch, self.home)
            res = execute_instruction_plan(plan, self.home)
            self.assertTrue(res.success)
            self.assertTrue(formal_target.is_symlink())

    def test_case_insensitive_global_grok_legacy_target_not_unlinked_when_formal(
        self,
    ) -> None:
        from unittest.mock import patch

        write_agents(
            self.ws,
            "[agents.grok]\n"
            'display_name = "Grok"\n'
            'instruction_path = ".grok/agents.md"\n',
        )
        with patch("aikito.compat.is_directory_case_sensitive", return_value=False):
            batch = build_global_instruction_batch(self.ws, self.home)
            self.assertEqual(len(batch.stale_targets), 0)
