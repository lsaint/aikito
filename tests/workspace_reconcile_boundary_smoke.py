"""Exercise portable skill mode sync and explicit legacy-state refusal."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE, REPLICA_STATE
from aikito.workspace.resources import SKILL_FINGERPRINT_SCHEME, snapshot_workspace
from aikito.workspace.skill_metadata import (
    read_executable_metadata,
    write_executable_metadata,
)
from aikito.workspace.remote_store import InvalidContent
from workspace_reconcile_backend import files
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


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

    fingerprint = snapshot_workspace(a).resources["skill:portable"].fingerprint
    run(a)
    run(b)
    script = a / "skills/portable/run.sh"
    if os.name == "nt":
        write_executable_metadata(script.parent / ".aikito-executable.json", ["run.sh"])
    else:
        script.chmod(script.stat().st_mode | 0o111)
    updated = snapshot_workspace(a).resources["skill:portable"]
    assert updated.fingerprint == fingerprint
    assert (
        updated.mode_fingerprint
        != snapshot_workspace(b).resources["skill:portable"].mode_fingerprint
    )
    assert [item.id for item in build_reconcile_plan(a, remote).changes] == [
        "skill:portable"
    ]
    run(a)
    run(b)
    assert (
        snapshot_workspace(b).resources["skill:portable"].mode_fingerprint
        == updated.mode_fingerprint
    )
    write(b, "skills/portable/run.sh", "echo changed\n")
    run(b)
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
    if os.name == "nt":
        (b / "skills/portable/run.sh").unlink()
        assert not build_reconcile_plan(b, remote).blocked
        run(b)
        run(a)
        assert not (a / "skills/portable/run.sh").exists()
        assert not read_executable_metadata(
            a / "skills/portable/.aikito-executable.json"
        )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Remote Store Boundary mode checks passed")
