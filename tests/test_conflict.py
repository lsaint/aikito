from layout_helpers import write_agents
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import (
    InvalidProjectConfigError,
    Project,
    ProjectPrepareConflictError,
)
from aikito.cli import cmd_global_sync, cmd_project_sync
from aikito.conflict import (
    collect_resource_conflicts,
    find_conflict_marker_lines,
    has_any_conflict_markers,
)


class ConflictDetectionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_grouped_conflict_in_markdown_is_blocking(self) -> None:
        md = self.root / "note.md"
        md.write_text(
            "# Note\n<<<<<<< HEAD\nversion A\n=======\nversion B\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(md)
        self.assertEqual(blocking, [2, 4, 6])
        self.assertEqual(isolated, [])

    def test_isolated_heading_underline_is_not_blocking(self) -> None:
        md = self.root / "heading.md"
        md.write_text(
            "Section Title\n=======\nSome content here\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(md)
        self.assertEqual(blocking, [])
        self.assertEqual(isolated, [2])

    def test_code_block_with_conflict_is_blocking(self) -> None:
        md = self.root / "code.md"
        md.write_text(
            "```python\n"
            "<<<<<<< HEAD\n"
            "def foo(): pass\n"
            "=======\n"
            "def foo(): return 1\n"
            ">>>>>>> branch\n"
            "```\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(md)
        self.assertEqual(blocking, [2, 4, 6])
        self.assertEqual(isolated, [])

    def test_diff3_conflict_is_blocking(self) -> None:
        md = self.root / "diff3.md"
        md.write_text(
            "<<<<<<< HEAD\nA\n||||||| base\nbase content\n=======\nB\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(md)
        self.assertEqual(blocking, [1, 3, 5, 7])
        self.assertEqual(isolated, [])

    def test_multiple_conflicts_in_one_file(self) -> None:
        md = self.root / "multi.md"
        md.write_text(
            "<<<<<<< HEAD\n"
            "A\n"
            "=======\n"
            "B\n"
            ">>>>>>> br1\n"
            "middle\n"
            "<<<<<<< HEAD\n"
            "C\n"
            "=======\n"
            "D\n"
            ">>>>>>> br2\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(md)
        self.assertEqual(blocking, [1, 3, 5, 7, 9, 11])
        self.assertEqual(isolated, [])

    def test_toml_any_marker_is_blocking(self) -> None:
        t = self.root / "config.toml"
        t.write_text(
            "<<<<<<< HEAD\na = 1\n=======\na = 2\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        blocking, isolated = find_conflict_marker_lines(t)
        self.assertEqual(blocking, [1, 3, 5])
        self.assertEqual(isolated, [])

    def test_toml_isolated_marker_is_blocking(self) -> None:
        t = self.root / "single.toml"
        t.write_text("<<<<<<< HEAD\n", encoding="utf-8")
        blocking, isolated = find_conflict_marker_lines(t)
        self.assertEqual(blocking, [1])
        self.assertEqual(isolated, [])

    def test_has_any_conflict_markers_helper(self) -> None:
        self.assertTrue(has_any_conflict_markers("<<<<<<< HEAD\n"))
        self.assertTrue(has_any_conflict_markers("=======\n"))
        self.assertTrue(has_any_conflict_markers(">>>>>>> br\n"))
        self.assertFalse(has_any_conflict_markers("# Clean note\nfoo = 'bar'\n"))

    def test_collect_resource_conflicts_reports_file_and_line(self) -> None:
        md = self.root / "note.md"
        md.write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n",
            encoding="utf-8",
        )
        errors = collect_resource_conflicts([self.root], self.root)
        self.assertEqual(len(errors), 3)
        self.assertTrue(any(":1: Git conflict marker detected" in e for e in errors))
        self.assertTrue(any(":3: Git conflict marker detected" in e for e in errors))
        self.assertTrue(any(":5: Git conflict marker detected" in e for e in errors))


class ProjectPrepareConflictCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = (self.root / "home").resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.project_path = (self.root / "code").resolve()
        self.definition = (self.workspace / "projects" / "demo").resolve()
        self.home.mkdir()
        self.project_path.mkdir()

        (self.definition / "memory" / "notes").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill" / "SKILL.md").write_text(
            "# Demo skill\n", encoding="utf-8"
        )
        (self.definition / "AGENTS.md").write_text(
            "# Project instructions\n", encoding="utf-8"
        )
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\n'
            'sync_mode = "link"\nskills = ["demo-skill"]\n',
            encoding="utf-8",
        )
        write_agents(
            self.workspace,
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n',
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_prepare_aborts_on_project_instruction_conflict(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "# Project instructions\n"
            "<<<<<<< HEAD\n"
            "rule A\n"
            "=======\n"
            "rule B\n"
            ">>>>>>> branch\n",
            encoding="utf-8",
        )
        project = Project.load("demo", workspace=self.workspace, home=self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            project.prepare(agent="pi")
        self.assertIn("Git conflict marker detected", str(ctx.exception))
        self.assertIn("AGENTS.md:2", str(ctx.exception))
        # Ensure no files/links were written to project runtime
        self.assertFalse((self.project_path / ".agents").exists())

    def test_prepare_aborts_on_project_skill_conflict(self) -> None:
        (self.workspace / "skills" / "demo-skill" / "SKILL.md").write_text(
            "<<<<<<< HEAD\n# Skill A\n=======\n# Skill B\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        project = Project.load("demo", workspace=self.workspace, home=self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            project.prepare(agent="pi")
        self.assertIn("Git conflict marker detected", str(ctx.exception))
        self.assertIn("SKILL.md:1", str(ctx.exception))
        self.assertFalse((self.project_path / ".agents").exists())

    def test_prepare_aborts_on_project_memory_note_conflict(self) -> None:
        note = self.definition / "memory" / "notes" / "arch.md"
        note.write_text(
            "<<<<<<< HEAD\nnote A\n=======\nnote B\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        project = Project.load("demo", workspace=self.workspace, home=self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            project.prepare(agent="pi")
        self.assertIn("Git conflict marker detected", str(ctx.exception))
        self.assertIn("arch.md:1", str(ctx.exception))
        self.assertFalse((self.project_path / ".agents").exists())

    def test_prepare_aborts_on_referenced_memory_directory_conflict(self) -> None:
        shared_mem = self.workspace / "memory" / "shared"
        shared_mem.mkdir(parents=True, exist_ok=True)
        conflicted = shared_mem / "shared-note.md"
        conflicted.write_text(
            "<<<<<<< HEAD\nshared A\n=======\nshared B\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        # Update agent.toml to include memory = ["shared"] (a directory)
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\n'
            'sync_mode = "link"\nskills = ["demo-skill"]\n'
            'memory = ["shared"]\n',
            encoding="utf-8",
        )
        project = Project.load("demo", workspace=self.workspace, home=self.home)
        with self.assertRaises(ProjectPrepareConflictError) as ctx:
            project.prepare(agent="pi")
        self.assertIn("Git conflict marker detected", str(ctx.exception))
        self.assertIn("shared-note.md:1", str(ctx.exception))
        self.assertFalse((self.project_path / ".agents").exists())

    def test_prepare_isolated_heading_underline_does_not_abort(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "Project Title\n=======\nLegitimate content\n",
            encoding="utf-8",
        )
        project = Project.load("demo", workspace=self.workspace, home=self.home)
        # Must succeed without error
        prepared = project.prepare(agent="pi")
        self.assertEqual(prepared.name, "demo")
        self.assertTrue((self.project_path / ".agents").exists())

    def test_prepare_ignores_unrelated_project_conflict(self) -> None:
        # Create an unrelated project with conflict markers
        other = self.workspace / "projects" / "other"
        other.mkdir(parents=True)
        (other / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nX\n=======\nY\n>>>>>>> br\n", encoding="utf-8"
        )
        (other / "agent.toml").write_text(
            "<<<<<<< HEAD\npath = 'x'\n=======\npath = 'y'\n>>>>>>> br\n",
            encoding="utf-8",
        )

        project = Project.load("demo", workspace=self.workspace, home=self.home)
        prepared = project.prepare(agent="pi")
        self.assertEqual(prepared.name, "demo")
        self.assertTrue((self.project_path / ".agents").exists())

    def test_prepare_ignores_unrelated_global_instruction_conflict(self) -> None:
        global_inst = self.workspace / "global" / "AGENTS.md"
        global_inst.parent.mkdir(parents=True)
        global_inst.write_text(
            "<<<<<<< HEAD\nGlobal A\n=======\nGlobal B\n>>>>>>> br\n",
            encoding="utf-8",
        )

        project = Project.load("demo", workspace=self.workspace, home=self.home)
        prepared = project.prepare(agent="pi")
        self.assertEqual(prepared.name, "demo")
        self.assertTrue((self.project_path / ".agents").exists())

    def test_project_load_aborts_on_agent_toml_conflict(self) -> None:
        (self.definition / "agent.toml").write_text(
            "<<<<<<< HEAD\nname = 'demo'\n=======\nname = 'demo2'\n>>>>>>> branch\n",
            encoding="utf-8",
        )
        with self.assertRaises(InvalidProjectConfigError) as ctx:
            Project.load("demo", workspace=self.workspace, home=self.home)
        self.assertIn("Git conflict marker detected", str(ctx.exception))
        self.assertIn("agent.toml:1", str(ctx.exception))


class SyncCliConflictCheckTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.home = (self.root / "home").resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.project_path = (self.root / "code").resolve()
        self.definition = (self.workspace / "projects" / "demo").resolve()
        self.home.mkdir()
        self.project_path.mkdir()

        (self.definition / "memory" / "notes").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill" / "SKILL.md").write_text(
            "# Demo skill\n", encoding="utf-8"
        )
        (self.definition / "AGENTS.md").write_text(
            "# Project instructions\n", encoding="utf-8"
        )
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\n'
            'sync_mode = "link"\nskills = ["demo-skill"]\n',
            encoding="utf-8",
        )
        (self.workspace / "skills.toml").write_text(
            'skills = ["demo-skill"]\n', encoding="utf-8"
        )
        (self.workspace / "global").mkdir(parents=True)
        (self.workspace / "global" / "AGENTS.md").write_text(
            "# Global instructions\n", encoding="utf-8"
        )
        write_agents(
            self.workspace,
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n',
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_sync_project_aborts_on_instruction_conflict(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n", encoding="utf-8"
        )
        import argparse

        args = argparse.Namespace(
            project_name="demo",
            project_path=None,
            dry_run=False,
            force=False,
        )
        with (
            patch("aikito.cli.get_aikito_dir", return_value=self.workspace),
            patch("pathlib.Path.home", return_value=self.home),
            self.assertRaises(SystemExit) as ctx,
        ):
            cmd_project_sync(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse((self.project_path / ".agents").exists())

    def test_sync_project_force_cannot_bypass_conflict(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n", encoding="utf-8"
        )
        import argparse

        args = argparse.Namespace(
            project_name="demo",
            project_path=None,
            dry_run=False,
            force=True,
        )
        with (
            patch("aikito.cli.get_aikito_dir", return_value=self.workspace),
            patch("pathlib.Path.home", return_value=self.home),
            self.assertRaises(SystemExit) as ctx,
        ):
            cmd_project_sync(args)
        self.assertEqual(ctx.exception.code, 1)
        self.assertFalse((self.project_path / ".agents").exists())

    def test_sync_project_dry_run_reports_conflict(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n", encoding="utf-8"
        )
        import argparse

        args = argparse.Namespace(
            project_name="demo",
            project_path=None,
            dry_run=True,
            force=False,
        )
        with (
            patch("aikito.cli.get_aikito_dir", return_value=self.workspace),
            patch("pathlib.Path.home", return_value=self.home),
            self.assertRaises(SystemExit) as ctx,
        ):
            cmd_project_sync(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_sync_global_aborts_on_global_instruction_conflict(self) -> None:
        (self.workspace / "global" / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n", encoding="utf-8"
        )
        import argparse

        args = argparse.Namespace(dry_run=False)
        with (
            patch("aikito.cli.get_aikito_dir", return_value=self.workspace),
            patch("pathlib.Path.home", return_value=self.home),
            self.assertRaises(SystemExit) as ctx,
        ):
            cmd_global_sync(args)
        self.assertEqual(ctx.exception.code, 1)

    def test_sync_global_ignores_unrelated_project_conflict(self) -> None:
        (self.definition / "AGENTS.md").write_text(
            "<<<<<<< HEAD\nA\n=======\nB\n>>>>>>> br\n", encoding="utf-8"
        )
        import argparse

        args = argparse.Namespace(dry_run=False)
        with (
            patch("aikito.cli.get_aikito_dir", return_value=self.workspace),
            patch("pathlib.Path.home", return_value=self.home),
            patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"),
        ):
            cmd_global_sync(args)
        # Should complete successfully without raising SystemExit


if __name__ == "__main__":
    unittest.main()
