import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito_cli_loader import load_cli
from aikito_inbox import (
    InboxNoteRow,
    InboxTargetConflictError,
    collect_inbox_rows,
    find_inbox_files,
    remove_inbox_note,
    resolve_inbox_target,
    resolve_inbox_target_for_command,
)
from aikito_render import render_inbox_table

ROOT = Path(__file__).resolve().parents[1]
AIKITO_CLI = load_cli()


class AikitoInboxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.tmp.name).resolve()
        self.inbox_dir = self.aikito_dir / "inbox"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_find_inbox_files_missing_dir(self) -> None:
        self.assertEqual(find_inbox_files(self.inbox_dir), [])

    def test_find_inbox_files_and_filtering(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "note1.md").write_text("# Note 1")
        (self.inbox_dir / "note2.md").write_text("# Note 2")
        (self.inbox_dir / ".DS_Store").write_text("binary")
        (self.inbox_dir / ".hidden.md").write_text("# Hidden")
        (self.inbox_dir / "other.txt").write_text("plain text")

        sub = self.inbox_dir / "subfolder"
        sub.mkdir()
        (sub / "nested.md").write_text("# Nested")

        hidden_dir = self.inbox_dir / ".hidden_dir"
        hidden_dir.mkdir()
        (hidden_dir / "secret.md").write_text("# Secret")

        files = find_inbox_files(self.inbox_dir)
        names = [f.name for f in files]
        self.assertEqual(sorted(names), ["nested.md", "note1.md", "note2.md"])

    def test_collect_inbox_rows_ordering(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note_old = self.inbox_dir / "old-note.md"
        note_new = self.inbox_dir / "new-note.md"

        note_old.write_text("# Old Note")
        time.sleep(0.01)
        note_new.write_text("# New Note")

        # Set older mtime explicitly
        os.utime(note_old, (1000000000, 1000000000))
        os.utime(note_new, (1000005000, 1000005000))

        rows = collect_inbox_rows(self.inbox_dir)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].name, "new-note")
        self.assertEqual(rows[1].name, "old-note")
        self.assertRegex(rows[0].modified, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

    def test_resolve_inbox_target_exact_and_prefix(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "perplexity-positioning.md").write_text(
            "# Perplexity Content"
        )
        (self.inbox_dir / "claude-guidelines.md").write_text("# Claude Content")

        # Exact match stem
        p1 = resolve_inbox_target(self.inbox_dir, "perplexity-positioning")
        self.assertEqual(p1.name, "perplexity-positioning.md")

        # Exact match with .md extension
        p2 = resolve_inbox_target(self.inbox_dir, "claude-guidelines.md")
        self.assertEqual(p2.name, "claude-guidelines.md")

        # Unique prefix match
        p3 = resolve_inbox_target(self.inbox_dir, "perp")
        self.assertEqual(p3.name, "perplexity-positioning.md")

    def test_resolve_inbox_target_conflict(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "ai-news.md").write_text("# News")
        (self.inbox_dir / "ai-notes.md").write_text("# Notes")

        with self.assertRaises(InboxTargetConflictError) as cm:
            resolve_inbox_target(self.inbox_dir, "ai")

        self.assertEqual(len(cm.exception.candidates), 2)

    def test_resolve_inbox_target_not_found(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "note.md").write_text("# Note")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            self.assertRaises(SystemExit) as cm,
        ):
            resolve_inbox_target(self.inbox_dir, "nonexistent")

        self.assertEqual(cm.exception.code, 1)
        self.assertIn(
            "[ERROR] Inbox note 'nonexistent' not found.", mock_stderr.getvalue()
        )
        self.assertIn(
            "Run 'aikito show inbox' to view available inbox files.",
            mock_stderr.getvalue(),
        )

    def test_resolve_inbox_target_for_command_conflict_formatting(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "report-alpha.md").write_text("# Alpha")
        (self.inbox_dir / "report-beta.md").write_text("# Beta")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            self.assertRaises(SystemExit) as cm,
        ):
            resolve_inbox_target_for_command(self.inbox_dir, "report", operation="show")

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT] Multiple inbox notes match 'report':", err)
        self.assertIn("- report-alpha", err)
        self.assertIn("- report-beta", err)
        self.assertIn("aikito show inbox report-alpha", err)
        self.assertIn("aikito show inbox report-beta", err)

    def test_render_inbox_table(self) -> None:
        rows = [
            InboxNoteRow(
                name="perplexity-positioning",
                modified="2026-08-18 17:30",
                file_path=Path("/tmp/perplexity-positioning.md"),
                mtime=100.0,
            )
        ]
        rendered = render_inbox_table(rows, use_unicode=True, use_color=False)
        self.assertIn("Name", rendered)
        self.assertIn("Modified", rendered)
        self.assertIn("perplexity-positioning", rendered)
        self.assertIn("2026-08-18 17:30", rendered)

    def test_cli_show_inbox_missing_dir_notice(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox"])
            args.func(args)

        out = mock_stdout.getvalue()
        self.assertIn(f"Inbox directory does not exist: {self.inbox_dir}", out)

    def test_cli_show_inbox_target_missing_dir_exits_1(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox", "any-note"])
            args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[ERROR] Inbox note 'any-note' not found.", err)
        self.assertIn("Run 'aikito show inbox' to view available inbox files.", err)

    def test_cli_show_inbox_empty_dir_notice(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox"])
            args.func(args)

        out = mock_stdout.getvalue()
        self.assertIn(f"Inbox is empty ({self.inbox_dir}).", out)

    def test_cli_show_inbox_list_table(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "sample-note.md").write_text("# Sample Note Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox"])
            args.func(args)

        out = mock_stdout.getvalue()
        self.assertIn("Name", out)
        self.assertIn("Modified", out)
        self.assertIn("sample-note", out)

    def test_cli_show_inbox_target_content(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note_path = self.inbox_dir / "sample-note.md"
        note_content = "# Sample Note Content\n\nSome body text here.\n"
        note_path.write_text(note_content)

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "inbox", "sample-note"]
            )
            args.func(args)

        self.assertEqual(mock_stdout.getvalue(), note_content)

    def test_cli_show_inbox_custom_config_path(self) -> None:
        custom_inbox = self.aikito_dir / "my_custom_inbox"
        custom_inbox.mkdir(parents=True)
        (custom_inbox / "custom-note.md").write_text("# Custom note body")

        config_file = self.aikito_dir / "config.toml"
        config_file.write_text(
            f'[inbox]\npath = "{custom_inbox.as_posix()}"\n', encoding="utf-8"
        )

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox"])
            args.func(args)

        out = mock_stdout.getvalue()
        self.assertIn("custom-note", out)

    def test_remove_inbox_note_function(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note1 = self.inbox_dir / "note1.md"
        note1.write_text("# Note 1")
        note2 = self.inbox_dir / "note2.md"
        note2.write_text("# Note 2")

        # Remove by str target
        deleted = remove_inbox_note(self.inbox_dir, "note1")
        self.assertEqual(deleted, note1)
        self.assertFalse(note1.exists())

        # Remove by Path target
        deleted2 = remove_inbox_note(self.inbox_dir, note2)
        self.assertEqual(deleted2, note2)
        self.assertFalse(note2.exists())

        # Nonexistent raises FileNotFoundError
        with self.assertRaises(FileNotFoundError):
            remove_inbox_note(self.inbox_dir, self.inbox_dir / "missing.md")

    def test_cli_edit_inbox_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["edit", "inbox", "my-note"])
        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "inbox")
        self.assertEqual(args.target, "my-note")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_inbox)

    def test_cli_edit_inbox_invokes_editor(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note_file = self.inbox_dir / "sample-note.md"
        note_file.write_text("# Sample Note")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(AIKITO_CLI, "open_in_editor") as mock_open,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "inbox", "sample"])
            args.func(args)

        mock_open.assert_called_once_with(note_file)

    def test_cli_edit_inbox_conflict(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "report-alpha.md").write_text("# Alpha")
        (self.inbox_dir / "report-beta.md").write_text("# Beta")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "inbox", "report"])
            args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT] Multiple inbox notes match 'report':", err)
        self.assertIn("aikito edit inbox report-alpha", err)
        self.assertIn("aikito edit inbox report-beta", err)

    def test_cli_rm_inbox_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args_rm = parser.parse_args(["rm", "inbox", "target-note"])
        self.assertEqual(args_rm.command, "rm")
        self.assertEqual(args_rm.rm_target, "inbox")
        self.assertEqual(args_rm.target, "target-note")
        self.assertEqual(args_rm.func, AIKITO_CLI.cmd_rm_inbox)

        args_remove = parser.parse_args(["remove", "inbox", "target-note"])
        self.assertEqual(args_remove.command, "remove")
        self.assertEqual(args_remove.remove_target, "inbox")
        self.assertEqual(args_remove.target, "target-note")
        self.assertEqual(args_remove.func, AIKITO_CLI.cmd_rm_inbox)

    def test_cli_rm_inbox_success(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note_path = self.inbox_dir / "obsolete-note.md"
        note_path.write_text("# Obsolete Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rm", "inbox", "obsolete-note"]
            )
            args.func(args)

        self.assertFalse(note_path.exists())
        out = mock_stdout.getvalue()
        self.assertIn("[OK] Removed inbox note 'obsolete-note' (obsolete-note.md)", out)

    def test_cli_remove_inbox_alias(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        note_path = self.inbox_dir / "discard-note.md"
        note_path.write_text("# Discard Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["remove", "inbox", "discard-note"]
            )
            args.func(args)

        self.assertFalse(note_path.exists())
        out = mock_stdout.getvalue()
        self.assertIn("[OK] Removed inbox note 'discard-note' (discard-note.md)", out)

    def test_cli_rm_inbox_nested_note(self) -> None:
        nested_dir = self.inbox_dir / "research"
        nested_dir.mkdir(parents=True)
        note_path = nested_dir / "deepseek-test.md"
        note_path.write_text("# Deepseek Test")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rm", "inbox", "research/deepseek-test"]
            )
            args.func(args)

        self.assertFalse(note_path.exists())
        out = mock_stdout.getvalue()
        self.assertIn(
            "[OK] Removed inbox note 'research/deepseek-test' (deepseek-test.md)", out
        )

    def test_cli_rm_inbox_conflict(self) -> None:
        self.inbox_dir.mkdir(parents=True)
        (self.inbox_dir / "draft-1.md").write_text("# Draft 1")
        (self.inbox_dir / "draft-2.md").write_text("# Draft 2")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "inbox", "draft"])
            args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT] Multiple inbox notes match 'draft':", err)
        self.assertIn("aikito rm inbox draft-1", err)
        self.assertIn("aikito rm inbox draft-2", err)

    def test_cli_rm_inbox_not_found(self) -> None:
        self.inbox_dir.mkdir(parents=True)

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "inbox", "nonexistent"])
            args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[ERROR] Inbox note 'nonexistent' not found.", err)

    def test_cli_rm_inbox_custom_config_path(self) -> None:
        custom_inbox = self.aikito_dir / "custom_inbox"
        custom_inbox.mkdir(parents=True)
        note = custom_inbox / "temp.md"
        note.write_text("# Temp")

        config_file = self.aikito_dir / "config.toml"
        config_file.write_text(
            f'[inbox]\npath = "{custom_inbox.as_posix()}"\n', encoding="utf-8"
        )

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "inbox", "temp"])
            args.func(args)

        self.assertFalse(note.exists())
        self.assertIn(
            "[OK] Removed inbox note 'temp' (temp.md)", mock_stdout.getvalue()
        )


if __name__ == "__main__":
    unittest.main()
