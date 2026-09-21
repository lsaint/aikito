"""Unit tests for memory_runtime module.

Tests MemoryResource, MemoryBatch, MemoryPlan pure planning, exact ownership,
legacy notes migration, offline checkout, and execute_memory_plan.
"""

from __future__ import annotations

import shutil
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

    def test_unsafe_memory_reference_traversal_causes_conflict(self) -> None:
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["../escape"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].rule_id, "INV-MEM-02")
        self.assertIn("traversal", plan.conflicts[0].reason)

    def test_unsafe_memory_reference_absolute_causes_conflict(self) -> None:
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["/absolute/path"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].rule_id, "INV-MEM-02")
        self.assertIn("absolute", plan.conflicts[0].reason)

    def test_checkout_agents_symlink_causes_conflict(self) -> None:
        (self.ws_mem / "shared.md").write_text("# Shared\n", encoding="utf-8")
        external_dir = self.root / "external"
        external_dir.mkdir()
        safe_symlink(external_dir, self.co / ".agents")

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared.md"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].rule_id, "INV-MEM-02")
        self.assertIn("cannot be a symlink", plan.conflicts[0].reason)

    def test_project_notes_precedence_over_workspace_ref_notes(self) -> None:
        # Both workspace/memory/notes and projects/demo/memory/notes exist
        (self.ws_mem / "notes").mkdir()
        proj_notes = self.proj_dir / "memory" / "notes"
        proj_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["notes"]},
            active_checkouts=[self.co],
        )
        # Project notes takes precedence: only one resource with relative_target == Path("notes")
        notes_resources = [
            r for r in batch.resources if r.relative_target == Path("notes")
        ]
        self.assertEqual(len(notes_resources), 1)
        self.assertEqual(notes_resources[0].source_kind, "project_notes")
        self.assertEqual(notes_resources[0].canonical_source, proj_notes)

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)
        create_ops = [
            op
            for op in plan.operations
            if op.target_path == self.co / ".agents" / "memory" / "notes"
        ]
        self.assertEqual(len(create_ops), 1)
        self.assertEqual(create_ops[0].canonical_path, proj_notes)

    def test_hierarchical_target_overlap_causes_conflict(self) -> None:
        (self.ws_mem / "foo").mkdir()
        (self.ws_mem / "foo" / "bar").mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["foo", "foo/bar"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(plan.conflicts[0].rule_id, "INV-MEM-02")
        self.assertIn("collision", plan.conflicts[0].reason)

    def test_stale_plan_source_priority_change_invalidates(self) -> None:
        # Plan time: projects/demo/memory does NOT exist
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Before apply: projects/demo/memory/notes is created!
        new_notes = self.proj_dir / "memory" / "notes"
        new_notes.mkdir(parents=True)

        # Execution of old plan must detect source priority change and halt
        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn("source priority changed", res.error_message or "")
        # Zero filesystem writes
        self.assertFalse((self.co / ".agents" / "memory").exists())

    def test_stale_plan_canonical_missing_before_apply_invalidates(self) -> None:
        (self.ws_mem / "temp.md").write_text("# Temp\n", encoding="utf-8")
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["temp.md"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Before apply: canonical file is deleted
        (self.ws_mem / "temp.md").unlink()

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn("canonical source missing", res.error_message or "")
        self.assertFalse((self.co / ".agents" / "memory").exists())

    def test_notes_migration_atomic_does_not_destroy_old_link_on_failure(self) -> None:
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)
        new_notes = self.proj_dir / "memory" / "notes"
        new_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        # Pre-create legacy link
        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        target = runtime_mem / "notes"
        safe_symlink(legacy_notes, target)

        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Before apply: new_notes directory is removed!
        new_notes.rmdir()

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        # Existing legacy link MUST still be present and intact!
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), legacy_notes.resolve())

    def test_nested_runtime_parent_symlink_toctou_prevented(self) -> None:
        (self.ws_mem / "foo").mkdir(parents=True)
        (self.ws_mem / "foo" / "bar").write_text("# Nested\n", encoding="utf-8")
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["foo/bar"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # TOCTOU: external actor creates .agents/memory/foo -> /external
        external_dir = Path(tempfile.mkdtemp())
        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        (runtime_mem / "foo").symlink_to(external_dir)

        # Execution must detect intermediate directory symlink and abort
        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn("target boundary violation", res.error_message or "")
        self.assertFalse((external_dir / "bar").exists())
        shutil.rmtree(external_dir)

    def test_canonical_source_type_change_toctou_prevented(self) -> None:
        # Scene A: Project notes changed from dir to regular file
        proj_notes = self.proj_dir / "memory" / "notes"
        proj_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # TOCTOU: replace directory with regular file
        proj_notes.rmdir()
        proj_notes.write_text("regular file\n", encoding="utf-8")

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertTrue(
            "canonical source type changed" in (res.error_message or "")
            or "source priority changed" in (res.error_message or "")
            or "must be a directory" in (res.error_message or "")
            or "safety violation" in (res.error_message or "")
        )
        proj_notes.unlink()

    def test_canonical_source_type_change_workspace_memory_toctou(self) -> None:
        (self.ws_mem / "shared").mkdir()
        batch_b = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )
        plan_b = plan_project_memory(batch_b)
        self.assertTrue(plan_b.can_apply)

        # TOCTOU: replace shared dir with symlink to /external
        external_dir = Path(tempfile.mkdtemp())
        (self.ws_mem / "shared").rmdir()
        (self.ws_mem / "shared").symlink_to(external_dir)

        res_b = execute_memory_plan(plan_b)
        self.assertFalse(res_b.success)
        self.assertEqual(res_b.applied_count, 0)
        self.assertTrue(
            "canonical source type changed" in (res_b.error_message or "")
            or "canonical source safety violation" in (res_b.error_message or "")
        )
        (self.ws_mem / "shared").unlink()
        shutil.rmtree(external_dir)

    def test_legacy_notes_migration_foreign_tampered_symlink_not_overwritten(
        self,
    ) -> None:
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)
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

        # External process alters .agents/memory/notes to point to /foreign
        foreign_dir = Path(tempfile.mkdtemp())
        target.unlink()
        target.symlink_to(foreign_dir)

        # Execute must abort: ownership evidence broken, foreign symlink preserved
        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn("ownership evidence broken", res.error_message or "")
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), foreign_dir.resolve())
        shutil.rmtree(foreign_dir)

    def test_inv_mem_09_notes_created_when_proj_mem_already_existed(self) -> None:
        # At plan time: projects/demo/memory exists, but notes/ does NOT exist
        (self.proj_dir / "memory").mkdir(parents=True)
        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Before apply: projects/demo/memory/notes is created
        (self.proj_dir / "memory" / "notes").mkdir(parents=True)

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn("source priority changed", res.error_message or "")

    def test_inv_mem_09_agent_toml_mutation_invalidates_plan(self) -> None:
        agent_toml = self.proj_dir / "agent.toml"
        agent_toml.write_text('memory = ["a.md"]\n', encoding="utf-8")
        (self.ws_mem / "a.md").write_text("# A\n", encoding="utf-8")
        (self.ws_mem / "b.md").write_text("# B\n", encoding="utf-8")

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["a.md"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Mutate agent.toml memory before apply
        agent_toml.write_text('memory = ["b.md"]\n', encoding="utf-8")

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        self.assertIn(
            "agent.toml memory configuration changed", res.error_message or ""
        )

    def test_project_notes_external_symlink_causes_conflict(self) -> None:
        (self.proj_dir / "memory").mkdir(parents=True)
        external_dir = Path(tempfile.mkdtemp())
        (self.proj_dir / "memory" / "notes").symlink_to(external_dir)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(
            any("escapes project memory boundary" in f for f in plan.findings)
        )
        shutil.rmtree(external_dir)

    def test_project_memory_root_external_symlink_causes_conflict(self) -> None:
        external_dir = Path(tempfile.mkdtemp())
        (self.proj_dir / "memory").symlink_to(external_dir)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(any("escapes project boundary" in f for f in plan.findings))
        (self.proj_dir / "memory").unlink()
        shutil.rmtree(external_dir)

    def test_project_memory_root_regular_file_causes_conflict(self) -> None:
        (self.proj_dir / "memory").write_text("not a directory\n", encoding="utf-8")

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(any("must be a directory" in f for f in plan.findings))

    def test_workspace_memory_root_external_symlink_causes_conflict(self) -> None:
        external_dir = Path(tempfile.mkdtemp())
        self.ws_mem.rmdir()
        self.ws_mem.symlink_to(external_dir)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": ["shared"]},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(any("escapes workspace boundary" in f for f in plan.findings))
        self.ws_mem.unlink()
        self.ws_mem.mkdir()
        shutil.rmtree(external_dir)

    def test_broken_project_memory_root_with_legacy_notes_causes_conflict_no_fallback(
        self,
    ) -> None:
        # Broken project memory root
        missing_external = Path("/tmp/nonexistent_external_dir_aikito_test_123")
        (self.proj_dir / "memory").symlink_to(missing_external)

        # Legacy notes exists
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        self.assertTrue(any("broken symlink" in f for f in plan.findings))
        # MUST NOT fallback to legacy notes!
        self.assertFalse(
            any(
                op.action == "CREATE" and op.canonical_path == legacy_notes
                for op in plan.operations
            )
        )
        (self.proj_dir / "memory").unlink()

    def test_broken_project_notes_preserves_runtime_link_and_conflicts(self) -> None:
        (self.proj_dir / "memory").mkdir(parents=True)
        missing_external = Path("/tmp/nonexistent_notes_dir_aikito_test_123")
        notes_target = self.proj_dir / "memory" / "notes"
        notes_target.symlink_to(missing_external)

        # Pre-create runtime link
        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        (runtime_mem / "notes").symlink_to(notes_target)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertFalse(plan.can_apply)
        self.assertTrue(plan.has_conflicts)
        # Must NOT treat as absent or schedule UNLINK
        self.assertFalse(any(op.action == "UNLINK" for op in plan.operations))
        notes_target.unlink()

    def test_project_notes_mutated_to_broken_symlink_after_plan_causes_stale_plan(
        self,
    ) -> None:
        notes_target = self.proj_dir / "memory" / "notes"
        notes_target.mkdir(parents=True)

        batch = build_project_memory_batch(
            self.ws,
            "demo",
            {"memory": []},
            active_checkouts=[self.co],
        )
        plan = plan_project_memory(batch)
        self.assertTrue(plan.can_apply)

        # Mutate to broken symlink before apply
        notes_target.rmdir()
        missing_external = Path("/tmp/nonexistent_notes_mutated_test_123")
        notes_target.symlink_to(missing_external)

        res = execute_memory_plan(plan)
        self.assertFalse(res.success)
        self.assertEqual(res.applied_count, 0)
        notes_target.unlink()
