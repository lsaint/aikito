"""Unit tests for GlobalSkillBatch and global skills planning."""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from aikito.agents import Agent, AgentAvailability, AgentRegistry
from aikito.global_skills import (
    build_global_skill_batch,
    execute_global_skills,
    plan_global_skills,
)


class GlobalSkillBatchTest(TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        self.workspace = self.root / "aikito"
        self.workspace.mkdir()
        self.skills_dir = self.workspace / "skills"
        self.skills_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

        # Workspace config
        (self.workspace / "skills.toml").write_text(
            'skills = ["skill-a", "skill-b"]\n', encoding="utf-8"
        )
        for s in ("skill-a", "skill-b"):
            s_dir = self.skills_dir / s
            s_dir.mkdir()
            (s_dir / "SKILL.md").write_text(f"# {s}\n", encoding="utf-8")

        # Mock bundled agents registry
        self.agents_skills = self.home / ".agents" / "skills"
        self.claude_skills = self.home / ".claude" / "skills"
        self.agy_skills = self.home / ".gemini" / "antigravity-cli" / "skills"

        self.registry = AgentRegistry(
            {
                "codex": Agent("codex", "Codex", skills_path=self.agents_skills),
                "opencode": Agent(
                    "opencode", "OpenCode", skills_path=self.agents_skills
                ),
                "github-copilot": Agent(
                    "github-copilot", "Copilot", skills_path=self.agents_skills
                ),
                "dsh": Agent("dsh", "DeepSeek", skills_path=self.agents_skills),
                "grok": Agent("grok", "Grok", skills_path=self.agents_skills),
                "pi": Agent("pi", "Pi", skills_path=self.agents_skills),
                "claude-code": Agent(
                    "claude-code", "Claude Code", skills_path=self.claude_skills
                ),
                "agy": Agent("agy", "Antigravity", skills_path=self.agy_skills),
            }
        )

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_build_global_skill_batch_deduplication_and_counts(self) -> None:
        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )

        # Statistics
        self.assertEqual(batch.resource_count, 2)
        self.assertEqual(batch.consumer_count, 8)
        self.assertEqual(batch.consumer_target_count, 3)

        # Three-tier target kinds
        self.assertEqual(batch.container.kind, "managed_container")
        self.assertEqual(batch.container.path, self.agents_skills)

        self.assertEqual(len(batch.selected_entries), 2)
        for entry in batch.selected_entries:
            self.assertEqual(entry.kind, "managed_entry")

        self.assertEqual(len(batch.consumers), 3)
        for c in batch.consumers:
            self.assertEqual(c.kind, "consumer_link")

        # Verify same_object consumer target
        same_obj = [c for c in batch.consumers if c.is_same_object]
        self.assertEqual(len(same_obj), 1)
        self.assertEqual(same_obj[0].path, self.agents_skills)
        self.assertEqual(len(same_obj[0].consumers), 6)

    def test_plan_fresh_sync_creates_links_and_shared_path(self) -> None:
        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        self.assertTrue(plan.can_apply)
        self.assertFalse(plan.has_conflicts)

        # Container op is CREATE (missing directory)
        self.assertEqual(plan.container_op.action, "CREATE")

        # Entries are CREATE
        self.assertEqual(len(plan.entry_ops), 2)
        for op in plan.entry_ops:
            self.assertEqual(op.action, "CREATE")

        # Consumers: 1 SHARED_PATH, 2 CREATE or SKIP
        same_ops = [op for op in plan.consumer_ops if op.action == "SHARED_PATH"]
        self.assertEqual(len(same_ops), 1)
        self.assertEqual(plan.same_object_count, 1)

    def test_plan_stale_owned_link_and_stale_directory(self) -> None:
        # Create container with one stale owned link and one stale directory
        self.agents_skills.mkdir(parents=True)

        stale_canon = self.skills_dir / "stale-skill"
        stale_canon.mkdir()
        stale_link = self.agents_skills / "stale-skill"
        stale_link.symlink_to(stale_canon)

        # Stale directory with matching content
        stale_dir = self.agents_skills / "matching-dir"
        stale_dir.mkdir()
        (stale_dir / "SKILL.md").write_text("content", encoding="utf-8")

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        self.assertEqual(len(batch.stale_entries), 2)

        plan = plan_global_skills(batch, self.home)
        stale_ops = {
            op.target_path.name: op
            for op in plan.entry_ops
            if op.desired_representation == "absent"
        }

        # Stale owned link should be UNLINK
        self.assertEqual(stale_ops["stale-skill"].action, "UNLINK")
        self.assertEqual(stale_ops["stale-skill"].rule_id, "INV-TR-14")

        # Stale normal directory should be PRESERVED as CONFLICT under INV-GLB-03
        self.assertEqual(stale_ops["matching-dir"].action, "CONFLICT")
        self.assertEqual(stale_ops["matching-dir"].rule_id, "INV-GLB-03")

    def test_plan_wrong_consumer_symlink_reports_conflict(self) -> None:
        self.agents_skills.mkdir(parents=True)
        # Point claude skills to external location
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)
        external = self.root / "external-skills"
        external.mkdir()
        self.claude_skills.symlink_to(external)

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        self.assertTrue(plan.has_conflicts)
        self.assertFalse(plan.can_apply)
        conflict_ops = [op for op in plan.consumer_ops if op.action == "CONFLICT"]
        self.assertEqual(len(conflict_ops), 1)
        self.assertEqual(conflict_ops[0].rule_id, "INV-GLB-05")
        self.assertEqual(conflict_ops[0].target_path, self.claude_skills)

    def test_plan_legacy_container_migration(self) -> None:
        # Legacy container: .agents/skills is a symlink to workspace skills root
        self.agents_skills.parent.mkdir(parents=True, exist_ok=True)
        self.agents_skills.symlink_to(self.skills_dir)

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        self.assertEqual(plan.container_op.action, "MIGRATE_CONTAINER")
        self.assertEqual(plan.container_op.rule_id, "INV-GLB-04")
        self.assertTrue(plan.container_op.is_authorized)
        self.assertFalse(plan.has_conflicts)
        self.assertTrue(plan.can_apply)
        self.assertEqual(len(plan.entry_ops), 2)
        for op in plan.entry_ops:
            self.assertEqual(op.action, "CREATE")
            self.assertEqual(op.rule_id, "INV-TR-01")

        # Verify execution successfully migrates container and links entries
        res = execute_global_skills(plan)
        self.assertTrue(res.success)
        self.assertTrue(self.agents_skills.is_dir())
        self.assertFalse(self.agents_skills.is_symlink())
        for s in ("skill-a", "skill-b"):
            entry_link = self.agents_skills / s
            self.assertTrue(entry_link.is_symlink())
            self.assertEqual(entry_link.resolve(), (self.skills_dir / s).resolve())

    def test_plan_bundled_refresh_consistency_when_canonical_missing(self) -> None:
        # Canonical skill does not exist in workspace, but is in refreshed_bundled
        missing_skill = "aikito"
        batch = build_global_skill_batch(
            self.workspace,
            self.home,
            skills=[missing_skill],
            registry=self.registry,
            container_path=self.agents_skills,
        )
        self.agents_skills.mkdir(parents=True, exist_ok=True)

        plan_dry = plan_global_skills(
            batch, self.home, dry_run=True, refreshed_bundled={missing_skill}
        )
        plan_real = plan_global_skills(
            batch, self.home, dry_run=False, refreshed_bundled={missing_skill}
        )

        self.assertFalse(plan_dry.has_conflicts)
        self.assertFalse(plan_real.has_conflicts)
        self.assertTrue(plan_dry.can_apply)
        self.assertTrue(plan_real.can_apply)

        dry_actions = [op.action for op in plan_dry.entry_ops]
        real_actions = [op.action for op in plan_real.entry_ops]
        self.assertEqual(dry_actions, real_actions)
        self.assertEqual(dry_actions, ["CREATE"])

    def test_execute_consumer_links_creates_link(self) -> None:
        self.agents_skills.mkdir(parents=True)
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        res = execute_global_skills(plan)
        self.assertTrue(res.success)
        self.assertTrue(self.claude_skills.is_symlink())
        self.assertEqual(self.claude_skills.resolve(), self.agents_skills.resolve())

    def test_execute_consumer_links_skips_when_parent_missing(self) -> None:
        self.agents_skills.mkdir(parents=True)
        # Parent of claude skills does not exist; agent not installed
        self.assertFalse(self.claude_skills.parent.exists())

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        with patch(
            "aikito.global_skills.check_target_availability",
            return_value=AgentAvailability("not_installed", "mock_not_installed"),
        ):
            plan = plan_global_skills(batch, self.home)

        res = execute_global_skills(plan)
        self.assertTrue(res.success)
        self.assertFalse(self.claude_skills.parent.exists())
        self.assertFalse(self.claude_skills.exists())

    def test_execute_consumer_links_conflict_on_external_symlink_does_not_relink(
        self,
    ) -> None:
        self.agents_skills.mkdir(parents=True)
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)
        external = self.root / "external-skills"
        external.mkdir()
        self.claude_skills.symlink_to(external)

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        # Plan detects conflict (INV-GLB-05)
        self.assertTrue(plan.has_conflicts)
        # If execution is attempted on conflicting op, it fails and does not unlink/relink
        res = execute_global_skills(plan)
        self.assertFalse(res.success)
        self.assertTrue(self.claude_skills.is_symlink())
        self.assertEqual(self.claude_skills.resolve(), external.resolve())

    def test_execute_global_skills_deterministic_pipeline(self) -> None:
        (self.skills_dir / "my-skill").mkdir()
        (self.skills_dir / "my-skill" / "SKILL.md").write_text(
            "# My Skill", encoding="utf-8"
        )
        (self.workspace / "skills.toml").write_text(
            'skills = ["my-skill"]\n', encoding="utf-8"
        )
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)

        batch = build_global_skill_batch(
            self.workspace, self.home, skills=["my-skill"], registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        success, results = execute_global_skills(plan)
        self.assertTrue(success)
        # Container created
        self.assertTrue(self.agents_skills.is_dir())
        # Managed entry created
        entry_link = self.agents_skills / "my-skill"
        self.assertTrue(entry_link.is_symlink())
        self.assertEqual(entry_link.resolve(), (self.skills_dir / "my-skill").resolve())
        # Consumer link created
        self.assertTrue(self.claude_skills.is_symlink())
        self.assertEqual(self.claude_skills.resolve(), self.agents_skills.resolve())

    def test_execute_global_skills_result_metrics(self) -> None:
        (self.skills_dir / "my-skill").mkdir()
        (self.skills_dir / "my-skill" / "SKILL.md").write_text(
            "# My Skill", encoding="utf-8"
        )
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)

        batch = build_global_skill_batch(
            self.workspace, self.home, skills=["my-skill"], registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)

        res = execute_global_skills(plan, refreshed_bundled=["my-skill"])
        self.assertTrue(res.success)
        self.assertEqual(res.resource_count, 1)
        self.assertEqual(res.consumer_count, 8)
        self.assertEqual(res.consumer_target_count, 3)
        self.assertGreater(res.planned_change_count, 0)
        self.assertEqual(res.executed_change_count, res.planned_change_count)
        self.assertEqual(res.refreshed_bundled, ("my-skill",))
        # Verify tuple unpacking compatibility
        ok, ops = res
        self.assertTrue(ok)
        self.assertEqual(len(ops), len(res.operations))

    def test_cross_workspace_ownership_conflict_does_not_adopt(self) -> None:
        # Create second workspace with same skill name
        ws_b = self.root / "aikito-b"
        ws_b_skills = ws_b / "skills"
        ws_b_skill = ws_b_skills / "my-skill"
        ws_b_skill.mkdir(parents=True)
        (ws_b_skill / "SKILL.md").write_text("# WS B Skill", encoding="utf-8")
        (ws_b / "skills.toml").write_text('skills = ["my-skill"]\n', encoding="utf-8")

        # First, workspace A owns the link in container
        self.agents_skills.mkdir(parents=True, exist_ok=True)
        (self.skills_dir / "my-skill").mkdir(parents=True, exist_ok=True)
        target_link = self.agents_skills / "my-skill"
        target_link.symlink_to(self.skills_dir / "my-skill")

        # Now, plan for workspace B against the same target
        batch_b = build_global_skill_batch(
            ws_b, self.home, skills=["my-skill"], registry=self.registry
        )
        plan_b = plan_global_skills(batch_b, self.home)

        self.assertTrue(plan_b.has_conflicts)
        self.assertFalse(plan_b.can_apply)
        entry_op = plan_b.entry_ops[0]
        self.assertEqual(entry_op.action, "CONFLICT")
        self.assertEqual(entry_op.rule_id, "INV-TR-05")
        # Assert §8.3 elements in reason
        self.assertIn(f"Target preserved: {target_link}", entry_op.reason)
        self.assertIn(str(self.skills_dir / "my-skill"), entry_op.reason)
        self.assertIn(str(ws_b_skill), entry_op.reason)
        self.assertIn(
            "Other workspace or unmanaged skill symlink will not be overwritten automatically",
            entry_op.reason,
        )
        self.assertIn(
            "inspect manually, then run 'aikito sync global' again", entry_op.reason
        )

        # Execution must fail and original link must be preserved
        res = execute_global_skills(plan_b)
        self.assertFalse(res.success)
        self.assertEqual(
            target_link.resolve(), (self.skills_dir / "my-skill").resolve()
        )

    def test_legacy_container_subpath_and_external_and_other_workspace_blocked(
        self,
    ) -> None:
        self.agents_skills.parent.mkdir(parents=True, exist_ok=True)

        # 1. Points to subpath of current workspace skills
        subpath = self.skills_dir / "subpath"
        subpath.mkdir(parents=True, exist_ok=True)
        if self.agents_skills.exists() or self.agents_skills.is_symlink():
            self.agents_skills.unlink()
        self.agents_skills.symlink_to(subpath)

        batch = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)
        self.assertEqual(plan.container_op.action, "CONFLICT")
        self.assertEqual(plan.container_op.rule_id, "INV-GLB-04")
        self.assertIn(
            "points to a subpath instead of skills root", plan.container_op.reason
        )
        self.assertFalse(plan.can_apply)

        # 2. Points to other workspace skills root
        other_ws_skills = self.root / "other-aikito" / "skills"
        other_ws_skills.mkdir(parents=True, exist_ok=True)
        self.agents_skills.unlink()
        self.agents_skills.symlink_to(other_ws_skills)

        batch2 = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan2 = plan_global_skills(batch2, self.home)
        self.assertEqual(plan2.container_op.action, "CONFLICT")
        self.assertEqual(plan2.container_op.rule_id, "INV-GLB-04")
        self.assertIn("points outside current workspace", plan2.container_op.reason)
        self.assertFalse(plan2.can_apply)

        # 3. Points to external path
        ext = self.root / "external-container"
        ext.mkdir(parents=True, exist_ok=True)
        self.agents_skills.unlink()
        self.agents_skills.symlink_to(ext)

        batch3 = build_global_skill_batch(
            self.workspace, self.home, registry=self.registry
        )
        plan3 = plan_global_skills(batch3, self.home)
        self.assertEqual(plan3.container_op.action, "CONFLICT")
        self.assertEqual(plan3.container_op.rule_id, "INV-GLB-04")
        self.assertIn("points outside current workspace", plan3.container_op.reason)
        self.assertFalse(plan3.can_apply)

    def test_stale_plan_preflight_rejection(self) -> None:
        (self.skills_dir / "s-stale").mkdir(parents=True, exist_ok=True)
        self.agents_skills.mkdir(parents=True, exist_ok=True)
        self.claude_skills.parent.mkdir(parents=True, exist_ok=True)

        batch = build_global_skill_batch(
            self.workspace, self.home, skills=["s-stale"], registry=self.registry
        )
        plan = plan_global_skills(batch, self.home)
        self.assertTrue(plan.can_apply)

        # Scenario A: Target link appears externally before apply
        target_entry = self.agents_skills / "s-stale"
        target_entry.write_text("external collision", encoding="utf-8")
        res_a = execute_global_skills(plan)
        self.assertFalse(res_a.success)
        self.assertIn("target already exists or changed", str(res_a.error_message))
        target_entry.unlink()

        # Scenario B: Container directory disappears before apply
        shutil.rmtree(self.agents_skills)
        res_b = execute_global_skills(plan)
        self.assertFalse(res_b.success)
        self.assertIn("managed container", str(res_b.error_message))
        self.agents_skills.mkdir(parents=True, exist_ok=True)

        # Scenario C: Canonical skill directory deleted before apply
        shutil.rmtree(self.skills_dir / "s-stale")
        res_c = execute_global_skills(plan)
        self.assertFalse(res_c.success)
        self.assertIn("canonical source does not exist", str(res_c.error_message))
        self.assertIn("stale plan", str(res_c.error_message))
        (self.skills_dir / "s-stale").mkdir(parents=True, exist_ok=True)

        # Scenario D: Consumer parent directory disappears before apply
        shutil.rmtree(self.claude_skills.parent)
        res_d = execute_global_skills(plan)
        self.assertFalse(res_d.success)
        self.assertIn("consumer parent directory missing", str(res_d.error_message))
