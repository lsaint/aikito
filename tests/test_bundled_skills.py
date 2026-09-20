import io
import shutil
import tempfile
import threading
import unittest
from pathlib import Path

from unittest.mock import patch

from aikito.bundled_skills import (
    outdated_bundled_skills,
    print_bundled_skill_notice,
    refresh_bundled_skills,
)
from aikito.skill_state import SkillWriterLock, get_skill_state_dir
from aikito.templating import BUNDLED_SKILL_NAMES, bundled_skill_path


class BundledSkillRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.home = self.root / "home"
        self.skills = self.workspace / "skills"
        self.skills.mkdir(parents=True)
        self.home.mkdir()
        for name in BUNDLED_SKILL_NAMES:
            shutil.copytree(bundled_skill_path(name), self.skills / name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_identical_snapshots_need_no_refresh(self) -> None:
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        self.assertEqual(
            refresh_bundled_skills(self.workspace, self.home),
            (),
        )
        self.assertFalse((self.home / ".aikito").exists())

    def test_notice_reports_only_selected_divergent_skill(self) -> None:
        (self.skills / "aikito" / "SKILL.md").write_text(
            "customized\n", encoding="utf-8"
        )
        (self.skills / "durable-memory" / "SKILL.md").write_text(
            "customized\n", encoding="utf-8"
        )
        output = io.StringIO()

        outdated = print_bundled_skill_notice(
            self.workspace,
            names=("aikito",),
            output=output,
        )

        self.assertEqual(outdated, ("aikito",))
        self.assertTrue(output.getvalue().startswith("\n[NOTICE]"))
        self.assertIn("aikito", output.getvalue())
        self.assertNotIn("durable-memory", output.getvalue())

    def test_dry_run_reports_without_writing_or_backing_up(self) -> None:
        skill_file = self.skills / "aikito" / "SKILL.md"
        skill_file.write_text("customized\n", encoding="utf-8")

        refreshed = refresh_bundled_skills(
            self.workspace,
            self.home,
            dry_run=True,
        )

        self.assertEqual(refreshed, ("aikito",))
        self.assertEqual(skill_file.read_text(encoding="utf-8"), "customized\n")
        self.assertFalse((self.home / ".aikito").exists())

    def test_refresh_backs_up_and_replaces_complete_skill_trees(self) -> None:
        for name in BUNDLED_SKILL_NAMES:
            skill_dir = self.skills / name
            (skill_dir / "SKILL.md").write_text("customized\n", encoding="utf-8")
            (skill_dir / "local.md").write_text("local\n", encoding="utf-8")

        refreshed = refresh_bundled_skills(self.workspace, self.home)

        self.assertEqual(refreshed, BUNDLED_SKILL_NAMES)
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        for name in BUNDLED_SKILL_NAMES:
            self.assertFalse((self.skills / name / "local.md").exists())
            self.assertEqual(
                (self.skills / name / "SKILL.md").read_bytes(),
                (bundled_skill_path(name) / "SKILL.md").read_bytes(),
            )

        backup_roots = list(
            (self.home / ".aikito" / "backups").glob("bundled-skills_*")
        )
        self.assertEqual(len(backup_roots), 1)
        for name in BUNDLED_SKILL_NAMES:
            self.assertEqual(
                (backup_roots[0] / name / "SKILL.md").read_text(encoding="utf-8"),
                "customized\n",
            )
            self.assertTrue((backup_roots[0] / name / "local.md").is_file())

    def test_refresh_restores_missing_bundled_skill(self) -> None:
        shutil.rmtree(self.skills / "durable-memory")

        refreshed = refresh_bundled_skills(self.workspace, self.home)

        self.assertEqual(refreshed, ("durable-memory",))
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        self.assertFalse((self.home / ".aikito").exists())


class BundledSkillWriterLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.workspace = self.root / "workspace"
        self.home = self.root / "home"
        self.skills = self.workspace / "skills"
        self.skills.mkdir(parents=True)
        self.home.mkdir()
        for name in BUNDLED_SKILL_NAMES:
            shutil.copytree(bundled_skill_path(name), self.skills / name)
        (self.workspace / "config.toml").write_text("", encoding="utf-8")
        (self.workspace / "skills.toml").write_text("", encoding="utf-8")
        (self.workspace / "agents.toml").write_text(
            '[agents.codex]\nskills_path = ".agents/skills"\n',
            encoding="utf-8",
        )
        (self.workspace / "subagents.toml").write_text("", encoding="utf-8")
        for marker in ("mcps", "memory", "projects", "skills", "global", "subagents"):
            (self.workspace / marker).mkdir(parents=True, exist_ok=True)
        (self.workspace / "global" / "AGENTS.md").write_text(
            "# Global\n", encoding="utf-8"
        )
        (self.home / ".agents" / "skills").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_cli_sync_global_holds_writer_lock_when_applying(self) -> None:
        from aikito.cli import sync_global_resources

        lock_acquired = False
        orig_acquire = SkillWriterLock.acquire

        def tracking_acquire(lock_self: SkillWriterLock) -> None:
            nonlocal lock_acquired
            lock_acquired = True
            orig_acquire(lock_self)

        with (
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
            patch.object(
                SkillWriterLock, "acquire", side_effect=tracking_acquire, autospec=True
            ),
        ):
            ok = sync_global_resources(self.workspace, self.home, dry_run=False)
            self.assertTrue(ok)
            self.assertTrue(lock_acquired)

    def test_cli_sync_global_dry_run_does_not_acquire_or_create_lock(self) -> None:
        from aikito.cli import sync_global_resources

        lock_acquired = False
        orig_acquire = SkillWriterLock.acquire

        def tracking_acquire(lock_self: SkillWriterLock) -> None:
            nonlocal lock_acquired
            lock_acquired = True
            orig_acquire(lock_self)

        with (
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
            patch.object(
                SkillWriterLock, "acquire", side_effect=tracking_acquire, autospec=True
            ),
        ):
            ok = sync_global_resources(self.workspace, self.home, dry_run=True)
            self.assertTrue(ok)
            self.assertFalse(lock_acquired)

        lock_file = get_skill_state_dir(self.home) / "writer.lock"
        self.assertFalse(lock_file.exists())

    def test_cli_sync_global_executes_apply_under_writer_lock(self) -> None:
        from aikito.cli import sync_global_resources
        import aikito.cli

        lock_depth_during_apply = -1
        orig_execute = aikito.cli.execute_global_skills

        def tracking_execute(*args, **kwargs):
            nonlocal lock_depth_during_apply
            lock_depth_during_apply = SkillWriterLock._lock_depth
            return orig_execute(*args, **kwargs)

        with (
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
            patch("aikito.cli.execute_global_skills", side_effect=tracking_execute),
        ):
            ok = sync_global_resources(self.workspace, self.home, dry_run=False)
            self.assertTrue(ok)
            self.assertGreaterEqual(lock_depth_during_apply, 1)

    def test_cli_sync_global_aborts_if_refreshed_canonical_missing(self) -> None:
        from aikito.cli import sync_global_resources

        with (
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
            patch("aikito.cli.outdated_bundled_skills", return_value=["missing-skill"]),
            patch("aikito.cli.refresh_bundled_skills", return_value=["missing-skill"]),
        ):
            res = sync_global_resources(self.workspace, self.home, dry_run=False)
            self.assertFalse(res)
            self.assertIn("missing-skill", res.error_message or "")

    def test_cli_sync_global_result_segmentation_on_instruction_failure(self) -> None:
        from aikito.cli import sync_global_resources

        (self.workspace / "skills.toml").write_text(
            'skills = ["aikito"]\n', encoding="utf-8"
        )
        (self.workspace / "agents.toml").write_text(
            '[agents.claude]\nskills_path = ".agents/skills"\ninstruction_path = ".claude/AGENTS.md"\n',
            encoding="utf-8",
        )

        # Mock sync_global_entry for instructions to fail
        with (
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
            patch("aikito.cli.sync_global_entry", return_value=False),
        ):
            res = sync_global_resources(self.workspace, self.home, dry_run=False)
            self.assertFalse(res.success)
            self.assertFalse(bool(res))
            self.assertIsNotNone(res.skill_result)
            self.assertTrue(res.skill_result.success)
            self.assertFalse(res.instruction_success)
            # Skills runtime links were nonetheless successfully applied
            container = self.home / ".agents" / "skills"
            self.assertTrue(container.is_dir())
            self.assertTrue((container / "aikito").is_symlink())


    def test_init_existing_workspace_holds_writer_lock(self) -> None:
        from aikito.init import init_workspace

        for marker in ("agents.toml", "skills.toml", "subagents.toml"):
            (self.workspace / marker).write_text("", encoding="utf-8")
        for marker in ("mcps", "memory", "projects", "skills", "global", "subagents"):
            (self.workspace / marker).mkdir(parents=True, exist_ok=True)

        lock_acquired = False
        orig_acquire = SkillWriterLock.acquire

        def tracking_acquire(lock_self: SkillWriterLock) -> None:
            nonlocal lock_acquired
            lock_acquired = True
            orig_acquire(lock_self)

        with patch.object(
            SkillWriterLock, "acquire", side_effect=tracking_acquire, autospec=True
        ):
            ok = init_workspace(self.workspace, self.home)
            self.assertTrue(ok)
            self.assertTrue(lock_acquired)

    def test_reentrant_lock_does_not_deadlock_on_nested_calls(self) -> None:
        with SkillWriterLock(self.home):
            self.assertEqual(SkillWriterLock._lock_depth, 1)
            with SkillWriterLock(self.home):
                self.assertEqual(SkillWriterLock._lock_depth, 2)
            self.assertEqual(SkillWriterLock._lock_depth, 1)
        self.assertEqual(SkillWriterLock._lock_depth, 0)

    def test_writer_lock_serializes_threads(self) -> None:
        first_entered = threading.Event()
        release_first = threading.Event()
        second_entered = threading.Event()

        def first_writer() -> None:
            with SkillWriterLock(self.home):
                first_entered.set()
                release_first.wait(timeout=2)

        def second_writer() -> None:
            if not first_entered.wait(timeout=2):
                return
            with SkillWriterLock(self.home):
                second_entered.set()

        first = threading.Thread(target=first_writer)
        second = threading.Thread(target=second_writer)
        first.start()
        second.start()
        self.assertTrue(first_entered.wait(timeout=2))
        self.assertFalse(second_entered.wait(timeout=0.1))
        release_first.set()
        first.join(timeout=2)
        second.join(timeout=2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertTrue(second_entered.is_set())


if __name__ == "__main__":
    unittest.main()
