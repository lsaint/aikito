"""Unit tests for memory_runtime module.

Tests MemoryResource, MemoryBatch, MemoryPlan pure planning, exact ownership,
legacy notes migration, offline checkout, and execute_memory_plan.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from aikito.compat import safe_symlink
from aikito.memory_runtime import (
    build_project_memory_batch,
    execute_memory_plan,
    plan_project_memory,
    resolve_project_notes_source,
)


class MemoryRuntimeTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.co = self.root / "checkout"
        self.home.mkdir()
        self.ws.mkdir()
        self.co.mkdir()

        self.ws_mem = self.ws / "memory"
        self.ws_mem.mkdir(parents=True)
        self.proj_dir = self.ws / "projects" / "demo"
        self.proj_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_resolve_project_notes_source_precedence(self) -> None:
        # When projects/demo/memory exists, it has precedence
        proj_mem = self.proj_dir / "memory"
        proj_mem.mkdir()
        notes_path, has_proj_mem = resolve_project_notes_source(self.ws, "demo")
        self.assertTrue(has_proj_mem)
        self.assertEqual(notes_path, proj_mem / "notes")

        # When projects/demo/memory does not exist, legacy fallback is used
        proj_mem.rmdir()
        notes_path, has_proj_mem = resolve_project_notes_source(self.ws, "demo")
        self.assertFalse(has_proj_mem)
        self.assertEqual(notes_path, self.ws_mem / "demo" / "notes")

    def test_build_project_memory_batch(self) -> None:
        (self.ws_mem / "shared").mkdir()
        (self.ws_mem / "notes.md").write_text("# Notes\n", encoding="utf-8")
        proj_notes = self.proj_dir / "memory" / "notes"
        proj_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared", "notes.md"]},
            active_checkouts=[self.co],
        )

        self.assertEqual(batch.project_name, "demo")
        self.assertEqual(len(batch.resources), 3)  # shared, notes.md, notes
        identities = [r.identity for r in batch.resources]
        self.assertIn("workspace_ref:shared", identities)
        self.assertIn("workspace_ref:notes.md", identities)
        self.assertIn("project_notes", identities)

    def test_pure_planning_does_not_mutate_filesystem(self) -> None:
        (self.ws_mem / "shared").mkdir()
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )

        runtime_mem = self.co / ".agents" / "memory"
        self.assertFalse(runtime_mem.exists())

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].action, "CREATE")

        # Filesystem must remain completely unmutated by pure planning
        self.assertFalse(runtime_mem.exists())

    def test_plan_missing_source_causes_conflict(self) -> None:
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["missing.md"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].action, "CONFLICT")
        self.assertIn("Project memory source does not exist", plan.findings[0])

    def test_plan_idempotent_noop_on_exact_symlink(self) -> None:
        (self.ws_mem / "shared").mkdir()
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )

        # Pre-create exact symlink
        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        (runtime_mem / "shared").symlink_to(self.ws_mem / "shared")

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)
        self.assertEqual(plan.noop_count, 1)
        self.assertEqual(plan.operations[0].action, "NOOP")

    def test_plan_unmanaged_target_causes_conflict(self) -> None:
        (self.ws_mem / "shared").mkdir()
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        # Pre-create regular file at target
        (runtime_mem / "shared").write_text("user file", encoding="utf-8")

        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].action, "CONFLICT")
        self.assertIn("regular file", plan.conflicts[0].reason)

    def test_plan_stale_managed_link_unlinks(self) -> None:
        (self.ws_mem / "active.md").write_text("# Active\n", encoding="utf-8")
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["active.md"]},
            active_checkouts=[self.co],
        )

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        # Target for active.md
        (runtime_mem / "active.md").symlink_to(self.ws_mem / "active.md")
        # Stale target previously pointing to canonical <ws>/memory/old.md
        old_canonical = self.ws_mem / "old.md"
        old_canonical.write_text("# Old\n", encoding="utf-8")
        (runtime_mem / "old.md").symlink_to(old_canonical)

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)
        unlink_ops = [op for op in plan.operations if op.action == "UNLINK"]
        self.assertEqual(len(unlink_ops), 1)
        self.assertEqual(unlink_ops[0].target_path, runtime_mem / "old.md")
        self.assertEqual(unlink_ops[0].canonical_path, old_canonical)

    def test_plan_stale_foreign_entry_preserved_as_conflict(self) -> None:
        (self.ws_mem / "active.md").write_text("# Active\n", encoding="utf-8")
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["active.md"]},
            active_checkouts=[self.co],
        )

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        # Stale link pointing to external path
        external_file = self.root / "external.md"
        external_file.write_text("# Ext\n", encoding="utf-8")
        safe_symlink(external_file, runtime_mem / "foreign.md")

        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        conflict_ops = [op for op in plan.operations if op.action == "CONFLICT"]
        self.assertEqual(len(conflict_ops), 1)
        self.assertEqual(conflict_ops[0].target_path, runtime_mem / "foreign.md")

    def test_plan_notes_migration_from_legacy_to_new_canonical(self) -> None:
        # Legacy source
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)
        # New canonical source
        new_notes = self.proj_dir / "memory" / "notes"
        new_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        target = runtime_mem / "notes"
        safe_symlink(legacy_notes, target)

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)
        actions = [op.action for op in plan.operations]
        self.assertEqual(actions, ["UNLINK", "CREATE"])
        self.assertEqual(plan.operations[0].canonical_path, legacy_notes)
        self.assertEqual(plan.operations[1].canonical_path, new_notes)

    def test_execute_memory_plan_creates_and_dry_run_zero_writes(self) -> None:
        (self.ws_mem / "shared").mkdir()
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)

        # Dry-run execution
        dry_res = execute_memory_plan(plan, dry_run=True)
        self.assertTrue(dry_res.success)
        self.assertEqual(dry_res.applied_count, 0)
        self.assertFalse((self.co / ".agents" / "memory").exists())

        # Real execution
        real_res = execute_memory_plan(plan, dry_run=False)
        self.assertTrue(real_res.success)
        self.assertEqual(real_res.applied_count, 1)
        target = self.co / ".agents" / "memory" / "shared"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), (self.ws_mem / "shared").resolve())

        # Repeated execution is pure NOOP
        plan2 = plan_project_memory(batch)
        self.assertEqual(plan2.noop_count, 1)
        res2 = execute_memory_plan(plan2, dry_run=False)
        self.assertTrue(res2.success)
        self.assertEqual(res2.applied_count, 0)
        self.assertEqual(res2.noop_count, 1)
