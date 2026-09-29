"""Exercise replica-center reconciliation through its internal API."""

from __future__ import annotations

import json
import os
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE, REPLICA_STATE


def _workspace(root: Path) -> Path:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects/demo/memory/notes",
        "skills",
        "global",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (root / "projects/demo/agent.toml").write_text('name = "demo"\n', encoding="utf-8")
    return root


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    left, right = _workspace(base / "left"), _workspace(base / "right")
    remote = FilesystemRemote.create(base / "center")
    home = base / "home"

    def apply(local: Path, **kwargs):
        return run_reconciliation(local, remote, home, dry_run=False, **kwargs)

    apply(left)
    apply(right)
    note = Path("memory/notes/reconcile-smoke.md")
    project_note = Path("projects/demo/memory/notes/reconcile-smoke.md")
    (left / note).write_text("# Local reconciliation\n", encoding="utf-8")
    (left / project_note).write_text("# Project reconciliation\n", encoding="utf-8")
    (left / "skills/reconcile-smoke/empty").mkdir(parents=True)
    (left / "skills/reconcile-smoke/SKILL.md").write_text(
        "# Example\n", encoding="utf-8"
    )
    preview = run_reconciliation(left, remote, home, dry_run=True)
    assert len(preview.changes) == 3 and not (right / note).exists()
    apply(left)
    apply(right)
    assert (right / note).read_bytes() == (left / note).read_bytes()
    assert (right / project_note).read_bytes() == (left / project_note).read_bytes()
    assert (right / "skills/reconcile-smoke/empty").is_dir()
    (right / note).write_text("# Updated\n", encoding="utf-8")
    (right / project_note).write_text("# Updated project\n", encoding="utf-8")
    (right / "skills/reconcile-smoke/SKILL.md").write_text(
        "# Updated skill\n", encoding="utf-8"
    )
    apply(right)
    apply(left)
    assert (left / note).read_text(encoding="utf-8") == "# Updated\n"
    assert (left / project_note).read_text(encoding="utf-8") == "# Updated project\n"
    assert (left / "skills/reconcile-smoke/SKILL.md").read_text(
        encoding="utf-8"
    ) == "# Updated skill\n"

    conflict = Path("memory/notes/conflict.md")
    (left / conflict).write_text("base", encoding="utf-8")
    apply(left)
    apply(right)
    (left / conflict).write_text("local", encoding="utf-8")
    (right / conflict).write_text("remote", encoding="utf-8")
    apply(right)
    (left / "memory/notes/safe.md").write_text("safe", encoding="utf-8")
    assert apply(left).conflicts
    assert (remote.root / "memory/notes/safe.md").exists()
    apply(left, resolutions={"memory:notes/conflict.md": "remote"})
    assert (left / conflict).read_text(encoding="utf-8") == "remote"
    apply(right)

    for local in (left, right):
        revision = remote.read().revision
        assert not apply(local).changes
        assert remote.read().revision == revision

    for relative in (
        Path("memory/notes/deleted.md"),
        Path("projects/demo/memory/notes/deleted.md"),
    ):
        (left / relative).write_text("delete", encoding="utf-8")
    (left / "skills/deleted").mkdir()
    (left / "skills/deleted/SKILL.md").write_text("# Delete\n", encoding="utf-8")
    apply(left)
    apply(right)
    (left / "memory/notes/deleted.md").unlink()
    (left / "projects/demo/memory/notes/deleted.md").unlink()
    shutil.rmtree(left / "skills/deleted")
    apply(left)
    apply(right)
    assert not (right / "memory/notes/deleted.md").exists()
    assert not (right / "projects/demo/memory/notes/deleted.md").exists()
    assert not (right / "skills/deleted").exists()

    (left / "memory/notes/recovered.md").write_text("recover", encoding="utf-8")
    original_replace = os.replace

    def interrupt(src, dst):
        if (
            "stage" in Path(src).parts
            and Path(dst) == remote.root / "memory/notes/recovered.md"
        ):
            raise KeyboardInterrupt
        original_replace(src, dst)

    try:
        with patch("aikito.workspace.transactions.os.replace", side_effect=interrupt):
            apply(left)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("Expected an interrupted center write")
    try:
        apply(left)
    except WorkspaceReconcileError as exc:
        assert "Recovered an interrupted" in str(exc)
    else:
        raise AssertionError("Expected recovery before replay")
    apply(left)
    apply(right)
    assert (right / "memory/notes/recovered.md").read_text(
        encoding="utf-8"
    ) == "recover"

    old_state = json.loads((left / REPLICA_STATE).read_text(encoding="utf-8"))
    shutil.move(left, base / "moved-left")
    shutil.move(remote.root, base / "moved-center")
    left = base / "moved-left"
    remote = FilesystemRemote(base / "moved-center")
    (left / "memory/notes/relocated.md").write_text("relocated", encoding="utf-8")
    apply(left)
    apply(right)
    new_state = json.loads((left / REPLICA_STATE).read_text(encoding="utf-8"))
    assert old_state["sync_id"] == new_state["sync_id"]
    assert old_state["replica_id"] == new_state["replica_id"]
    assert not build_reconcile_plan(right, remote).changes
    assert (remote.root / REMOTE_STATE).is_file()
    print("[SUCCESS] Resource reconciliation smoke passed")


if __name__ == "__main__":
    main()
