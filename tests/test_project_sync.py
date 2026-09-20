"""Unit tests for project_sync orchestration, preflight, and batch execution."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import MagicMock, patch

from aikito.project_sync import (
    LegacySyncResult,
    ProjectSyncBatch,
    ProjectSyncExecutionResult,
    apply_project_sync_batch,
    build_project_sync_batch,
)


class ProjectSyncBatchTests(TestCase):
    def test_preflight_collects_errors_and_blocks_batch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            (ws / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )

            # Missing canonical skill and memory
            data = {
                "name": "demo",
                "skills": ["nonexistent-skill"],
                "memory": ["nonexistent-mem.md"],
                "path": str(co),
            }

            batch = build_project_sync_batch(ws, home, "demo", data)
            self.assertFalse(batch.can_apply)
            self.assertTrue(len(batch.legacy_preflight_errors) > 0)
            self.assertTrue(
                any("nonexistent-skill" in e for e in batch.legacy_preflight_errors)
            )

    def test_successful_project_sync_applies_skills_and_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            (ws / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# S\n", encoding="utf-8")

            mem_canon = ws / "memory" / "notes.md"
            mem_canon.parent.mkdir(parents=True)
            mem_canon.write_text("# Notes\n", encoding="utf-8")

            data = {
                "name": "demo",
                "sync_mode": "copy",
                "skills": ["my-skill"],
                "memory": ["notes.md"],
                "path": str(co),
            }

            batch = build_project_sync_batch(ws, home, "demo", data)
            self.assertTrue(batch.can_apply)

            res = apply_project_sync_batch(batch, data, home, dry_run=False)
            self.assertTrue(res.is_success)

            # Assert skill copy was created
            skill_target = co / ".agents" / "skills" / "my-skill"
            self.assertTrue(skill_target.is_dir())
            self.assertFalse(skill_target.is_symlink())

            # Assert memory link was created
            mem_target = co / ".agents" / "memory" / "notes.md"
            self.assertTrue(mem_target.is_symlink())

    def test_offline_checkout_does_not_raise_name_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home = (root / "home").resolve()
            ws = (root / "workspace").resolve()
            co_active = (root / "checkout-active").resolve()
            co_offline = (root / "checkout-offline-not-existing").resolve()
            home.mkdir()
            ws.mkdir()
            co_active.mkdir()

            (ws / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# S\n", encoding="utf-8")

            data = {
                "name": "demo",
                "skills": ["my-skill"],
                "paths": [str(co_active), str(co_offline)],
            }

            # Must build batch without NameError (DesiredSkill, ObservedSkill)
            batch = build_project_sync_batch(ws, home, "demo", data)
            self.assertTrue(batch.can_apply)
            self.assertEqual(len(batch.active_checkouts), 1)
            self.assertEqual(len(batch.offline_checkouts), 1)
            # Find the offline op
            offline_ops = [
                op
                for op in batch.skill_plan.operations
                if op.target.physical_checkout.resolve() == co_offline
            ]
            self.assertEqual(len(offline_ops), 1)
            self.assertEqual(offline_ops[0].action, "NOOP")
            self.assertEqual(offline_ops[0].rule_id, "INV-AUTH-02")

    def test_capture_sync_plan_evaluates_batches_fn_after_preview(self) -> None:
        from unittest.mock import MagicMock
        from aikito.sync_plan import capture_sync_plan

        cached: list[any] = []
        mock_batch = MagicMock(can_apply=True, skill_plan=MagicMock(operations=()))

        def preview_callback() -> bool:
            # Populate cached during preview run
            cached.append(mock_batch)
            return True

        # Using skill_batches_fn evaluates after callback runs
        plan = capture_sync_plan(
            preview_callback,
            skill_batches_fn=lambda: list(cached),
        )
        self.assertEqual(plan.skill_batches, (mock_batch,))

    def test_segmented_execution_result_isolates_memory_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home = (root / "home").resolve()
            ws = (root / "workspace").resolve()
            co = (root / "checkout").resolve()
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            (ws / "agents.toml").write_text(
                '[agents.codex]\nskills_path = ".agents/skills"\n',
                encoding="utf-8",
            )
            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# S\n", encoding="utf-8")

            mem_canon = ws / "memory" / "notes.md"
            mem_canon.parent.mkdir(parents=True)
            mem_canon.write_text("# Notes\n", encoding="utf-8")

            data = {
                "name": "demo",
                "sync_mode": "copy",
                "skills": ["my-skill"],
                "memory": ["notes.md"],
                "path": str(co),
            }

            batch = build_project_sync_batch(ws, home, "demo", data)
            self.assertTrue(batch.can_apply)

            # Invalidate sync_resource specifically when syncing memory
            original_sync_resource = __import__(
                "aikito.project_sync", fromlist=["sync_resource"]
            ).sync_resource

            def fail_memory_sync(source: Path, target: Path, **kwargs: Any) -> bool:
                if "memory" in str(target):
                    return False
                return original_sync_resource(source, target, **kwargs)

            with patch("aikito.project_sync.sync_resource", side_effect=fail_memory_sync):
                res = apply_project_sync_batch(batch, data, home, dry_run=False)

            self.assertFalse(res.is_success)
            self.assertIn("Failed to synchronize project memory", res.error_message or "")
            # Skill segment succeeded and was not overwritten
            self.assertTrue(res.skill_result.is_success)
            self.assertEqual(len(res.skill_result.applied_ops), 1)
            self.assertEqual(res.skill_result.failed_ops, ())
            self.assertEqual(res.failed_ops, ())
            # Verify the skill copy was actually created on disk
            self.assertTrue((co / ".agents" / "skills" / "my-skill").is_dir())
            # Legacy segment recorded failure
            self.assertEqual(len(res.legacy_results), 1)
            self.assertEqual(res.legacy_results[0].resource_kind, "memory")
            self.assertFalse(res.legacy_results[0].success)

    def test_segmented_execution_result_isolates_instruction_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td).resolve()
            home = (root / "home").resolve()
            ws = (root / "workspace").resolve()
            co = (root / "checkout").resolve()
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            (ws / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\nskills_path = ".agents/skills"\n',
                encoding="utf-8",
            )
            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# S\n", encoding="utf-8")

            proj_dir = ws / "projects" / "demo"
            proj_dir.mkdir(parents=True)
            (proj_dir / "AGENTS.md").write_text("# Project instructions\n", encoding="utf-8")

            data = {
                "name": "demo",
                "sync_mode": "copy",
                "skills": ["my-skill"],
                "path": str(co),
            }

            batch = build_project_sync_batch(ws, home, "demo", data)
            self.assertTrue(batch.can_apply)

            with patch("aikito.project_sync.sync_project_instruction", return_value=False):
                res = apply_project_sync_batch(batch, data, home, dry_run=False)

            self.assertFalse(res.is_success)
            self.assertIn("Failed to synchronize project instructions", res.error_message or "")
            # Skill segment succeeded and was not overwritten
            self.assertTrue(res.skill_result.is_success)
            self.assertEqual(len(res.skill_result.applied_ops), 1)
            self.assertEqual(res.skill_result.failed_ops, ())
            self.assertEqual(res.failed_ops, ())
            # Verify the skill copy was actually created on disk
            self.assertTrue((co / ".agents" / "skills" / "my-skill").is_dir())
            # Legacy segment recorded failure
            self.assertEqual(len(res.legacy_results), 1)
            self.assertEqual(res.legacy_results[0].resource_kind, "instructions")
            self.assertFalse(res.legacy_results[0].success)
