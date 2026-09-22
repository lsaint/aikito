"""Characterization tests for instruction handling across global and project scopes.

Locks down baseline behavior, target states, empty canonical cleanup,
and preservation of unmanaged regular files and external symlinks.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from unittest import TestCase
from unittest.mock import patch

from aikito.cli import sync_global_resources
from aikito.compat import safe_symlink
from aikito.project_sync import (
    sync_project,
)


class GlobalInstructionCharacterizationTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.home.mkdir()
        self.ws.mkdir()

        # Isolate agents directory and home from real environment
        self.patcher_agents = patch(
            "aikito.cli.get_agents_dir", return_value=self.home / ".agents"
        )
        self.patcher_home = patch("pathlib.Path.home", return_value=self.home)
        self.patcher_agents.start()
        self.patcher_home.start()

        # Agent marker directories in home
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".agents" / "skills").mkdir(parents=True)

        # Workspace configuration
        (self.ws / "skills.toml").write_text("", encoding="utf-8")
        (self.ws / "agents.toml").write_text(
            "[agents.codex]\n"
            'instruction_path = ".codex/AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            "[agents.claude-code]\n"
            'instruction_path = ".claude/CLAUDE.md"\n'
            'skills_path = ".claude/skills"\n',
            encoding="utf-8",
        )
        (self.ws / "global").mkdir()
        self.global_agents_md = self.ws / "global" / "AGENTS.md"
        self.global_agents_md.write_text("# Global Instructions\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.patcher_home.stop()
        self.patcher_agents.stop()
        self.td.cleanup()

    def test_missing_global_instruction_source_fails_preflight(self) -> None:
        self.global_agents_md.unlink()
        res = sync_global_resources(self.ws, self.home)
        self.assertFalse(res.success)
        self.assertIn("Global instruction file not found", res.error_message or "")

    def test_empty_global_instruction_source_is_still_linked(self) -> None:
        self.global_agents_md.write_text("", encoding="utf-8")
        res = sync_global_resources(self.ws, self.home)
        self.assertTrue(res.success)
        codex_target = self.home / ".codex" / "AGENTS.md"
        self.assertTrue(codex_target.is_symlink())
        self.assertEqual(
            codex_target.resolve(strict=False), self.global_agents_md.resolve()
        )

    def test_target_missing_creates_symlink(self) -> None:
        res = sync_global_resources(self.ws, self.home)
        self.assertTrue(res.success)
        codex_target = self.home / ".codex" / "AGENTS.md"
        self.assertTrue(codex_target.is_symlink())
        self.assertEqual(
            codex_target.resolve(strict=False), self.global_agents_md.resolve()
        )

    def test_target_correct_symlink_noop(self) -> None:
        # First sync creates the symlink
        sync_global_resources(self.ws, self.home)
        codex_target = self.home / ".codex" / "AGENTS.md"
        st1 = codex_target.lstat()

        # Second sync should be a NOOP
        res = sync_global_resources(self.ws, self.home)
        self.assertTrue(res.success)
        st2 = codex_target.lstat()
        self.assertEqual(st1.st_mtime_ns, st2.st_mtime_ns)

    def test_target_regular_file_causes_conflict(self) -> None:
        codex_dir = self.home / ".codex"
        codex_target = codex_dir / "AGENTS.md"
        codex_target.write_text("user owned file\n", encoding="utf-8")

        res = sync_global_resources(self.ws, self.home)
        self.assertFalse(res.success)
        # Regular file must not be overwritten or unlinked
        self.assertTrue(codex_target.is_file())
        self.assertFalse(codex_target.is_symlink())
        self.assertEqual(codex_target.read_text(encoding="utf-8"), "user owned file\n")

    def test_same_object_disposition(self) -> None:
        # If target path points to the same object as canonical source
        (self.ws / "agents.toml").write_text(
            "[agents.self_agent]\n"
            f'instruction_path = "{self.global_agents_md.relative_to(self.home) if self.global_agents_md.is_relative_to(self.home) else self.global_agents_md.name}"\n',
            encoding="utf-8",
        )
        res = sync_global_resources(self.ws, self.home)
        self.assertTrue(res.success)

    def test_legacy_grok_cleaned_up_if_exact_owned(self) -> None:
        grok_dir = self.home / ".grok"
        grok_dir.mkdir(parents=True)
        legacy_grok = grok_dir / "AGENTS.md"
        safe_symlink(self.global_agents_md.resolve(), legacy_grok)
        self.assertTrue(legacy_grok.is_symlink())

        (self.ws / "agents.toml").write_text(
            "[agents.grok]\n",
            encoding="utf-8",
        )
        res = sync_global_resources(self.ws, self.home)
        self.assertTrue(res.success)
        self.assertFalse(legacy_grok.exists())


class ProjectInstructionCharacterizationTests(TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.co = self.root / "checkout"
        self.home.mkdir()
        self.ws.mkdir()
        self.co.mkdir()

        # Create agent marker directories in home
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)

        (self.ws / "agents.toml").write_text(
            "[agents.codex]\n"
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            "[agents.claude-code]\n"
            'project_instruction_path = ".claude/CLAUDE.md"\n'
            'skills_path = ".claude/skills"\n',
            encoding="utf-8",
        )
        proj_dir = self.ws / "projects" / "demo"
        proj_dir.mkdir(parents=True)
        (proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}"]\nskills = []\n',
            encoding="utf-8",
        )
        self.proj_agents_md = proj_dir / "AGENTS.md"
        self.proj_agents_md.write_text("# Project instructions\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_non_empty_canonical_creates_symlinks(self) -> None:
        success = sync_project(self.ws, self.home, "demo")
        self.assertTrue(success)

        codex_link = self.co / "AGENTS.md"
        claude_link = self.co / ".claude" / "CLAUDE.md"
        self.assertTrue(codex_link.is_symlink())
        self.assertTrue(claude_link.is_symlink())
        self.assertEqual(
            codex_link.resolve(strict=False), self.proj_agents_md.resolve()
        )
        self.assertEqual(
            claude_link.resolve(strict=False), self.proj_agents_md.resolve()
        )

    def test_non_empty_canonical_regular_file_conflict(self) -> None:
        # Pre-existing regular file in checkout
        existing_file = self.co / "AGENTS.md"
        existing_file.write_text("# Custom local instructions\n", encoding="utf-8")

        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)
        # Regular file must not be overwritten or converted to symlink
        self.assertTrue(existing_file.is_file())
        self.assertFalse(existing_file.is_symlink())
        self.assertEqual(
            existing_file.read_text(encoding="utf-8"), "# Custom local instructions\n"
        )

    def test_non_empty_canonical_external_symlink_conflict(self) -> None:
        # External symlink pointing elsewhere
        external_target = self.root / "external.md"
        external_target.write_text("# External\n", encoding="utf-8")
        link = self.co / "AGENTS.md"
        safe_symlink(external_target, link)

        success = sync_project(self.ws, self.home, "demo")
        self.assertFalse(success)
        # Link must remain pointing to external target
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(strict=False), external_target.resolve())

    def test_empty_canonical_unlinks_exact_owned_symlink(self) -> None:
        # First sync with non-empty instructions creates the link
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        codex_link = self.co / "AGENTS.md"
        self.assertTrue(codex_link.is_symlink())

        # Now empty the canonical instruction file
        self.proj_agents_md.write_text("", encoding="utf-8")
        self.assertTrue(sync_project(self.ws, self.home, "demo"))

        # Exact owned symlink must be unlinked
        self.assertFalse(codex_link.exists())
        self.assertFalse(codex_link.is_symlink())

    def test_empty_canonical_preserves_regular_file(self) -> None:
        self.proj_agents_md.write_text("", encoding="utf-8")
        local_file = self.co / "AGENTS.md"
        local_file.write_text("# Project owned file\n", encoding="utf-8")

        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        # Regular file must be strictly preserved
        self.assertTrue(local_file.is_file())
        self.assertFalse(local_file.is_symlink())
        self.assertEqual(
            local_file.read_text(encoding="utf-8"), "# Project owned file\n"
        )

    def test_empty_canonical_preserves_foreign_symlink(self) -> None:
        self.proj_agents_md.write_text("", encoding="utf-8")
        external_target = self.root / "other.md"
        external_target.write_text("# Other\n", encoding="utf-8")
        link = self.co / "AGENTS.md"
        safe_symlink(external_target, link)

        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        # Foreign symlink must NOT be unlinked
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.resolve(strict=False), external_target.resolve())

    def test_empty_to_non_empty_transition(self) -> None:
        # Start empty
        self.proj_agents_md.write_text("", encoding="utf-8")
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        codex_link = self.co / "AGENTS.md"
        self.assertFalse(codex_link.exists())

        # Transition to non-empty
        self.proj_agents_md.write_text("# Active instructions\n", encoding="utf-8")
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        self.assertTrue(codex_link.is_symlink())

        # Repeated sync is idempotent
        st1 = codex_link.lstat()
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        st2 = codex_link.lstat()
        self.assertEqual(st1.st_mtime_ns, st2.st_mtime_ns)

    def test_non_empty_to_empty_transition(self) -> None:
        # Start non-empty
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        codex_link = self.co / "AGENTS.md"
        self.assertTrue(codex_link.is_symlink())

        # Transition to empty
        self.proj_agents_md.write_text("", encoding="utf-8")
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        self.assertFalse(codex_link.exists())

        # Repeated sync remains clean
        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        self.assertFalse(codex_link.exists())

    def test_multi_checkout_instructions(self) -> None:
        co2 = self.root / "checkout2"
        co2.mkdir()
        proj_dir = self.ws / "projects" / "demo"
        (proj_dir / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.co.as_posix()}", "{co2.as_posix()}"]\nskills = []\n',
            encoding="utf-8",
        )

        self.assertTrue(sync_project(self.ws, self.home, "demo"))
        link1 = self.co / "AGENTS.md"
        link2 = co2 / "AGENTS.md"
        self.assertTrue(link1.is_symlink())
        self.assertTrue(link2.is_symlink())
        self.assertEqual(link1.resolve(strict=False), self.proj_agents_md.resolve())
        self.assertEqual(link2.resolve(strict=False), self.proj_agents_md.resolve())
