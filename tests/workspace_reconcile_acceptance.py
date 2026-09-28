"""End-to-end acceptance of two independent replicas through one resource center."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import (
    FilesystemRemote,
    LOCAL_CONFIG,
    REPLICA_STATE,
    SYNC_KINDS,
)
from aikito.workspace.resources import snapshot_workspace
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


def _files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _resources(root: Path) -> dict[str, str]:
    return {
        identity: resource.fingerprint
        for identity, resource in snapshot_workspace(root).resources.items()
        if resource.kind in SYNC_KINDS
        and not (resource.kind == "config" and resource.name in LOCAL_CONFIG)
    }


def exercise(base: Path) -> None:
    a, b = _workspace(base / "left"), _workspace(base / "right")
    remote = FilesystemRemote.create(base / "center")
    home = base / "home"

    def apply(local: Path, **kwargs):
        plan = run_reconciliation(local, remote, home, dry_run=False, **kwargs)
        assert not plan.blocked, plan.findings
        return plan

    def converge():
        for local in (a, b, a):
            assert not apply(local).conflicts
        fingerprints = {
            key: value.fingerprint for key, value in remote.read().resources.items()
        }
        assert _resources(a) == _resources(b) == fingerprints
        for local, prefix in ((a, "capture-a"), (b, "capture-b")):
            config = tomllib.loads((local / "config.toml").read_text(encoding="utf-8"))
            assert config["inbox"]["path"] == prefix
        assert "config:inbox.path" not in fingerprints

    for local, prefix in ((a, "capture-a"), (b, "capture-b")):
        write(local, "config.toml", f'[inbox]\npath = "{prefix}"\n')
        assert not apply(local).conflicts
    payloads = {
        "memory/notes/acceptance.md": "first",
        "projects/demo/memory/notes/acceptance.md": "first",
        "capture-a/deep/acceptance.md": "first",
        "global/AGENTS.md": "first",
        "projects/demo/AGENTS.md": "first",
        "skills/example/SKILL.md": "# Example\n",
        "agents/custom.toml": '[agents.custom]\nvalue = "first"\n',
        "mcps/docs.toml": 'agents = ["custom"]\ncommand = "first"\n',
        "subagents/review.md": '---\ndescription: "Review"\nagents: ["custom"]\n---\nfirst\n',
        "skills.toml": 'skills = ["example", "aikito"]\n',
        "projects/demo/agent.toml": 'name = "demo"\npaths = ["~/offline"]\nskills = ["example"]\n',
        "config.toml": '[inbox]\npath = "capture-a"\n[memory]\nstale_days = 10\n[update]\ncheck = true\n',
    }
    for relative, value in payloads.items():
        write(a, relative, value)
    before = _files(a), _files(b), _files(remote.root)
    preview = build_reconcile_plan(a, remote)
    assert preview.changes and not preview.conflicts and not preview.blocked
    assert before == (_files(a), _files(b), _files(remote.root))
    converge()
    assert {identity.partition(":")[0] for identity in _resources(a)} == SYNC_KINDS

    # Identical concurrent shared-field edits confirm bases without another upload.
    for local in (a, b):
        text = (local / "config.toml").read_text(encoding="utf-8")
        write(local, "config.toml", text.replace("true", "false"))
    apply(b)
    generation = remote.read().generation
    plan = apply(a)
    assert (
        next(item for item in plan.items if item.id == "config:update.check").action
        == "NOOP"
    )
    assert remote.read().generation == generation
    converge()

    # Conflicts retain their old base while independent additions still converge.
    ancestor = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))["base"][
        "config:memory.stale_days"
    ]
    for local, days in ((a, 20), (b, 30)):
        text = (local / "config.toml").read_text(encoding="utf-8")
        write(local, "config.toml", text.replace("10", str(days)))
    apply(b)
    write(a, "capture-a/safe.md", "safe")
    plan = apply(a)
    assert {item.id for item in plan.conflicts} == {"config:memory.stale_days"}
    assert (
        json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))["base"][
            "config:memory.stale_days"
        ]
        == ancestor
    )
    assert (remote.root / "inbox/safe.md").read_text(encoding="utf-8") == "safe"
    apply(b)
    assert (b / "capture-b/safe.md").read_text(encoding="utf-8") == "safe"
    assert not apply(a, resolutions={"config:memory.stale_days": "local"}).conflicts
    converge()

    # A stale local plan and a stale center generation both fail without any writes.
    write(a, "memory/notes/pending.md", "pending")
    plan = build_reconcile_plan(a, remote)
    write(a, "projects/demo/AGENTS.md", "after preview")
    before = _files(a), _files(b), _files(remote.root)
    try:
        apply_reconcile_plan(plan, home)
    except WorkspaceReconcileError as exc:
        assert "changed after planning" in str(exc)
    else:
        raise AssertionError("A stale local plan was accepted")
    assert before == (_files(a), _files(b), _files(remote.root))
    plan = build_reconcile_plan(a, remote)
    write(b, "global/AGENTS.md", "new generation")
    apply(b)
    before = _files(a), _files(b), _files(remote.root)
    try:
        apply_reconcile_plan(plan, home)
    except WorkspaceReconcileError as exc:
        assert "changed after planning" in str(exc)
    else:
        raise AssertionError("A stale center plan was accepted")
    assert before == (_files(a), _files(b), _files(remote.root))
    converge()

    # Recover a shared-file download together with its unchanged replica base.
    text = (b / "config.toml").read_text(encoding="utf-8")
    write(b, "config.toml", text.replace("20", "40"))
    apply(b)
    before = _files(a)
    original = os.replace

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == a / "config.toml":
            original(src, dst)
            raise KeyboardInterrupt
        original(src, dst)

    try:
        with patch("aikito.workspace.transactions.os.replace", side_effect=interrupt):
            apply(a)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("Expected shared-file interruption")
    try:
        apply(a)
    except WorkspaceReconcileError as exc:
        assert "Recovered an interrupted" in str(exc)
    else:
        raise AssertionError("Expected recovery before replay")
    assert _files(a) == before
    converge()

    # Removal propagates from B; moving completed roots retains pairing and paths.
    (b / "capture-b/safe.md").unlink()
    apply(b)
    converge()
    assert not (a / "capture-a/safe.md").exists()
    identity = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))
    shutil.move(a, base / "moved-left")
    shutil.move(remote.root, base / "moved-center")
    a = base / "moved-left"
    remote = FilesystemRemote(base / "moved-center")
    write(a, "capture-a/relocated.md", "relocated")
    converge()
    assert (b / "capture-b/relocated.md").read_text(encoding="utf-8") == "relocated"
    state = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))
    assert (state["sync_id"], state["replica_id"]) == (
        identity["sync_id"],
        identity["replica_id"],
    )
    write(b, "projects/demo/memory/notes/acceptance.md", "final")
    converge()
    assert (a / "projects/demo/memory/notes/acceptance.md").read_text(
        encoding="utf-8"
    ) == "final"
    before = _files(a), _files(b), _files(remote.root)
    for local in (a, b, a, b):
        plan = apply(local)
        assert not plan.changes and not plan.conflicts
    assert before == (_files(a), _files(b), _files(remote.root))


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Workspace reconciliation acceptance passed")
