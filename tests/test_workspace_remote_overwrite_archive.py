"""A local UPDATE must not silently replace a remote version it has never seen."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workspace_reconcile_smoke import _workspace

from aikito.workspace.auto_sync import AutoSyncScheduler
from aikito.workspace.reconcile import build_reconcile_plan, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.resource_state import SYNC_CONFLICTS_DIR

RELATIVE = Path("projects/demo/memory/notes/preflight.md")
IDENTITY = "project-memory:demo/notes/preflight.md"


class RemoteOverwriteArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        self.device = _workspace(root / "device")
        self.writer = _workspace(root / "writer")
        self.home = root / "home"
        self.remote = FilesystemRemote.create(root / "store")
        (self.writer / RELATIVE).write_text("Base\n", encoding="utf-8")
        self.apply(self.writer)
        self.apply(self.device)

    def apply(self, workspace):
        return run_reconciliation(workspace, self.remote, self.home, dry_run=False)

    def remote_update(self, text):
        (self.writer / RELATIVE).write_text(text, encoding="utf-8")
        self.apply(self.writer)

    def step(self):
        return AutoSyncScheduler(self.device, self.remote, self.home).step()

    def archived_texts(self):
        return [
            path.read_text(encoding="utf-8")
            for path in (self.device / SYNC_CONFLICTS_DIR).glob("**/preflight.md")
        ]

    def test_remote_version_unseen_by_device_is_archived_before_overwrite(self):
        # Device has applied Base, remote advanced, then a stale buffer is saved.
        self.remote_update("Delegated update\n")
        self.step()
        (self.device / RELATIVE).write_text("Stale buffer\n", encoding="utf-8")

        plan = build_reconcile_plan(self.device, self.remote)
        item = next(item for item in plan.items if item.id == IDENTITY)
        self.assertEqual((item.action, item.target), ("UPDATE", "remote"))
        self.step()

        # The overwritten remote content must be recoverable locally...
        self.assertEqual(self.archived_texts(), ["Delegated update\n"])
        # ...and the local edit still converges to the remote.
        self.apply(self.writer)
        self.assertEqual((self.writer / RELATIVE).read_text(), "Stale buffer\n")

    def test_local_edit_archives_the_replaced_remote_version(self):
        # Normal editing also overwrites remote; the replaced Base stays recoverable.
        (self.device / RELATIVE).write_text("Local edit\n", encoding="utf-8")
        self.step()
        self.assertEqual(self.archived_texts(), ["Base\n"])
        self.apply(self.writer)
        self.assertEqual((self.writer / RELATIVE).read_text(), "Local edit\n")

    def test_archive_failure_aborts_before_overwrite(self):
        self.remote_update("Delegated update\n")
        self.step()
        (self.device / RELATIVE).write_text("Stale buffer\n", encoding="utf-8")
        remote_before = self.remote.read()
        with (
            patch.object(self.remote, "fetch", side_effect=OSError("disk full")),
            self.assertRaises(OSError),
        ):
            self.step()
        # Nothing was committed: remote still holds the unarchived version.
        self.assertEqual(self.remote.read(), remote_before)
        self.assertEqual(self.archived_texts(), [])


if __name__ == "__main__":
    unittest.main()
