"""Tests for 'aikito remote' CLI commands (bind and sync)."""

from workspace_memory_remote import InMemoryRemote
from aikito.workspace.encrypted_sync import initialize_encrypted_workspace

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
            ["remote", "bind", "--endpoint", "https://example.com/v1/remote"]
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
                "--endpoint",
                "https://example.com/v1/remote",
                "--token-env",
                "CUSTOM_TOKEN",
            ]
        )
        self.assertEqual(args.token_env, "CUSTOM_TOKEN")

    def test_remote_bind_rebind_flag(self):
        args = self.parser.parse_args(
            [
                "remote",
                "bind",
                "--endpoint",
                "https://example.com/v1/remote",
                "--rebind",
            ]
        )
        self.assertTrue(args.rebind)

    def test_remote_unbind_args(self):
        args = self.parser.parse_args(["remote", "unbind", "--force"])
        self.assertEqual(args.command, "remote")
        self.assertEqual(args.remote_target, "unbind")
        self.assertTrue(args.force)

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

    def test_remote_sync_mode_defaults_to_apply(self):
        args = self.parser.parse_args(["remote", "sync"])
        self.assertFalse(args.dry_run)
        self.assertFalse(args.apply)

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
                ["remote", "bind", "--endpoint", "https://example.com/v1/remote"]
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
                [
                    "remote",
                    "bind",
                    "--endpoint",
                    "http://external.example.com/v1/remote",
                ]
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
                [
                    "remote",
                    "bind",
                    "--endpoint",
                    "https://example.com/v1/remote?token=secret",
                ]
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
            backend = InMemoryRemote("11223344556677889900aabbccddeeff")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind_a = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--token-env",
                        AUTH.env,
                    ]
                )
                stdout_bind = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_bind),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind_a)
                    if not backend.read().resources:
                        initialize_encrypted_workspace(ws_a, home, lambda code: None)

                self.assertIn(
                    "[SUCCESS] Synchronization connected.", stdout_bind.getvalue()
                )
                self.assertIn(
                    "aikito remote key initialize",
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
                    if not backend.read().resources:
                        initialize_encrypted_workspace(ws_a, home, lambda code: None)
                self.assertEqual(ctx.exception.code, 1)
                self.assertIn("Remote binding already exists", stderr_bind2.getvalue())

                # Bind ws_b
                args_bind_b = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--token-env",
                        AUTH.env,
                    ]
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
                    "Remote sync plan (revision: 1) [preview]", stdout_dry.getvalue()
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
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--token-env",
                        "WRONG_TOKEN",
                    ]
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
                id="global-memory:notes/idea.md",
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
            self.assertIn(
                "[CONFLICT] global-memory:notes/idea.md (Diverged from base)", out
            )
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

    def test_sync_without_flags_defaults_to_apply(self):
        import tempfile
        from unittest.mock import MagicMock

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(
                    1,
                    "https://example.com/v1/remote",
                    "11223344556677889900aabbccddeeff",
                    AUTH,
                ),
            )
            args = self.parser.parse_args(["remote", "sync"])
            mock_plan = MagicMock()
            mock_plan.revision = 1
            mock_plan.applied_items = ()
            mock_plan.changes = ()
            mock_plan.conflicts = ()
            mock_plan.blocked = ()
            mock_plan.blocked_items = ()
            mock_plan.stop_reason = None
            mock_plan.deferred = ()
            mock_plan.remaining_rounds = 0
            mock_plan.interrupted_round_recovered = False

            with (
                patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                patch(
                    "aikito.remote_cli.sync_workspace_remote",
                    return_value=mock_plan,
                ) as mock_sync,
                patch("sys.stdout", io.StringIO()),
            ):
                cmd_remote_sync(args)

            mock_sync.assert_called_once()
            self.assertFalse(mock_sync.call_args.kwargs.get("dry_run", True))

    def test_remote_unbind_command(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(
                    1,
                    "https://example.com/v1/remote",
                    "11223344556677889900aabbccddeeff",
                    AUTH,
                ),
            )
            args = self.parser.parse_args(["remote", "unbind"])
            stdout = io.StringIO()
            with (
                patch("aikito.remote_cli._resolve_cli_workspace", return_value=ws),
                patch("aikito.remote_cli.is_recognized_workspace", return_value=True),
                patch("sys.stdout", stdout),
            ):
                from aikito.remote_cli import cmd_remote_unbind

                cmd_remote_unbind(args)
            self.assertIn("Unbound workspace from remote store", stdout.getvalue())
            from aikito.workspace.remote_binding import load_remote_binding

            self.assertIsNone(load_remote_binding(ws))

    def test_bind_encrypted_store_with_recovery_fd(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home_a = base / "home_a"
            home_b = base / "home_b"
            backend = InMemoryRemote("223344556677889900aabbccddeeff00")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                # Step 1: ws_a initializes the store
                args_bind_a = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_a),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                env_a = dict(ENV)
                env_a["XDG_CONFIG_HOME"] = str(home_a / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_a),
                    patch.dict(os.environ, env_a, clear=False),
                    patch("sys.stdout", io.StringIO()),
                ):
                    cmd_remote_bind(args_bind_a)
                    codes = []
                    initialize_encrypted_workspace(ws_a, home_a, codes.append)
                    recovery_code = codes[0]

                # Step 2: ws_b binds using --recovery-fd
                r_fd, w_fd = os.pipe()
                os.write(w_fd, recovery_code.encode("ascii"))
                os.close(w_fd)

                args_bind_b = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_b),
                        "--token-env",
                        AUTH.env,
                        "--recovery-fd",
                        str(r_fd),
                    ]
                )
                stdout_b = io.StringIO()
                env_b = dict(ENV)
                env_b["XDG_CONFIG_HOME"] = str(home_b / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_b),
                    patch.dict(os.environ, env_b, clear=False),
                    patch("sys.stdout", stdout_b),
                ):
                    cmd_remote_bind(args_bind_b)
                os.close(r_fd)

                out = stdout_b.getvalue()
                self.assertIn(
                    "Synchronization key authorized and saved to device keystore", out
                )
                self.assertIn("Synchronization connected.", out)

    def test_bind_encrypted_store_interactive(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home_a = base / "home_a"
            home_b = base / "home_b"
            backend = InMemoryRemote("3344556677889900aabbccddeeff0011")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind_a = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_a),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                env_a = dict(ENV)
                env_a["XDG_CONFIG_HOME"] = str(home_a / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_a),
                    patch.dict(os.environ, env_a, clear=False),
                    patch("sys.stdout", io.StringIO()),
                ):
                    cmd_remote_bind(args_bind_a)
                    codes = []
                    initialize_encrypted_workspace(ws_a, home_a, codes.append)
                    recovery_code = codes[0]

                args_bind_b = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_b),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                stdout_b = io.StringIO()
                env_b = dict(ENV)
                env_b["XDG_CONFIG_HOME"] = str(home_b / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_b),
                    patch.dict(os.environ, env_b, clear=False),
                    patch("sys.stdin.isatty", return_value=True),
                    patch("getpass.getpass", return_value=recovery_code),
                    patch("sys.stdout", stdout_b),
                ):
                    cmd_remote_bind(args_bind_b)

                out = stdout_b.getvalue()
                self.assertIn(
                    "Synchronization key authorized and saved to device keystore", out
                )

    def test_bind_encrypted_store_non_interactive_without_fd_fails(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home_a = base / "home_a"
            home_b = base / "home_b"
            backend = InMemoryRemote("44556677889900aabbccddeeff001122")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind_a = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_a),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                env_a = dict(ENV)
                env_a["XDG_CONFIG_HOME"] = str(home_a / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_a),
                    patch.dict(os.environ, env_a, clear=False),
                    patch("sys.stdout", io.StringIO()),
                ):
                    cmd_remote_bind(args_bind_a)
                    codes = []
                    initialize_encrypted_workspace(ws_a, home_a, codes.append)

                args_bind_b = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws_b),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                stderr_b = io.StringIO()
                env_b = dict(ENV)
                env_b["XDG_CONFIG_HOME"] = str(home_b / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home_b),
                    patch.dict(os.environ, env_b, clear=False),
                    patch("sys.stdin.isatty", return_value=False),
                    patch("sys.stderr", stderr_b),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    cmd_remote_bind(args_bind_b)

                self.assertEqual(ctx.exception.code, 1)
                self.assertIn("Recovery code is required", stderr_b.getvalue())

    def test_bind_empty_store_interactive_auto_initializes(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = InMemoryRemote("556677889900aabbccddeeff00112233")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws),
                        "--token-env",
                        AUTH.env,
                    ]
                )
                stdout = io.StringIO()
                stdout.isatty = lambda: True
                env = dict(ENV)
                env["XDG_CONFIG_HOME"] = str(home / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home),
                    patch.dict(os.environ, env, clear=False),
                    patch("sys.stdout", stdout),
                    patch("sys.stdin.isatty", return_value=True),
                    patch("builtins.input", return_value="y"),
                ):
                    cmd_remote_bind(args)

                out = stdout.getvalue()
                self.assertIn("Initializing end-to-end encryption", out)
                self.assertIn("SYNC RECOVERY CODE", out)
                self.assertIn("Device key created and saved to device keystore", out)
                self.assertIn("format:encrypted-v1", backend.read().resources)

    def test_bind_empty_store_with_recovery_output_fd(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = InMemoryRemote("6677889900aabbccddeeff0011223344")

            r_fd, w_fd = os.pipe()
            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws),
                        "--token-env",
                        AUTH.env,
                        "--recovery-output-fd",
                        str(w_fd),
                    ]
                )
                stdout = io.StringIO()
                env = dict(ENV)
                env["XDG_CONFIG_HOME"] = str(home / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home),
                    patch.dict(os.environ, env, clear=False),
                    patch("sys.stdout", stdout),
                ):
                    cmd_remote_bind(args)

                os.close(w_fd)
                with os.fdopen(r_fd, "rb") as stream:
                    code = stream.read().decode("ascii").strip()

                self.assertTrue(len(code) > 10)
                out = stdout.getvalue()
                self.assertIn("Device key created and saved to device keystore", out)
                self.assertIn("format:encrypted-v1", backend.read().resources)

    def test_bind_empty_store_with_no_initialize(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = InMemoryRemote("77889900aabbccddeeff001122334455")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args = self.parser.parse_args(
                    [
                        "remote",
                        "bind",
                        "--endpoint",
                        server.url,
                        "--workspace",
                        str(ws),
                        "--token-env",
                        AUTH.env,
                        "--no-initialize",
                    ]
                )
                stdout = io.StringIO()
                stdout.isatty = lambda: True
                env = dict(ENV)
                env["XDG_CONFIG_HOME"] = str(home / ".config")
                with (
                    patch("aikito.remote_cli.Path.home", return_value=home),
                    patch.dict(os.environ, env, clear=False),
                    patch("sys.stdout", stdout),
                ):
                    cmd_remote_bind(args)

                out = stdout.getvalue()
                self.assertNotIn("Device key created", out)
                self.assertIn("aikito remote key initialize", out)
                self.assertEqual(len(backend.read().resources), 0)


if __name__ == "__main__":
    unittest.main()
