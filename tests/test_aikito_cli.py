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

        # Stem conflict -> exits with code 1 and prints candidates to stderr
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "conflict"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            err_msg = mock_stderr.getvalue()
            self.assertIn("[CONFLICT]", err_msg)
            self.assertIn("global/notes/conflict", err_msg)
            self.assertIn("doxturbo/notes/conflict", err_msg)
            self.assertIn("aikito show memory global/notes/conflict", err_msg)
            self.assertNotIn("aikito edit memory", err_msg)

        # Disambiguated by full identifier: global/notes/conflict
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "global/notes/conflict"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Global Conflict Content")

        # Disambiguated by full identifier: doxturbo/notes/conflict
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
        (self.aikito_dir / "memory" / "archive").mkdir(parents=True, exist_ok=True)
        note1 = self.aikito_dir / "memory" / "notes" / "dup.md"
        note1.write_text("# Active Dup Note")
        note2 = self.aikito_dir / "memory" / "archive" / "dup.md"
        note2.write_text("# Archived Dup Note")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "dup"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            err_msg = mock_stderr.getvalue()
            self.assertIn("[CONFLICT]", err_msg)
            self.assertIn("global/notes/dup", err_msg)
            self.assertIn("global/archive/dup", err_msg)

        # Disambiguated by full identifier: global/archive/dup
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "global/archive/dup"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Archived Dup Note")

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
            err_msg = mock_stderr.getvalue()
            self.assertIn("[ERROR] Memory note 'nonexistent' not found.", err_msg)
            self.assertIn(
                "Run 'aikito status memory' to view available memory files.", err_msg
            )


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


if __name__ == "__main__":
    unittest.main()
