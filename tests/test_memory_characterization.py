"""Characterization tests for project memory runtime visibility in Aikito 1.46.0.

Locks down baseline behavior, source resolution priority, notes scoping,
deselection cleanup, legacy sync_resource overwrite gaps, multi-checkout, and Project.prepare.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase

from aikito.compat import safe_symlink
from aikito.project_runtime import Project, ProjectPrepareConflictError
from aikito.project_sync import sync_project


class ProjectMemoryCharacterizationTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.co = self.root / "checkout"
        self.home.mkdir()
        self.ws.mkdir()
        self.co.mkdir()

        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".agents" / "skills").mkdir(parents=True)

        (self.ws / "agents.toml").write_text(
            "[agents.codex]\n"
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n',
            encoding="utf-8",
        )
        self.proj_dir = self.ws / "projects" / "demo"
        self.proj_dir.mkdir(parents=True)
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = []\nmemory = []\n',
            encoding="utf-8",
        )
        (self.proj_dir / "AGENTS.md").write_text(
            "# Project instructions\n", encoding="utf-8"
        )

        self.ws_mem = self.ws / "memory"
        self.ws_mem.mkdir(parents=True)

    def tearDown(self) -> None:
        self.td.cleanup()

    def _set_project_memory(self, refs: list[str]) -> None:
        lines = [f'  "{r}",' for r in refs]
        refs_str = "\n".join(lines)
        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = []\nmemory = [\n{refs_str}\n]\n',
            encoding="utf-8",
        )

    def test_workspace_memory_references_link_correctly(self) -> None:
        # Create canonical sources
        (self.ws_mem / "shared").mkdir()
        (self.ws_mem / "shared" / "doc.md").write_text(
            "# Shared Doc\n", encoding="utf-8"
        )
        (self.ws_mem / "notes.md").write_text(
            "# Single File Memory\n", encoding="utf-8"
        )

        self._set_project_memory(["shared", "notes.md"])

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        shared_target = self.co / ".agents" / "memory" / "shared"
        notes_target = self.co / ".agents" / "memory" / "notes.md"

        self.assertTrue(shared_target.is_symlink())
        self.assertEqual(shared_target.resolve(), (self.ws_mem / "shared").resolve())
        self.assertTrue(notes_target.is_symlink())
        self.assertEqual(notes_target.resolve(), (self.ws_mem / "notes.md").resolve())

    def test_selected_memory_source_missing_blocks_sync(self) -> None:
        self._set_project_memory(["nonexistent.md"])
        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)

        target = self.co / ".agents" / "memory" / "nonexistent.md"
        self.assertFalse(target.exists())
        self.assertFalse(target.is_symlink())

    def test_project_memory_root_precedence_over_legacy_fallback(self) -> None:
        # If <workspace>/projects/<project>/memory exists, legacy fallback is disabled
        proj_mem = self.proj_dir / "memory"
        proj_mem.mkdir()
        # notes/ is absent in proj_mem, but present in legacy root
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)
        (legacy_notes / "legacy.md").write_text("# Legacy\n", encoding="utf-8")

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        # Because proj_mem exists, legacy fallback MUST NOT be used
        target = self.co / ".agents" / "memory" / "notes"
        self.assertFalse(target.exists())
        self.assertFalse(target.is_symlink())

    def test_legacy_fallback_used_when_project_memory_does_not_exist(self) -> None:
        # <workspace>/projects/<project>/memory does NOT exist
        legacy_notes = self.ws_mem / "demo" / "notes"
        legacy_notes.mkdir(parents=True)
        (legacy_notes / "legacy.md").write_text("# Legacy\n", encoding="utf-8")

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        target = self.co / ".agents" / "memory" / "notes"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), legacy_notes.resolve())

    def test_canonical_project_notes_used_when_present(self) -> None:
        proj_notes = self.proj_dir / "memory" / "notes"
        proj_notes.mkdir(parents=True)
        (proj_notes / "note1.md").write_text("# Note 1\n", encoding="utf-8")

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        target = self.co / ".agents" / "memory" / "notes"
        self.assertTrue(target.is_symlink())
        self.assertEqual(target.resolve(), proj_notes.resolve())

    def test_project_memory_ignores_non_notes_files(self) -> None:
        proj_mem = self.proj_dir / "memory"
        proj_mem.mkdir(parents=True)
        (proj_mem / "notes").mkdir()
        (proj_mem / "README.md").write_text("# Ignored\n", encoding="utf-8")
        (proj_mem / "extra").mkdir()

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        runtime_mem = self.co / ".agents" / "memory"
        self.assertTrue((runtime_mem / "notes").is_symlink())
        self.assertFalse((runtime_mem / "README.md").exists())
        self.assertFalse((runtime_mem / "extra").exists())

    def test_deselected_workspace_memory_exact_link_is_cleaned_up(self) -> None:
        (self.ws_mem / "shared").mkdir()
        (self.ws_mem / "old.md").write_text("# Old\n", encoding="utf-8")

        # First sync with both selected
        self._set_project_memory(["shared", "old.md"])
        self.assertTrue(sync_project(self.ws, self.home, "demo"))

        old_target = self.co / ".agents" / "memory" / "old.md"
        shared_target = self.co / ".agents" / "memory" / "shared"
        self.assertTrue(old_target.is_symlink())
        self.assertTrue(shared_target.is_symlink())

        # Deselect old.md
        self._set_project_memory(["shared"])
        self.assertTrue(sync_project(self.ws, self.home, "demo"))

        # Exact owned link for old.md must be unlinked
        self.assertFalse(old_target.exists())
        self.assertFalse(old_target.is_symlink())
        # shared remains intact
        self.assertTrue(shared_target.is_symlink())

    def test_deselected_memory_pointing_to_other_memory_is_preserved(self) -> None:
        (self.ws_mem / "shared").mkdir()
        (self.ws_mem / "other.md").write_text("# Other\n", encoding="utf-8")

        # runtime/old.md points to canonical/other.md
        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        old_target = runtime_mem / "old.md"
        safe_symlink(self.ws_mem / "other.md", old_target)

        # Deselect old.md (only shared is configured)
        self._set_project_memory(["shared"])
        # Sync halts with conflict because old.md is an unmanaged / foreign symlink
        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)

        # The link must be preserved
        self.assertTrue(old_target.is_symlink())
        self.assertEqual(old_target.resolve(), (self.ws_mem / "other.md").resolve())

    def test_selected_memory_target_regular_file_causes_conflict(
        self,
    ) -> None:
        """Verify INV-MEM-07: pre-existing regular file at target raises CONFLICT and is preserved."""
        (self.ws_mem / "shared").mkdir()
        self._set_project_memory(["shared"])

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        conflict_file = runtime_mem / "shared"
        conflict_file.write_text("# Local file\n", encoding="utf-8")

        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)
        self.assertTrue(conflict_file.is_file())
        self.assertFalse(conflict_file.is_symlink())
        self.assertEqual(conflict_file.read_text(encoding="utf-8"), "# Local file\n")

    def test_selected_memory_target_external_symlink_causes_conflict(
        self,
    ) -> None:
        """Verify INV-MEM-07: pre-existing external symlink at target raises CONFLICT and is preserved."""
        (self.ws_mem / "shared").mkdir()
        self._set_project_memory(["shared"])

        external_path = self.root / "external_dir"
        external_path.mkdir()

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        conflict_link = runtime_mem / "shared"
        safe_symlink(external_path, conflict_link)

        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)
        self.assertTrue(conflict_link.is_symlink())
        self.assertEqual(conflict_link.resolve(), external_path.resolve())

    def test_project_prepare_raises_on_memory_conflict(self) -> None:
        """Verify INV-MEM-07, INV-MEM-12: prepare() raises ProjectPrepareConflictError on conflict."""
        (self.ws_mem / "shared").mkdir()
        self._set_project_memory(["shared"])

        runtime_mem = self.co / ".agents" / "memory"
        runtime_mem.mkdir(parents=True)
        conflict_file = runtime_mem / "shared"
        conflict_file.write_text("# Blocking file\n", encoding="utf-8")

        proj = Project.load("demo", self.ws, self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            proj.prepare("codex")
        self.assertEqual(ctx.exception.project_name, "demo")
        # Ensure file was not overwritten
        self.assertTrue(conflict_file.is_file())

    def test_project_prepare_raises_on_missing_memory_source(self) -> None:
        self._set_project_memory(["missing_source.md"])

        proj = Project.load("demo", self.ws, self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            proj.prepare("codex")

        self.assertEqual(ctx.exception.project_name, "demo")

    def test_multi_checkout_and_offline_checkout(self) -> None:
        co2 = self.root / "checkout2"
        co2.mkdir()
        offline_co = self.root / "offline_checkout"  # Does not exist

        (self.proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}", "{co2.as_posix()}", "{offline_co.as_posix()}"]\n'
            'skills = []\nmemory = ["shared"]\n',
            encoding="utf-8",
        )
        (self.ws_mem / "shared").mkdir()

        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        # Both active checkouts receive symlink
        self.assertTrue((self.co / ".agents" / "memory" / "shared").is_symlink())
        self.assertTrue((co2 / ".agents" / "memory" / "shared").is_symlink())
        # Offline checkout was not touched
        self.assertFalse(offline_co.exists())

    def test_repeated_sync_idempotent_noop(self) -> None:
        (self.ws_mem / "shared").mkdir()
        self._set_project_memory(["shared"])

        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        target = self.co / ".agents" / "memory" / "shared"
        st1 = target.lstat()

        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        st2 = target.lstat()

        self.assertEqual(st1.st_mtime_ns, st2.st_mtime_ns)
        self.assertEqual(target.resolve(), (self.ws_mem / "shared").resolve())
