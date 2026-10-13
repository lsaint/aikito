"""Tests for auto-sync worker lifecycle lock, watcher, scheduler, and conflict resolution."""

import io
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch

from http_remote_server import HTTPRemoteServer
from workspace_reconcile_smoke import _workspace

from aikito.cli_parser import build_parser
from aikito.remote_cli import cmd_remote_bind, cmd_remote_sync, cmd_remote_watch
from aikito.workspace.auto_sync import (
    AutoSyncLifecycleLock,
    AutoSyncScheduler,
    AutoSyncState,
    AutoSyncWorkerRunning,
    LocalWatcher,
    _is_network_transient_error,
    archive_conflict_loser,
    plan_conflict_resolutions,
    prune_conflict_archives,
)
from aikito.workspace.payload import (
    FilePayload,
    TomlPayload,
    TreeEntry,
    TreePayload,
)
from aikito.workspace.reconcile import WorkspaceReconcileError
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)
from aikito.workspace.remote_store import StoreUnavailable
from aikito.workspace.remote_transport import TransportNotDelivered
from aikito.workspace.resource_state import SYNC_CONFLICTS_DIR, WORKER_LOCK_STATE

TOKEN = "test-auto-sync-token"
AUTH = RemoteAuth("bearer_env", "AIKITO_AUTO_SYNC_TOKEN")
ENV = {AUTH.env: TOKEN}


class AutoSyncLifecycleLockTest(unittest.TestCase):
    def test_single_lock_acquisition_and_release(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            lock1 = AutoSyncLifecycleLock(ws)
            lock1.acquire()
            self.assertTrue((ws / WORKER_LOCK_STATE).exists())

            # Second acquisition in same process fails
            lock2 = AutoSyncLifecycleLock(ws)
            with self.assertRaises(AutoSyncWorkerRunning):
                lock2.acquire()

            lock1.release()

            # Now lock2 can acquire
            lock2.acquire()
            lock2.release()

    def test_lock_context_manager(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            with AutoSyncLifecycleLock(ws):
                lock2 = AutoSyncLifecycleLock(ws)
                with self.assertRaises(AutoSyncWorkerRunning):
                    lock2.acquire()


class AutoSyncConflictResolutionTest(unittest.TestCase):
    def test_archive_conflict_loser_local(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            note = ws / "memory/notes/local.md"
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("Local content to archive\n", encoding="utf-8")

            archived = archive_conflict_loser(
                ws, "memory:notes/local.md", "local", timestamp="20261009-120000"
            )
            self.assertEqual(archived.status, "archived")
            self.assertIsNotNone(archived.path)
            self.assertTrue(archived.path.exists())
            self.assertIn(
                ".local/sync-conflicts/20261009-120000/memory/notes/local.md",
                archived.path.as_posix(),
            )
            self.assertEqual(
                archived.path.read_text(encoding="utf-8"),
                "Local content to archive\n",
            )

    def test_archive_conflict_loser_remote(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            mock_remote = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.resources = {"memory:notes/remote.md": MagicMock()}
            mock_payload = FilePayload(b"Remote payload bytes\n")
            mock_remote.fetch.return_value = {"memory:notes/remote.md": mock_payload}

            archived = archive_conflict_loser(
                ws,
                "memory:notes/remote.md",
                "remote",
                remote_store=mock_remote,
                remote_snapshot=mock_snapshot,
                timestamp="20261009-120000",
            )
            self.assertEqual(archived.status, "archived")
            self.assertIsNotNone(archived.path)
            self.assertTrue(archived.path.exists())
            self.assertEqual(archived.path.read_bytes(), b"Remote payload bytes\n")

    def test_archive_conflict_loser_remote_tree(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            mock_remote = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.resources = {"skill:my-skill": MagicMock()}
            tree_payload = TreePayload(
                (
                    TreeEntry("SKILL.md", b"---\nname: my-skill\n---\n"),
                    TreeEntry("sub/helper.py", b"def run(): pass\n"),
                )
            )
            mock_remote.fetch.return_value = {"skill:my-skill": tree_payload}

            archived = archive_conflict_loser(
                ws,
                "skill:my-skill",
                "remote",
                remote_store=mock_remote,
                remote_snapshot=mock_snapshot,
                timestamp="20261009-120000",
            )
            self.assertEqual(archived.status, "archived")
            self.assertIsNotNone(archived.path)
            self.assertTrue(archived.path.is_dir())
            self.assertTrue((archived.path / "SKILL.md").exists())
            self.assertEqual(
                (archived.path / "SKILL.md").read_bytes(),
                b"---\nname: my-skill\n---\n",
            )
            self.assertTrue((archived.path / "sub/helper.py").exists())
            self.assertEqual(
                (archived.path / "sub/helper.py").read_bytes(), b"def run(): pass\n"
            )

    def test_archive_conflict_loser_remote_toml(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            mock_remote = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.resources = {"config:sync.mode": MagicMock()}
            toml_payload = TomlPayload(path=("sync", "mode"), text='value = "auto"\n')
            mock_remote.fetch.return_value = {"config:sync.mode": toml_payload}

            archived = archive_conflict_loser(
                ws,
                "config:sync.mode",
                "remote",
                remote_store=mock_remote,
                remote_snapshot=mock_snapshot,
                timestamp="20261009-120000",
            )
            self.assertEqual(archived.status, "archived")
            self.assertIsNotNone(archived.path)
            self.assertTrue(archived.path.is_file())
            self.assertEqual(
                archived.path.read_text(encoding="utf-8"), 'value = "auto"\n'
            )

    def test_archive_conflict_loser_no_content_when_deleted(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            # Local side deleted
            res_local = archive_conflict_loser(ws, "memory:notes/deleted.md", "local")
            self.assertEqual(res_local.status, "no_content")

            # Remote side deleted
            mock_remote = MagicMock()
            mock_snapshot = MagicMock()
            mock_snapshot.resources = {}
            res_remote = archive_conflict_loser(
                ws,
                "memory:notes/deleted.md",
                "remote",
                remote_store=mock_remote,
                remote_snapshot=mock_snapshot,
            )
            self.assertEqual(res_remote.status, "no_content")

    def test_archive_conflict_loser_failure_skips_resolution(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            mock_remote = MagicMock()
            mock_remote.fetch.side_effect = Exception("Fetch failed")
            mock_plan = MagicMock()
            mock_item = MagicMock()
            mock_item.id = "memory:notes/conflict.md"
            mock_plan.conflicts = [mock_item]
            mock_plan.local_snapshot.resources = {mock_item.id: MagicMock()}
            mock_plan.remote_snapshot.resources = {mock_item.id: MagicMock()}

            resolutions, details = plan_conflict_resolutions(mock_plan, ws, mock_remote)
            # Archiving failed so item must NOT be resolved/overwritten
            self.assertNotIn(mock_item.id, resolutions)
            self.assertEqual(details[mock_item.id].archive_status, "failed")
            self.assertIn("Fetch failed", details[mock_item.id].error)

    def test_network_transient_error_classification(self):
        err_transient = WorkspaceReconcileError("Wrapped store error")
        err_transient.__cause__ = StoreUnavailable("Network down")
        self.assertTrue(_is_network_transient_error(err_transient))

        err_transport = TransportNotDelivered("Cannot connect")
        self.assertTrue(_is_network_transient_error(err_transport))

        err_conn = ConnectionResetError("Reset")
        self.assertTrue(_is_network_transient_error(err_conn))

        err_other = WorkspaceReconcileError("Logical error")
        self.assertFalse(_is_network_transient_error(err_other))

    def test_prune_conflict_archives(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            conflicts_dir = ws / SYNC_CONFLICTS_DIR
            for i in range(1, 6):
                (conflicts_dir / f"20261009-00000{i}").mkdir(
                    parents=True, exist_ok=True
                )

            prune_conflict_archives(ws, max_retained=3)
            remaining = sorted(p.name for p in conflicts_dir.iterdir() if p.is_dir())
            self.assertEqual(len(remaining), 3)
            self.assertEqual(
                remaining,
                ["20261009-000003", "20261009-000004", "20261009-000005"],
            )


class LocalWatcherTest(unittest.TestCase):
    def test_local_watcher_debounced_event(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            events = []
            watcher = LocalWatcher(
                ws,
                on_dirty=lambda: events.append(time.time()),
                debounce_delay=0.05,
                scan_interval=0.05,
            )
            watcher.start()
            try:
                # Trigger multiple writes
                note = ws / "memory/notes/watch.md"
                note.write_text("First edit\n", encoding="utf-8")
                time.sleep(0.01)
                note.write_text("Second edit\n", encoding="utf-8")

                # Wait for debounce to expire
                deadline = time.time() + 3.0
                while not events and time.time() < deadline:
                    time.sleep(0.05)
                self.assertGreaterEqual(len(events), 1)
            finally:
                watcher.stop()


class AutoSyncEndToEndTest(unittest.TestCase):
    def setUp(self):
        self.parser = build_parser()

    def test_scheduler_does_not_busy_loop_on_failure(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            home = Path(tmp_dir) / "home"
            mock_remote = MagicMock()
            scheduler = AutoSyncScheduler(
                ws,
                mock_remote,
                home,
                poll_interval=0.05,
                fallback_scan_interval=10.0,
            )
            step_calls = 0

            def failing_step():
                nonlocal step_calls
                step_calls += 1
                raise StoreUnavailable("Simulated network outage")

            scheduler.step = failing_step

            # Trigger a local event
            scheduler.notify_dirty()

            # Start run in background thread
            t = threading.Thread(target=scheduler.run, daemon=True)
            t.start()
            try:
                # Wait 0.3s: without fix, this would loop hundreds of thousands of times.
                # With proper backoff, call count must be strictly bounded.
                time.sleep(0.3)
                self.assertLessEqual(step_calls, 3)
            finally:
                scheduler.stop()
                t.join(timeout=1.0)

    def test_scheduler_bidirectional_sync(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind)

                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind)

                # Initialize WS A and WS B via sync --apply
                args_apply = self.parser.parse_args(["remote", "sync", "--apply"])
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_apply)

                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_sync(args_apply)

                # WS A writes a note
                note_a = ws_a / "memory/notes/auto-note.md"
                note_a.write_text("Created on A\n", encoding="utf-8")

                # Scheduler A steps once
                args_watch = self.parser.parse_args(["remote", "watch", "--once"])
                stdout_watch_a = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_watch_a),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                self.assertIn("[SYNC] (rev 1) push memory:notes/auto-note.md", stdout_watch_a.getvalue())

                # Scheduler B steps once
                stdout_watch_b = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_watch_b),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                self.assertIn("[SYNC] (rev 2) pull memory:notes/auto-note.md", stdout_watch_b.getvalue())
                note_b = ws_b / "memory/notes/auto-note.md"
                self.assertTrue(note_b.exists())
                self.assertEqual(note_b.read_text(encoding="utf-8"), "Created on A\n")

    def test_scheduler_auto_resolves_conflicts(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                for ws in (ws_a, ws_b):
                    with (
                        patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                        patch("pathlib.Path.home", return_value=home),
                        patch.dict(os.environ, ENV),
                    ):
                        cmd_remote_bind(args_bind)

                args_apply = self.parser.parse_args(["remote", "sync", "--apply"])
                for ws in (ws_a, ws_b):
                    with (
                        patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                        patch("pathlib.Path.home", return_value=home),
                        patch.dict(os.environ, ENV),
                    ):
                        cmd_remote_sync(args_apply)

                # Both create same file with different content
                (ws_a / "memory/notes/conflict.md").write_text(
                    "Content from A\n", encoding="utf-8"
                )
                (ws_b / "memory/notes/conflict.md").write_text(
                    "Content from B\n", encoding="utf-8"
                )

                # Sync A first
                args_watch = self.parser.parse_args(
                    ["remote", "watch", "--once", "--verbose"]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                # Now sync B: B encounters conflict and resolves it automatically (B local wins)
                stdout_b = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_b),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                out_b = stdout_b.getvalue()
                self.assertIn("[CONFLICT] Auto-resolved", out_b)
                self.assertIn("local won", out_b)
                # Verify loser (remote A's content) was archived on B
                conflicts_dir = ws_b / SYNC_CONFLICTS_DIR
                self.assertTrue(conflicts_dir.exists())
                archived_files = list(conflicts_dir.glob("**/conflict.md"))
                self.assertGreaterEqual(len(archived_files), 1)
                self.assertEqual(
                    archived_files[0].read_text(encoding="utf-8"), "Content from A\n"
                )

    def test_scheduler_auto_resolves_delete_vs_modify(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws_a = _workspace(base / "ws_a")
            ws_b = _workspace(base / "ws_b")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                for ws in (ws_a, ws_b):
                    with (
                        patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                        patch("pathlib.Path.home", return_value=home),
                        patch.dict(os.environ, ENV),
                    ):
                        cmd_remote_bind(args_bind)

                # Initialize note on both
                (ws_a / "memory/notes/delmod.md").write_text(
                    "Initial content\n", encoding="utf-8"
                )
                args_apply = self.parser.parse_args(["remote", "sync", "--apply"])
                for ws in (ws_a, ws_b):
                    with (
                        patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                        patch("pathlib.Path.home", return_value=home),
                        patch.dict(os.environ, ENV),
                    ):
                        cmd_remote_sync(args_apply)

                # Direction: A deletes note, B modifies note
                (ws_a / "memory/notes/delmod.md").unlink()
                (ws_b / "memory/notes/delmod.md").write_text(
                    "Modified by B\n", encoding="utf-8"
                )

                # Sync A (applies delete to remote)
                args_watch = self.parser.parse_args(
                    ["remote", "watch", "--once", "--verbose"]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                # Sync B (B has modified, remote has deleted -> B local modification wins!)
                stdout_b = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_b),
                    patch("pathlib.Path.home", return_value=home),
                    patch("sys.stdout", stdout_b),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                out_b = stdout_b.getvalue()
                self.assertIn("[CONFLICT] Auto-resolved", out_b)
                self.assertIn("local won", out_b)
                self.assertTrue((ws_b / "memory/notes/delmod.md").exists())
                self.assertEqual(
                    (ws_b / "memory/notes/delmod.md").read_text(encoding="utf-8"),
                    "Modified by B\n",
                )

                # Sync A again: A pulls the preserved modification from remote
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws_a),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_watch(args_watch)

                self.assertTrue((ws_a / "memory/notes/delmod.md").exists())
                self.assertEqual(
                    (ws_a / "memory/notes/delmod.md").read_text(encoding="utf-8"),
                    "Modified by B\n",
                )

    def test_watch_rejects_second_worker(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = _workspace(Path(tmp_dir) / "ws")
            save_remote_binding(
                ws,
                RemoteBinding(1, "https://example.com/v1/remote", "a" * 32, AUTH),
            )
            lock = AutoSyncLifecycleLock(ws)
            lock.acquire()
            try:
                args = self.parser.parse_args(["remote", "watch", "--once"])
                stderr = io.StringIO()
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                    patch("sys.stderr", stderr),
                    patch.dict(os.environ, ENV),
                    self.assertRaises(SystemExit) as ctx,
                ):
                    cmd_remote_watch(args)

                self.assertEqual(ctx.exception.code, 75)
                self.assertIn(
                    "Another auto-sync worker is already running", stderr.getvalue()
                )
            finally:
                lock.release()

    def test_step_recovers_interrupted_pending_commit(self):
        from aikito.workspace.http_transport import HTTPTransport
        from aikito.workspace.pending_commit import PendingCommitStore
        from aikito.workspace.reconcile import run_reconciliation
        from aikito.workspace.remote_protocol import Operation
        from aikito.workspace.serialized_remote import SerializedRemoteStore

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                remote = SerializedRemoteStore(
                    HTTPTransport(
                        server.url,
                        authorization=f"Bearer {TOKEN}",
                        timeout=0.2,
                    ).exchange
                )
                run_reconciliation(ws, remote, home, dry_run=False)

                # Write a note and trigger an interrupted commit
                note = ws / "memory/notes/interrupted.md"
                note.write_text("Interrupted content\n", encoding="utf-8")
                server.arm("drop_after_handler", operation=Operation.COMMIT)
                with self.assertRaises(WorkspaceReconcileError):
                    run_reconciliation(ws, remote, home, dry_run=False)

                # Verify pending commit exists
                pending = PendingCommitStore(ws, home).load()
                self.assertIsNotNone(pending)

                # AutoSyncScheduler.step() recovers the pending commit and succeeds
                scheduler = AutoSyncScheduler(ws, remote, home)
                result = scheduler.step()
                self.assertIsNotNone(result)

                # Verify pending commit is cleared
                self.assertIsNone(PendingCommitStore(ws, home).load())
                self.assertEqual(scheduler.state, AutoSyncState.IDLE)

    def test_step_handles_blocked_resources_and_sets_attention(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind)

                # Create a file containing a secret to trigger BLOCKED
                secret_note = ws / "memory/notes/secret.md"
                secret_note.write_text(
                    "api_key: 12345678901234567890\n", encoding="utf-8"
                )

                from aikito.remote_service import open_bound_remote

                remote = open_bound_remote(ws, environ=ENV)
                scheduler = AutoSyncScheduler(ws, remote, home)
                result = scheduler.step()

                self.assertGreater(len(result.blocked_items), 0)
                self.assertEqual(scheduler.state, AutoSyncState.ATTENTION_REQUIRED)

    def test_scheduler_deduplicates_blocked_logging(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"
            backend = FilesystemRemote.create(base / "center")

            with HTTPRemoteServer(backend, token=TOKEN) as server:
                args_bind = self.parser.parse_args(
                    ["remote", "bind", server.url, "--token-env", AUTH.env]
                )
                with (
                    patch("aikito.remote_cli.resolve_workspace", return_value=ws),
                    patch("pathlib.Path.home", return_value=home),
                    patch.dict(os.environ, ENV),
                ):
                    cmd_remote_bind(args_bind)

                secret_note = ws / "memory/notes/secret.md"
                secret_note.write_text(
                    "api_key: 12345678901234567890\n", encoding="utf-8"
                )

                from aikito.remote_service import open_bound_remote

                remote = open_bound_remote(ws, environ=ENV)
                logged = []
                scheduler = AutoSyncScheduler(ws, remote, home, logger=logged.append)

                # Round 1: Logs [BLOCKED]
                scheduler.step()
                blocked_logs = [msg for msg in logged if msg.startswith("[BLOCKED]")]
                self.assertEqual(len(blocked_logs), 1)

                # Round 2: Unchanged state, suppresses duplicate log
                scheduler.step()
                blocked_logs_2 = [msg for msg in logged if msg.startswith("[BLOCKED]")]
                self.assertEqual(len(blocked_logs_2), 1)

                # Remove the secret file: Round 3 should clear and log info
                secret_note.unlink()
                scheduler.step()
                info_cleared = [msg for msg in logged if "cleared" in msg]
                self.assertEqual(len(info_cleared), 1)

    def test_get_installed_aikito_version_and_diff(self):
        import subprocess

        from aikito.workspace.auto_sync import (
            check_installed_version_differs,
            get_installed_aikito_version,
        )

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout="aikito 1.60.0\n"
            )
            v = get_installed_aikito_version()
            self.assertEqual(v, "1.60.0")

            # Same version -> None
            diff_same = check_installed_version_differs(current_version="1.60.0")
            self.assertIsNone(diff_same)

            # Different version -> returns new version
            diff_new = check_installed_version_differs(current_version="1.58.2")
            self.assertEqual(diff_new, "1.60.0")

    def test_scheduler_version_check_triggers_reexec(self):
        from aikito.workspace.auto_sync import is_worker_running

        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"

            logged = []
            scheduler = AutoSyncScheduler(
                ws,
                None,
                home,
                poll_interval=0.05,
                version_check_interval=0.01,
                logger=logged.append,
            )

            with (
                patch(
                    "aikito.workspace.auto_sync.check_installed_version_differs",
                    return_value="1.59.0",
                ),
                patch.object(
                    scheduler,
                    "_resolve_reexec_command",
                    return_value=["aikito", "remote", "watch", "--workspace", str(ws)],
                ),
                patch.object(scheduler, "_do_reexec") as mock_reexec,
            ):
                scheduler.run()

                self.assertTrue(scheduler._reexec_requested)
                self.assertEqual(scheduler._reexec_target_version, "1.59.0")
                mock_reexec.assert_called_once()
                reload_logs = [
                    msg
                    for msg in logged
                    if "Detected updated Aikito version 1.59.0" in msg
                ]
                self.assertEqual(len(reload_logs), 1)

                # Verify lifecycle lock was released
                running, _ = is_worker_running(ws)
                self.assertFalse(running)

    def test_scheduler_version_check_skips_reload_when_binary_invalid(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"

            logged = []
            scheduler = AutoSyncScheduler(
                ws,
                None,
                home,
                poll_interval=0.05,
                version_check_interval=0.01,
                logger=logged.append,
            )

            # Let it run 3 checks with same failed version, then stop
            call_count = 0

            def mock_diff():
                nonlocal call_count
                call_count += 1
                if call_count <= 3:
                    return "1.59.0"
                scheduler.stop()
                return None

            with (
                patch(
                    "aikito.workspace.auto_sync.check_installed_version_differs",
                    side_effect=mock_diff,
                ),
                patch.object(
                    scheduler, "_resolve_reexec_command", return_value=None
                ) as mock_resolve,
                patch.object(scheduler, "_do_reexec") as mock_reexec,
            ):
                scheduler.run()

                self.assertFalse(scheduler._reexec_requested)
                mock_reexec.assert_not_called()
                # Should resolve and warn ONLY ONCE across multiple loop iterations
                mock_resolve.assert_called_once_with("1.59.0")
                warn_logs = [
                    msg
                    for msg in logged
                    if "[WARN] Detected updated version 1.59.0 but candidate executable is invalid; skipping reload"
                    in msg
                ]
                self.assertEqual(len(warn_logs), 1)

    def test_resolve_and_do_reexec(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"

            logged = []
            scheduler = AutoSyncScheduler(
                ws,
                None,
                home,
                poll_interval=10.0,
                debounce_delay=1.0,
                version_check_interval=120.0,
                verbose=True,
                logger=logged.append,
            )

            # 1. Test _resolve_reexec_command
            with (
                patch(
                    "aikito.workspace.service.get_aikito_command",
                    return_value=[
                        "aikito",
                        "remote",
                        "watch",
                        "--workspace",
                        str(ws),
                    ],
                ),
                patch(
                    "aikito.workspace.service.is_matching_aikito_command",
                    return_value=True,
                ),
            ):
                resolved = scheduler._resolve_reexec_command("1.59.0")
                self.assertIsNotNone(resolved)
                assert resolved is not None
                self.assertEqual(resolved[0], "aikito")
                self.assertIn("watch", resolved)
                self.assertIn("--poll-interval", resolved)
                self.assertIn("10.0", resolved)
                self.assertIn("--debounce-ms", resolved)
                self.assertIn("1000", resolved)
                self.assertIn("--version-check-interval", resolved)
                self.assertIn("120.0", resolved)
                self.assertIn("--verbose", resolved)

            # 2. Test _do_reexec
            with (
                patch("os.execv") as mock_execv,
                patch("sys.exit"),
            ):
                scheduler._do_reexec(resolved)
                mock_execv.assert_called_once_with(resolved[0], resolved)

    def test_do_reexec_windows_output_redirection(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"

            scheduler = AutoSyncScheduler(ws, None, home)
            cmd = ["aikito", "remote", "watch", "--workspace", str(ws)]

            with (
                patch("aikito.workspace.auto_sync.is_windows", return_value=True),
                patch("subprocess.Popen") as mock_popen,
                patch("sys.exit"),
            ):
                scheduler._do_reexec(cmd)
                mock_popen.assert_called_once()
                _, kwargs = mock_popen.call_args
                self.assertIn("stdout", kwargs)
                self.assertIn("stderr", kwargs)
                self.assertIsNotNone(kwargs["stdout"])
                self.assertIs(kwargs["stdout"], kwargs["stderr"])
                kwargs["stdout"].close()

    def test_scheduler_logs_connection_restored_upon_recovery(self):
        with tempfile.TemporaryDirectory() as tmp_dir:
            base = Path(tmp_dir)
            ws = _workspace(base / "ws")
            home = base / "home"

            logged = []
            scheduler = AutoSyncScheduler(
                ws,
                None,
                home,
                poll_interval=0.01,
                version_check_interval=0,
                logger=logged.append,
            )

            calls = []

            def flaky_step():
                calls.append(1)
                if len(calls) == 1:
                    raise ConnectionError("Temporary connection failure")
                scheduler.stop()
                res = Mock()
                res.remote_snapshot.revision = 1
                return res

            with patch.object(scheduler, "step", side_effect=flaky_step):
                # Trigger wake event after brief pause to avoid waiting for backoff
                def wake_quickly():
                    time.sleep(0.02)
                    scheduler._wake_event.set()

                waker = threading.Thread(target=wake_quickly)
                waker.start()
                scheduler.run()
                waker.join()

                warn_logs = [msg for msg in logged if "[WARN] Connection unavailable" in msg]
                self.assertEqual(len(warn_logs), 1)

                restored_logs = [msg for msg in logged if "[INFO] Connection restored" in msg]
                self.assertEqual(len(restored_logs), 1)


if __name__ == "__main__":
    unittest.main()
