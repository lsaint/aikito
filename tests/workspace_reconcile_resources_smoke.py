"""Exercise all resource kinds and shared-file recovery through real invocations."""

from __future__ import annotations

import os
import shutil
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from workspace_reconcile_smoke import _workspace


def write(root: Path, relative: str, value: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.replace("\r\n", "\n"), encoding="utf-8", newline="\n")


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    a, b = _workspace(base / "left"), _workspace(base / "right")
    remote = FilesystemRemote.create(base / "center")
    home = base / "home"

    def apply(local, **kwargs):
        plan = run_reconciliation(local, remote, home, dry_run=False, **kwargs)
        assert not plan.blocked and not plan.conflicts
        return plan

    for local, prefix in ((a, "capture-a"), (b, "capture-b")):
        write(local, "config.toml", f'[inbox]\npath = "{prefix}"\n')
        apply(local)
    write(a, "capture-a/deep/note.md", "first")
    write(a, "global/AGENTS.md", "first")
    write(a, "projects/demo/AGENTS.md", "first")
    write(a, "agents/custom.toml", '[agents.custom]\nvalue = "first"\n')
    write(a, "mcps/docs.toml", 'agents = ["custom"]\ncommand = "first"\n')
    write(
        a,
        "subagents/review.md",
        '---\ndescription: "Review"\nagents: ["custom"]\n---\nfirst\n',
    )
    write(a, "skills/example/SKILL.md", "# Example\n")
    write(a, "skills.toml", 'skills = ["example", "aikito"]\n')
    write(
        a,
        "projects/demo/agent.toml",
        'name = "demo"\npaths = ["~/offline-a"]\nskills = ["example"]\nsync_mode = "copy"\n',
    )
    write(
        a,
        "config.toml",
        '[inbox]\npath = "capture-a"\n[memory]\nstale_days = 10\n[update]\ncheck = true\n',
    )
    apply(a)
    apply(b)
    assert (b / "capture-b/deep/note.md").read_text(encoding="utf-8") == "first"
    assert tomllib.loads((b / "projects/demo/agent.toml").read_text(encoding="utf-8"))[
        "paths"
    ] == ["~/offline-a"]
    write(b, "capture-b/deep/note.md", "second")
    write(b, "global/AGENTS.md", "second")
    write(b, "projects/demo/AGENTS.md", "second")
    write(b, "agents/custom.toml", '[agents.custom]\nvalue = "second"\n')
    write(b, "mcps/docs.toml", 'agents = ["custom"]\ncommand = "second"\n')
    write(
        b,
        "subagents/review.md",
        '---\ndescription: "Review"\nagents: ["custom"]\n---\nsecond\n',
    )
    write(
        b,
        "projects/demo/agent.toml",
        'name = "updated"\npaths = ["~/offline-b"]\nskills = ["aikito"]\n',
    )
    write(b, "skills.toml", 'skills = ["aikito"]\n')
    write(b, "config.toml", '[inbox]\npath = "capture-b"\n[memory]\nstale_days = 20\n')
    apply(b)
    before = (a / "config.toml").read_bytes()
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
        raise AssertionError("Expected interrupted round recovery")
    assert (a / "config.toml").read_bytes() == before
    apply(a)
    assert tomllib.loads((a / "config.toml").read_text(encoding="utf-8")) == {
        "inbox": {"path": "capture-a"},
        "memory": {"stale_days": 20},
    }
    assert tomllib.loads(
        (a / "projects/demo/agent.toml").read_text(encoding="utf-8")
    ) == {"name": "updated", "paths": ["~/offline-b"], "skills": ["aikito"]}
    for relative in (
        "global/AGENTS.md",
        "projects/demo/AGENTS.md",
        "agents/custom.toml",
        "mcps/docs.toml",
        "subagents/review.md",
    ):
        assert (a / relative).read_bytes() == (b / relative).read_bytes()
    # Delete every standalone kind and a complete project without affecting survivors.
    for relative in (
        "inbox/deleted.md",
        "global/AGENTS.md",
        "projects/retired/AGENTS.md",
        "agents/deleted.toml",
        "mcps/deleted.toml",
        "subagents/deleted.md",
    ):
        value = (
            '[agents.deleted]\nvalue = "delete"\n'
            if relative == "agents/deleted.toml"
            else 'command = "delete"\n'
            if relative == "mcps/deleted.toml"
            else '---\ndescription: "delete"\nagents: ["custom"]\n---\nDelete\n'
            if relative == "subagents/deleted.md"
            else "delete"
        )
        write(a, relative.replace("inbox/", "capture-a/"), value)
    write(
        a,
        "projects/retired/agent.toml",
        'name = "retired"\npaths = ["~/retired"]\nskills = ["aikito"]\n',
    )
    apply(a)
    apply(b)
    for relative in (
        "capture-a/deleted.md",
        "agents/deleted.toml",
        "mcps/deleted.toml",
        "subagents/deleted.md",
    ):
        (a / relative).unlink()
    shutil.rmtree(a / "projects/retired")
    # Restore the survivor global instructions after demonstrating its update.
    (a / "global/AGENTS.md").unlink()
    apply(a)
    apply(b)
    assert not (b / "global/AGENTS.md").exists()
    write(a, "global/AGENTS.md", "second")
    apply(a)
    apply(b)
    assert not build_reconcile_plan(b, remote).changes
    assert "config:inbox.path" not in remote.read().resources
    assert not (remote.root / "config.toml").exists()
    assert not (remote.root / "projects/demo/agent.toml").exists()
    print("[SUCCESS] Expanded resource reconciliation smoke passed")


if __name__ == "__main__":
    main()
