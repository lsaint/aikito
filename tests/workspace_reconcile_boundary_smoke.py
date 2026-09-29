"""Exercise portable skill fingerprints and explicit legacy-state refusal."""

from __future__ import annotations

import json
import os
import stat
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE, REPLICA_STATE
from aikito.workspace.resources import SKILL_FINGERPRINT_SCHEME, snapshot_workspace
from aikito.workspace.remote_store import InvalidContent
from workspace_reconcile_backend import files
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


@contextmanager
def executable_view(enabled: bool):
    """Simulate POSIX/Windows permission views on every CI operating system."""
    original = Path.lstat

    def lstat(path, *args, **kwargs):
        result = original(path, *args, **kwargs)
        if (
            "center" in path.parts
            or path.name != "run.sh"
            or not stat.S_ISREG(result.st_mode)
        ):
            return result
        fields = list(result)
        mask = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
        fields[0] = result.st_mode | mask if enabled else result.st_mode & ~mask
        return os.stat_result(fields)

    with patch.object(Path, "lstat", lstat):
        yield


def exercise(base: Path) -> None:
    a, b = _workspace(base / "left"), _workspace(base / "right")
    remote = FilesystemRemote.create(base / "center")
    home = base / "home"
    write(a, "skills/portable/SKILL.md", "# Portable\n")
    write(a, "skills/portable/run.sh", "echo portable\n")
    (a / "skills/portable/empty").mkdir()

    def run(local):
        plan = run_reconciliation(local, remote, home, dry_run=False)
        assert not plan.blocked and not plan.conflicts
        return plan

    with executable_view(True):
        fingerprint = snapshot_workspace(a).resources["skill:portable"].fingerprint
        run(a)
    with executable_view(False):
        assert (
            snapshot_workspace(a).resources["skill:portable"].fingerprint == fingerprint
        )
        run(b)
        assert (
            snapshot_workspace(b).resources["skill:portable"].fingerprint == fingerprint
        )
        revision = remote.read().revision
        assert not run(b).changes
        assert remote.read().revision == revision
        write(b, "skills/portable/run.sh", "echo changed\n")
        run(b)
    with executable_view(True):
        run(a)
        revision = remote.read().revision
        assert not run(a).changes
        assert remote.read().revision == revision
    assert (a / "skills/portable/run.sh").read_bytes() == b"echo changed\n"
    assert (b / "skills/portable/empty").is_dir()

    # Ambiguous historical skill hashes are refused before any state advances.
    for path, preview, error in (
        (remote.root / REMOTE_STATE, remote.read, InvalidContent),
        (
            a / REPLICA_STATE,
            lambda: build_reconcile_plan(a, remote),
            WorkspaceReconcileError,
        ),
    ):
        original = path.read_bytes()
        state = json.loads(original)
        assert state.pop("skill_fingerprint") == SKILL_FINGERPRINT_SCHEME
        path.write_text(json.dumps(state), encoding="utf-8")
        before = files(a), files(b), files(remote.root)
        try:
            preview()
        except error as exc:
            assert "Unsupported skill fingerprint scheme" in str(exc)
        else:
            raise AssertionError("Ambiguous legacy skill state was accepted")
        assert before == (files(a), files(b), files(remote.root))
        path.write_bytes(original)
    run(a)
    run(b)


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Remote Store Boundary fingerprint checks passed")
