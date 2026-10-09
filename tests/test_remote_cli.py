"""Tests for 'aikito remote' CLI commands (bind and sync)."""

import io
import os
import unittest
from pathlib import Path
from unittest.mock import patch

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from aikito.cli_parser import build_parser
from aikito.remote_cli import cmd_remote_bind, cmd_remote_sync
from aikito.workspace.reconcile import ReconcileItem, WorkspaceReconcileError
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)


TOKEN = "test-remote-token-secret"
AUTH = RemoteAuth("bearer_env", "AIKITO_REMOTE_TEST_TOKEN")
ENV = {AUTH.env: TOKEN}


class RemoteCLIParserTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_remote_bind_args(self):
        args = self.parser.parse_args(
            ["remote", "bind", "https://example.com/v1/remote"]
        )
        self.assertEqual(args.command, "remote")
        self.assertEqual(args.remote_target, "bind")
        self.assertEqual(args.endpoint, "https://example.com/v1/remote")
        self.assertEqual(args.token_env, "AIKITO_REMOTE_TOKEN")

    def test_remote_bind_custom_token_env(self):
        args = self.parser.parse_args(
            [
                "remote",
                "bind",
                "https://example.com/v1/remote",
                "--token-env",
                "CUSTOM_TOKEN",
            ]
        )
        self.assertEqual(args.token_env, "CUSTOM_TOKEN")

    def test_remote_sync_dry_run(self):
        args = self.parser.parse_args(["remote", "sync", "--dry-run"])
        self.assertEqual(args.command, "remote")
        self.assertEqual(args.remote_target, "sync")
        self.assertTrue(args.dry_run)
        self.assertFalse(args.apply)
        self.assertFalse(args.verbose)

    def test_remote_sync_apply_verbose(self):
        args = self.parser.parse_args(["remote", "sync", "--apply", "--verbose"])
        self.assertEqual(args.command, "remote")
        self.assertEqual(args.remote_target, "sync")
        self.assertFalse(args.dry_run)
        self.assertTrue(args.apply)
        self.assertTrue(args.verbose)

    def test_remote_sync_requires_mode(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(["remote", "sync"])
            self.assertNotEqual(ctx.exception.code, 0)

    def test_remote_sync_mutually_exclusive_modes(self):
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as ctx:
                self.parser.parse_args(["remote", "sync", "--dry-run", "--apply"])
            self.assertNotEqual(ctx.exception.code, 0)


class RemoteCLIOperationsTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_bind_uninitialized_workspace(self, tmp_path=None):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            empty_ws = Path(tmp_dir) / "empty"
            empty_ws.mkdir()
            args = self.parser.parse_args(
                ["remote", "bind", "https://example.com/v1/remote"]
            )
            stderr = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=empty_ws),
                patch("sys.stderr", stderr),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_bind(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("not a recognized Aikito workspace", stderr.getvalue())

    def test_sync_without_binding(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            args = self.parser.parse_args(["remote", "sync", "--dry-run"])
            stderr = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stderr", stderr),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_sync(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("No remote binding configured", stderr.getvalue())

    def test_bind_insecure_endpoint(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            args = self.parser.parse_args(
                ["remote", "bind", "http://external.example.com/v1/remote"]
            )
            stderr = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stderr", stderr),
                patch.dict(os.environ, ENV),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_bind(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn("HTTPS outside loopback", stderr.getvalue())

    def test_bind_credentials_in_query(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            args = self.parser.parse_args(
                ["remote", "bind", "https://example.com/v1/remote?token=secret"]
            )
            stderr = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stderr", stderr),
                patch.dict(os.environ, ENV),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_bind(args)
            self.assertEqual(ctx.exception.code, 1)
            self.assertIn(
                "Credentials must not appear in endpoint URL query", stderr.getvalue()
            )

    def test_bind_and_sync_lifecycle(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind_a = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                stdout_bind = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_bind),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind_a)

                self.assertIn(
                    "[SUCCESS] Bound workspace to remote store", stdout_bind.getvalue()
                )
                self.assertIn(server.url, stdout_bind.getvalue())
                self.assertIn(AUTH.env, stdout_bind.getvalue())
                self.assertIn(
                    "Run 'aikito remote sync --dry-run' to preview",
                    stdout_bind.getvalue(),
                )

                # Re-binding ws_a should fail
                stderr_bind2 = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stderr", stderr_bind2),
                    patch.dict(os.environ, ENV),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    cmd_remote_bind(args_bind_a)
                self.assertEqual(ctx.exception.code, 1)
                self.assertIn("Remote binding already exists", stderr_bind2.getvalue())

                # Bind ws_b
                args_bind_b = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", io.StringIO()),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind_b)

                # WS A: dry-run should show initial workspace resources to upload
                stdout_dry = io.StringIO()
                args_sync_dry = self.parser.parse_args(["remote", "sync", "--dry-run"])
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_dry),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_dry)
                self.assertIn(
                    "Remote sync plan (revision: 0) [preview]", stdout_dry.getvalue()
                )
                self.assertIn("Planned:   2", stdout_dry.getvalue())

                # WS A: Apply initial resources
                stdout_init_apply = io.StringIO()
                args_sync_apply = self.parser.parse_args(["remote", "sync", "--apply"])
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_init_apply),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_apply)
                self.assertIn("Applied:   2", stdout_init_apply.getvalue())

                # WS A: Now dry-run should be up to date
                stdout_up_to_date = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_up_to_date),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_dry)
                self.assertIn("Planned:   0", stdout_up_to_date.getvalue())

                # WS A: Add a new memory note and sync --apply
                note_a = ws_a / "memory/notes/idea.md"
                note_a.write_text("Hello from Machine A\n", encoding="utf-8")

                stdout_apply = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_apply),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_apply)

                self.assertIn("Remote sync result", stdout_apply.getvalue())
                self.assertIn("Applied:   1", stdout_apply.getvalue())
                self.assertNotIn("Remaining:", stdout_apply.getvalue())

                # WS B: dry-run should show 1 planned change
                stdout_dry_b = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_dry_b),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_dry)

                self.assertIn("Remote sync plan", stdout_dry_b.getvalue())
                self.assertIn("Planned:   1", stdout_dry_b.getvalue())
                self.assertFalse((ws_b / "memory/notes/idea.md").exists())

                # WS B: apply should fetch the note
                stdout_apply_b = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_apply_b),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_sync_apply)

                self.assertIn("Remote sync result", stdout_apply_b.getvalue())
                self.assertTrue((ws_b / "memory/notes/idea.md").exists())
                self.assertEqual(
                    (ws_b / "memory/notes/idea.md").read_text(encoding="utf-8"),
                    "Hello from Machine A\n",
                )

    def test_bind_auth_failures(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                # 401 unauthorized
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", "WRONG_TOKEN"]
                )
                stderr_401 = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stderr", stderr_401),
                    patch.dict(os.environ, {"WRONG_TOKEN": "bad-secret"}),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    cmd_remote_bind(args_bind)
                self.assertEqual(ctx.exception.code, 1)
                self.assertIn(
                    "Remote authentication failed (HTTP 401)", stderr_401.getvalue()
                )

    def test_sync_recovers_interrupted_round(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(1, "http://127.0.0.1:9/v1/remote", "a" * 32, AUTH),
            )

            args = self.parser.parse_args(["remote", "sync", "--apply"])
            stderr = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stderr", stderr),
                patch.dict(os.environ, ENV),
                patch(
                    "aikito.remote_cli.sync_workspace_remote",
                    side_effect=WorkspaceReconcileError(
                        "Recovered an interrupted round; run again"
                    ),
                ),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_sync(args)
            self.assertEqual(ctx.exception.code, 1)

    def test_sync_reports_individually_blocked_resource(self):
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(1, "https://example.com/v1/remote", "a" * 32, AUTH),
            )
            blocked_item = ReconcileItem(
                id="skill:secret_tool",
                action="BLOCKED",
                target=None,
                before=None,
                after=None,
                reason="Contains plaintext credential",
            )
            mock_plan = MagicMock()
            mock_plan.revision = 2
            mock_plan.changes = ()
            mock_plan.conflicts = ()
            mock_plan.findings = ()
            mock_plan.deferred = ()
            mock_plan.applied_items = ()
            mock_plan.rounds = 1
            mock_plan.stop_reason = None
            mock_plan.blocked = False
            mock_plan.blocked_items = (blocked_item,)
            mock_plan.items = (blocked_item,)

            args = self.parser.parse_args(["remote", "sync", "--apply"])
            stdout = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stdout", stdout),
                patch.dict(os.environ, ENV),
                patch(
                    "aikito.remote_cli.sync_workspace_remote",
                    return_value=mock_plan,
                ),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_sync(args)

            self.assertEqual(ctx.exception.code, 1)
            out = stdout.getvalue()
            self.assertIn("Blocked:   1", out)
            self.assertIn(
                "[BLOCKED] skill:secret_tool (Contains plaintext credential)", out
            )
            self.assertIn("Completed with blocked resources remaining", out)

    def test_sync_reports_conflicts_properly(self):
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(1, "https://example.com/v1/remote", "a" * 32, AUTH),
            )
            conflict_item = ReconcileItem(
                id="memory:notes/idea.md",
                action="CONFLICT",
                target=None,
                before=None,
                after=None,
                reason="Diverged from base",
            )
            applied_item = ReconcileItem(
                id="skill:safe_tool",
                action="CREATE",
                target=None,
                before=None,
                after=None,
                reason="Added remotely",
            )
            mock_plan = MagicMock()
            mock_plan.revision = 3
            mock_plan.changes = ()
            mock_plan.conflicts = (conflict_item,)
            mock_plan.findings = ()
            mock_plan.deferred = ()
            mock_plan.applied_items = (applied_item,)
            mock_plan.rounds = 1
            mock_plan.stop_reason = None
            mock_plan.blocked = False
            mock_plan.blocked_items = ()
            mock_plan.items = (conflict_item,)

            args = self.parser.parse_args(["remote", "sync", "--apply"])
            stdout = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stdout", stdout),
                patch.dict(os.environ, ENV),
                patch(
                    "aikito.remote_cli.sync_workspace_remote",
                    return_value=mock_plan,
                ),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_sync(args)

            self.assertEqual(ctx.exception.code, 1)
            out = stdout.getvalue()
            self.assertIn("Applied:   1", out)
            self.assertIn("Conflicts: 1", out)
            self.assertIn("[CONFLICT] memory:notes/idea.md (Diverged from base)", out)
            self.assertIn("Completed with conflicts remaining", out)

    def test_sync_reports_stop_reason(self):
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(1, "https://example.com/v1/remote", "a" * 32, AUTH),
            )
            mock_plan = MagicMock()
            mock_plan.revision = 1
            mock_plan.changes = ()
            mock_plan.conflicts = ()
            mock_plan.findings = ()
            mock_plan.deferred = ()
            mock_plan.applied_items = ()
            mock_plan.rounds = 5
            mock_plan.stop_reason = "Reconciliation round limit reached"
            mock_plan.blocked = False
            mock_plan.blocked_items = ()
            mock_plan.items = ()

            args = self.parser.parse_args(["remote", "sync", "--apply"])
            stdout = io.StringIO()
            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch("sys.stdout", stdout),
                patch.dict(os.environ, ENV),
                patch(
                    "aikito.remote_cli.sync_workspace_remote",
                    return_value=mock_plan,
                ),
                self.assertRaises(SystemExit) as ctx,
            ):
                cmd_remote_sync(args)

            self.assertEqual(ctx.exception.code, 1)
            out = stdout.getvalue()
            self.assertIn(
                "Synchronization stopped: Reconciliation round limit reached", out
            )


if __name__ == "__main__":
    unittest.main()
