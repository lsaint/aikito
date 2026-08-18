"""Tests for aikito inbox functionality and CLI."""

import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import importlib.machinery
import importlib.util

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
from aikito_inbox import (  # noqa: E402
    InboxNoteRow,
    InboxTargetConflictError,
    collect_inbox_rows,
    find_inbox_files,
    resolve_inbox_target,
    resolve_inbox_target_for_command,
)
from aikito_render import render_inbox_table  # noqa: E402


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
        config_file.write_text(f'[inbox]\npath = "{custom_inbox}"\n', encoding="utf-8")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "inbox"])
            args.func(args)

        out = mock_stdout.getvalue()
        self.assertIn("custom-note", out)


if __name__ == "__main__":
    unittest.main()
