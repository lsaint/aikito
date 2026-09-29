"""Verify revision persistence and read-only observations across a real round."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from aikito.skill_state import (
    ProjectSkillStateDocument,
    SkillStateRecord,
    WorkspaceWriterLock,
    get_binding_hash,
    get_skill_state_dir,
    load_project_skill_state,
    save_project_skill_state,
)
from aikito.workspace.reconcile import build_reconcile_plan, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.resource_state import REMOTE_STATE, REPLICA_STATE
from workspace_remote_store_smoke import exercise as exercise_portable_store


def exercise(base: Path):
    exercise_portable_store(base)
    left, right, home = base / "left", base / "right", base / "home"
    remote = FilesystemRemote(base / "center")
    center_path = remote.root / REMOTE_STATE
    replica_path = right / REPLICA_STATE
    before_revision = remote.read().revision
    before = {}
    for path in (center_path, replica_path):
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["revision"] == before_revision
        before[path] = path.read_bytes()
    assert remote.read().revision == before_revision
    assert remote.fetch(remote.read(), ()) == {}
    assert remote.commit(remote.read(), ()).revision == before_revision
    plan = build_reconcile_plan(right, remote)
    assert plan.revision == before_revision
    assert plan.state.revision == before_revision
    assert not plan.changes
    assert all(path.read_bytes() == contents for path, contents in before.items())
    (left / "memory/notes/revision.md").write_text(
        "revision persistence\n", encoding="utf-8"
    )
    run_reconciliation(left, remote, home, dry_run=False)
    run_reconciliation(right, remote, home, dry_run=False)
    revision = remote.read().revision
    assert revision == before_revision + 1
    for path in (center_path, replica_path):
        state = json.loads(path.read_text(encoding="utf-8"))
        assert state["revision"] == revision
    assert (right / "memory/notes/revision.md").read_bytes() == (
        left / "memory/notes/revision.md"
    ).read_bytes()
    assert not run_reconciliation(right, remote, home, dry_run=False).changes
    assert remote.read().revision == revision
    with WorkspaceWriterLock(home):
        skill_state = ProjectSkillStateDocument(
            version=1,
            revision=0,
            workspace_root=left.as_posix(),
            project_name="revision",
            physical_checkout=right.as_posix(),
            records={
                "revision": SkillStateRecord(
                    "revision", "copy", "active", "v1:" + "0" * 64, "write", True
                )
            },
        )
        ok, error = save_project_skill_state(home, skill_state)
        assert ok, error
        loaded, error = load_project_skill_state(home, left, "revision", right)
        assert error is None and loaded is not None and loaded.revision == 1
        binding = get_binding_hash(left, "revision", right)
        state_path = get_skill_state_dir(home) / f"{binding}.json"
        before = state_path.read_bytes()
        assert json.loads(before)["revision"] == 1
        ok, error = save_project_skill_state(home, skill_state, expected_revision=0)
        assert not ok and "revision mismatch" in error
        assert state_path.read_bytes() == before
        legacy = json.loads(before)
        legacy["generation"] = legacy.pop("revision")
        state_path.write_text(json.dumps(legacy), encoding="utf-8")
        legacy_bytes = state_path.read_bytes()
        loaded, error = load_project_skill_state(home, left, "revision", right)
        assert error is None and loaded is not None and loaded.revision == 1
        assert state_path.read_bytes() == legacy_bytes
        ok, error = save_project_skill_state(home, loaded, expected_revision=0)
        assert not ok and "revision mismatch" in error
        assert state_path.read_bytes() == legacy_bytes
        ok, error = save_project_skill_state(home, loaded, expected_revision=1)
        assert ok, error
        persisted = json.loads(state_path.read_text(encoding="utf-8"))
        assert persisted["revision"] == 2 and "generation" not in persisted


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Revision state persistence checks passed")
