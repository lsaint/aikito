import importlib.machinery
import importlib.util
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

LOADER = importlib.machinery.SourceFileLoader(
    "aikito_cli", str(ROOT / "bin" / "aikito")
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
AIKITO_CLI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(AIKITO_CLI)
sys.modules["aikito_cli"] = AIKITO_CLI


class GlobalEntrySyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.agent_dir = self.root / ".agent"
        self.target = self.agent_dir / "skills"
        self.source.mkdir()
        self.agent_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_and_preserves_expected_link(self) -> None:
        self.assertTrue(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )
        self.assertEqual(self.target.resolve(), self.source.resolve())
        self.assertTrue(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )

    def test_regular_directory_is_reported_as_conflict(self) -> None:
        self.target.mkdir()

        self.assertFalse(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )
        self.assertTrue(self.target.is_dir())


class SyncSubcommandParserTest(unittest.TestCase):
    def test_diff_command(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args = parser.parse_args(["diff"])

        self.assertEqual(args.command, "diff")
        self.assertEqual(args.func, AIKITO_CLI.cmd_diff)

    def test_sync_subcommands(self) -> None:
        parser = AIKITO_CLI.build_parser()

        # Global
        args = parser.parse_args(["sync", "global"])
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_target, "global")
        self.assertEqual(args.func, AIKITO_CLI.cmd_global_sync)

        # Project with path
        args = parser.parse_args(["sync", "project", "doxturbo", "~/com/doxturbo"])
        self.assertEqual(args.sync_target, "project")
        self.assertEqual(args.project_name, "doxturbo")
        self.assertEqual(args.project_path, "~/com/doxturbo")

        # Project without path
        args = parser.parse_args(["sync", "project", "doxturbo"])
        self.assertEqual(args.sync_target, "project")
        self.assertEqual(args.project_name, "doxturbo")
        self.assertIsNone(args.project_path)

        # MCP
        args = parser.parse_args(["sync", "mcp", "--dry-run", "--force"])
        self.assertEqual(args.sync_target, "mcp")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.force)

        # Subagents (and alias subagent)
        args = parser.parse_args(
            [
                "sync",
                "subagents",
                "--dry-run",
                "--prune",
                "--force",
                "claude-code/formatter",
            ]
        )
        self.assertEqual(args.sync_target, "subagents")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.prune)
        self.assertEqual(args.force, ["claude-code/formatter"])

        args_alias = parser.parse_args(["sync", "subagent"])
        self.assertEqual(args_alias.sync_target, "subagent")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_subagent_sync)


class InitSubcommandParserTest(unittest.TestCase):
    def test_init_workspace_and_project_subcommands(self) -> None:
        parser = AIKITO_CLI.build_parser()

        workspace_args = parser.parse_args(["init", "workspace", "~/aikito"])
        self.assertEqual(workspace_args.init_target, "workspace")
        self.assertEqual(workspace_args.workspace_path, "~/aikito")
        self.assertEqual(workspace_args.func, AIKITO_CLI.cmd_init)

        project_args = parser.parse_args(["init", "project"])
        self.assertEqual(project_args.init_target, "project")
        self.assertIsNone(project_args.project_name)
        self.assertIsNone(project_args.project_path)
        self.assertEqual(project_args.func, AIKITO_CLI.cmd_init_project)

        explicit_args = parser.parse_args(
            ["init", "project", "example", "~/code/example"]
        )
        self.assertEqual(explicit_args.project_name, "example")
        self.assertEqual(explicit_args.project_path, "~/code/example")

    def test_legacy_init_syntax_is_rejected(self) -> None:
        parser = AIKITO_CLI.build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "~/aikito"])

    def test_init_project_command_creates_runtime_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "example"
            project.mkdir()
            AIKITO_CLI.init_workspace(workspace, root)

            args = AIKITO_CLI.build_parser().parse_args(["init", "project"])
            with (
                patch.object(AIKITO_CLI, "get_aikito_dir", return_value=workspace),
                patch.object(AIKITO_CLI.Path, "cwd", return_value=project),
            ):
                args.func(args)
                args.func(args)

            canonical = workspace / "projects" / "example"
            runtime = project / ".agents"
            self.assertEqual(
                (runtime / "AGENTS.md").resolve(),
                (canonical / "AGENTS.md").resolve(),
            )
            self.assertEqual(
                (runtime / "memory" / "index.md").resolve(),
                (canonical / "memory" / "index.md").resolve(),
            )


class AuthSubcommandParserTest(unittest.TestCase):
    def test_auth_mcp_subcommand(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args = parser.parse_args(["auth", "mcp", "opencode", "atlassian-rovo"])
        self.assertEqual(args.command, "auth")
        self.assertEqual(args.auth_target, "mcp")
        self.assertEqual(args.agent, "opencode")
        self.assertEqual(args.server, "atlassian-rovo")
        self.assertEqual(args.func, AIKITO_CLI.cmd_mcp_auth)


class ShowMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "projects" / "doxturbo" / "memory" / "notes").mkdir(
            parents=True
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_memory_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["show", "memory", "simplified-clean-architecture"])
        self.assertEqual(args.command, "show")
        self.assertEqual(args.show_target, "memory")
        self.assertEqual(args.target, "simplified-clean-architecture")
        self.assertEqual(args.func, AIKITO_CLI.cmd_show_memory)

    def test_show_memory_unique_match(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "unique-note.md"
        global_note.write_text("# Unique Note Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "unique-note"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Unique Note Content")

    def test_show_memory_unique_prefix_match(self) -> None:
        note = (
            self.aikito_dir
            / "projects"
            / "doxturbo"
            / "memory"
            / "notes"
            / "long-running-tasks-retry-strategy.md"
        )
        note.write_text("# Retry Strategy")

        for target in ("long-running-tasks", "long-running-tasks-retr…"):
            with (
                self.subTest(target=target),
                patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
                patch.object(
                    AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir
                ),
            ):
                args = AIKITO_CLI.build_parser().parse_args(["show", "memory", target])
                args.func(args)
                self.assertEqual(mock_stdout.getvalue(), "# Retry Strategy")

    def test_show_memory_exact_match_wins_over_prefix_matches(self) -> None:
        exact_note = self.aikito_dir / "memory" / "notes" / "release.md"
        exact_note.write_text("# Release")
        longer_note = self.aikito_dir / "memory" / "notes" / "release-checklist.md"
        longer_note.write_text("# Release Checklist")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release"])
            args.func(args)

        self.assertEqual(mock_stdout.getvalue(), "# Release")

    def test_show_memory_ambiguous_prefix_requires_disambiguation(self) -> None:
        (self.aikito_dir / "memory" / "notes" / "release-checklist.md").write_text(
            "# Checklist"
        )
        (self.aikito_dir / "memory" / "notes" / "release-process.md").write_text(
            "# Process"
        )

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release-"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/release-checklist", error_message)
        self.assertIn("global/notes/release-process", error_message)

    def test_show_memory_conflict_and_disambiguation(self) -> None:
        g_note = self.aikito_dir / "memory" / "notes" / "conflict.md"
        g_note.write_text("# Global Conflict Content")
        p_note = (
            self.aikito_dir
            / "projects"
            / "doxturbo"
            / "memory"
            / "notes"
            / "conflict.md"
        )
        p_note.write_text("# Project Conflict Content")

        # 1. Stem conflict -> exits with code 1 and prints candidates & disambiguation command to stderr
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "conflict"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/conflict", error_message)
        self.assertIn("doxturbo/notes/conflict", error_message)
        self.assertIn("aikito show memory global/notes/conflict", error_message)
        self.assertIn("aikito show memory doxturbo/notes/conflict", error_message)

        # 2. Disambiguated by scope/type/stem -> prints exact content
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "global/notes/conflict"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Global Conflict Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "doxturbo/notes/conflict"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Project Conflict Content")

    def test_show_memory_same_scope_conflict(self) -> None:
        (self.aikito_dir / "memory" / "notes" / "release-checklist.md").write_text(
            "# Checklist"
        )
        (self.aikito_dir / "memory" / "notes" / "release-process.md").write_text(
            "# Process"
        )

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/release-checklist", error_message)
        self.assertIn("global/notes/release-process", error_message)

    def test_show_memory_not_found(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "nonexistent"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        self.assertIn(
            "[ERROR] Memory note 'nonexistent' not found.", mock_stderr.getvalue()
        )
        self.assertIn(
            "Run 'aikito show memory' to view available memory files.",
            mock_stderr.getvalue(),
        )

    def test_show_memory_omitted_target_renders_notes_table(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "global-note.md"
        global_note.write_text("# Global Note")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("global-note", output)
            self.assertIn("Scope", output)
            self.assertIn("Note File", output)


class ShowSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "skills").mkdir(parents=True)

        (self.aikito_dir / "skills.toml").write_text(
            'skills = ["durable-memory", "agent-browser"]\n'
        )

        g_skill = self.aikito_dir / "skills" / "durable-memory"
        g_skill.mkdir()
        (g_skill / "SKILL.md").write_text("# Durable Memory Content")

        b_skill = self.aikito_dir / "skills" / "agent-browser"
        b_skill.mkdir()
        (b_skill / "SKILL.md").write_text("# Agent Browser Content")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_skill_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["show", "skill", "durable-memory"])
        self.assertEqual(args.command, "show")
        self.assertEqual(args.show_target, "skill")
        self.assertEqual(args.target, "durable-memory")
        self.assertEqual(args.func, AIKITO_CLI.cmd_show_skill)

        args_alias = parser.parse_args(["show", "skills", "dur"])
        self.assertEqual(args_alias.show_target, "skills")
        self.assertEqual(args_alias.target, "dur")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_show_skill)

    def test_show_skill_unique_match(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "durable-memory"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Durable Memory Content")

    def test_show_skill_unique_prefix_match(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "skill", "dur"])
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Durable Memory Content")

    def test_show_skill_ambiguous_prefix_conflict(self) -> None:
        s1 = self.aikito_dir / "skills" / "gino-code-review"
        s1.mkdir()
        (s1 / "SKILL.md").write_text("s1")
        s2 = self.aikito_dir / "skills" / "gino-jira"
        s2.mkdir()
        (s2 / "SKILL.md").write_text("s2")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "skill", "gino"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", err)
        self.assertIn("gino-code-review", err)
        self.assertIn("gino-jira", err)

    def test_show_skill_not_found(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "nonexistent"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Skill 'nonexistent' not found.", mock_stderr.getvalue())


class EditMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_memory_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["edit", "memory", "test-note"])
        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "memory")
        self.assertEqual(args.target, "test-note")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_memory)

    def test_edit_memory_invokes_editor_with_shlex(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "edit-note.md"
        global_note.write_text("# Edit Note")

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"EDITOR": "code --wait"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "memory", "edit-note"])
            args.func(args)
            mock_run.assert_called_once_with(["code", "--wait", str(global_note)])

    def test_edit_memory_conflict_suggests_edit_command(self) -> None:
        project_notes = self.aikito_dir / "projects" / "doxturbo" / "memory" / "notes"
        project_notes.mkdir(parents=True)
        (self.aikito_dir / "memory" / "notes" / "conflict.md").write_text(
            "# Global Conflict"
        )
        (project_notes / "conflict.md").write_text("# Project Conflict")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "memory", "conflict"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("aikito edit memory global/notes/conflict", error_message)
        self.assertIn("aikito edit memory doxturbo/notes/conflict", error_message)
        self.assertNotIn("aikito show memory", error_message)

    def test_path_escape_prevention(self) -> None:
        outside_dir = Path(self.temporary_directory.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.md"
        outside_file.write_text("secret")

        # Create a symlink in memory pointing outside
        bad_symlink = self.aikito_dir / "memory" / "notes" / "escaped.md"
        bad_symlink.symlink_to(outside_file)

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "escaped"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("[ERROR] Path escape detected", mock_stderr.getvalue())


class EditInstructionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        self.home = self.aikito_dir / "home"
        self.project_path = self.aikito_dir / "project-code"
        project_dir = self.aikito_dir / "projects" / "doxturbo"
        project_dir.mkdir(parents=True)
        self.instructions_file = project_dir / "AGENTS.md"
        self.instructions_file.write_text("", encoding="utf-8")
        (project_dir / "agent.toml").write_text(
            f'name = "doxturbo"\npath = "{self.project_path}"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_instructions_parser(self) -> None:
        args = AIKITO_CLI.build_parser().parse_args(
            ["edit", "instructions", "doxturbo"]
        )

        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "instructions")
        self.assertEqual(args.target, "doxturbo")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_instructions)

    def test_edit_instructions_invokes_editor(self) -> None:
        mock_proc = MagicMock(returncode=0)
        with (
            patch("os.environ", {"EDITOR": "code --wait"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["edit", "instructions", "doxturbo"]
            )
            args.func(args)

        mock_run.assert_called_once_with(
            ["code", "--wait", str(self.instructions_file)]
        )


class EditSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "skills").mkdir(parents=True)
        (self.aikito_dir / "skills.toml").write_text('skills = ["durable-memory"]\n')

        skill_dir = self.aikito_dir / "skills" / "durable-memory"
        skill_dir.mkdir()
        self.skill_file = skill_dir / "SKILL.md"
        self.skill_file.write_text("# Durable Memory")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_skill_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["edit", "skill", "durable-memory"])
        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "skill")
        self.assertEqual(args.target, "durable-memory")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_skill)

        args_alias = parser.parse_args(["edit", "skills", "dur"])
        self.assertEqual(args_alias.edit_target, "skills")
        self.assertEqual(args_alias.target, "dur")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_edit_skill)

    def test_edit_skill_invokes_editor(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"EDITOR": "nano"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "dur"])
            args.func(args)
            mock_run.assert_called_once_with(["nano", str(self.skill_file)])

    def test_edit_skill_whitespace_editor_fallback_to_vi(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"VISUAL": "   ", "EDITOR": "   "}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "dur"])
            args.func(args)
            mock_run.assert_called_once_with(["vi", str(self.skill_file)])

    def test_edit_skill_conflict_suggests_edit_command(self) -> None:
        s1 = self.aikito_dir / "skills" / "gino-code-review"
        s1.mkdir()
        (s1 / "SKILL.md").write_text("s1")
        s2 = self.aikito_dir / "skills" / "gino-jira"
        s2.mkdir()
        (s2 / "SKILL.md").write_text("s2")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "gino"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", err)
        self.assertIn("aikito edit skill gino-code-review", err)
        self.assertIn("aikito edit skill gino-jira", err)

    def test_skill_path_escape_prevention(self) -> None:
        outside_dir = Path(self.temporary_directory.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "SKILL.md"
        outside_file.write_text("secret skill")

        # Create a symlink in skills pointing outside
        bad_symlink_dir = self.aikito_dir / "skills" / "escaped-skill"
        bad_symlink_dir.mkdir()
        bad_symlink_file = bad_symlink_dir / "SKILL.md"
        bad_symlink_file.symlink_to(outside_file)

        (self.aikito_dir / "skills.toml").write_text('skills = ["escaped-skill"]\n')

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "escaped-skill"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn(
                "[ERROR] Path escape detected for skill", mock_stderr.getvalue()
            )


class ShowSubcommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        self.home = self.aikito_dir / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.aikito_dir / "agents.toml").write_text(
            """
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

[agents.codex.subagents]
config_path = ".codex/agents"
config_format = "codex_toml"

[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "verbatim"
""".lstrip()
        )
        (self.aikito_dir / "mcps.toml").write_text(
            '[servers.managed]\ntransport = "remote"\nurl = "http://ex.com"\nagents = ["codex"]\n'
        )
        (self.aikito_dir / "subagents.toml").write_text(
            '[subagents.formatter]\ndescription = "Format"\nagents = ["codex"]\n'
        )
        (self.aikito_dir / "subagents").mkdir()
        (self.aikito_dir / "subagents" / "formatter.md").write_text(
            "# Formatter Instructions"
        )
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "memory" / "index.md").write_text("# Global Memory Index")
        (self.home / ".codex/config.toml").write_text(
            """
[mcp_servers.managed]
url = "http://ex.com"

[mcp_servers.custom]
url = "http://custom.example.com"
""".lstrip()
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_mcp(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "mcp"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("MCP Server", output)
            self.assertIn("managed", output)

    def test_show_mcp_agent_view_lists_managed_and_unmanaged(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "--agent", "codex", "--color", "never"]
            )
            args.func(args)

        output = mock_stdout.getvalue()
        self.assertIn("Agent: Codex", output)
        self.assertIn(str(self.home / ".codex/config.toml"), output)
        self.assertIn("1 managed", output)
        self.assertIn("1 unmanaged", output)
        self.assertIn("custom", output)

    def test_show_mcp_intersection_displays_managed_entry(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "man", "--agent", "codex"]
            )
            args.func(args)

        output = mock_stdout.getvalue()
        self.assertIn("MCP Server: managed", output)
        self.assertEqual(output.count("MCP Server: managed"), 1)
        self.assertIn("| Codex", output)
        self.assertNotIn("Agent: Codex", output)
        self.assertIn("Managed entry:", output)
        self.assertIn('"url": "http://ex.com"', output)

    def test_show_subagents(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "subagents"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Subagent", output)
            self.assertIn("formatter", output)

    def test_show_instructions_lists_and_reads_targets(self) -> None:
        project_path = self.aikito_dir / "project-code"
        project_dir = self.aikito_dir / "projects" / "example"
        project_dir.mkdir(parents=True)
        (self.aikito_dir / "global").mkdir()
        (self.aikito_dir / "global" / "AGENTS.md").write_text("global rules\n")
        (self.home / ".codex" / "AGENTS.md").symlink_to(
            self.aikito_dir / "global" / "AGENTS.md"
        )
        (project_dir / "AGENTS.md").write_text("project rules\n")
        (project_dir / "agent.toml").write_text(
            f'name = "example"\npath = "{project_path}"\n'
        )
        (project_path / ".agents").mkdir(parents=True)
        (project_path / ".agents" / "AGENTS.md").symlink_to(project_dir / "AGENTS.md")
        empty_project_dir = self.aikito_dir / "projects" / "empty"
        empty_project_dir.mkdir()
        (empty_project_dir / "AGENTS.md").write_text("\n")
        (empty_project_dir / "agent.toml").write_text(
            f'name = "empty"\npath = "{self.aikito_dir / "empty-code"}"\n'
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "--color", "always", "--no-color"]
            )
            args.func(args)
        listing = mock_stdout.getvalue()
        self.assertIn("╭", listing)
        self.assertIn("│ Codex", listing)
        self.assertIn("Status: linked", listing)
        self.assertIn("Target: ~/.codex/AGENTS.md", listing)
        self.assertIn("│ Projects", listing)
        self.assertIn("example: linked", listing)
        self.assertIn("empty: -", listing)

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "global"]
            )
            args.func(args)
        self.assertEqual(mock_stdout.getvalue(), "global rules\n")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "example"]
            )
            args.func(args)
        self.assertEqual(mock_stdout.getvalue(), "project rules\n")

    def test_show_instructions_dot_resolves_current_project(self) -> None:
        project_path = self.aikito_dir / "project-code"
        nested_path = project_path / "src"
        project_dir = self.aikito_dir / "projects" / "example"
        nested_path.mkdir(parents=True)
        project_dir.mkdir(parents=True)
        (project_dir / "AGENTS.md").write_text("project rules\n")
        (project_dir / "agent.toml").write_text(
            f'name = "example"\npath = "{project_path}"\n'
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch.object(Path, "cwd", return_value=nested_path),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "instructions", "."])
            args.func(args)

        self.assertEqual(mock_stdout.getvalue(), "project rules\n")
        self.assertIn("Also active: global instructions", mock_stderr.getvalue())

    def test_status_has_no_subcommands(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit) as cm:
                AIKITO_CLI.build_parser().parse_args(["status", "mcp"])
            self.assertEqual(cm.exception.code, 2)

    def test_resolve_color_flags(self) -> None:
        parser = AIKITO_CLI.build_parser()

        with patch.dict(AIKITO_CLI.os.environ, {"NO_COLOR": ""}):
            args = parser.parse_args(["show", "mcp", "--color", "always"])
            use_unicode, use_color = AIKITO_CLI.resolve_color_flags(args)
            self.assertTrue(use_unicode)
            self.assertTrue(use_color)

            args_no_color = parser.parse_args(
                ["show", "mcp", "--color", "always", "--no-color"]
            )
            use_unicode, use_color = AIKITO_CLI.resolve_color_flags(args_no_color)
            self.assertTrue(use_unicode)
            self.assertFalse(use_color)


if __name__ == "__main__":
    unittest.main()
