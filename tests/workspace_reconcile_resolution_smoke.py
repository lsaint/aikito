"""Exercise reference-conflict choices and rejected non-conflict choices."""

from __future__ import annotations

import shutil
import sys
import tomllib
from pathlib import Path

from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


def exercise(base: Path, identity: str, side: str) -> None:
    a, b = _workspace(base / "left"), _workspace(base / "right")
    remote = FilesystemRemote.create(base / "center")
    home = base / "home"

    def apply(local: Path, **kwargs):
        plan = run_reconciliation(local, remote, home, dry_run=False, **kwargs)
        assert not plan.blocked
        return plan

    write(a, "skills/x/SKILL.md", "# X\n")
    apply(a)
    apply(b)
    shutil.rmtree(a / "skills/x")
    apply(a)
    write(
        b,
        "projects/newcomer/agent.toml",
        'name = "newcomer"\nskills = ["x", "aikito"]\n',
    )
    assert {i.id for i in apply(b).conflicts} == {"skill:x", "project-skill:newcomer/x"}
    plan = run_reconciliation(
        b, remote, home, dry_run=True, resolutions={identity: side}
    )
    assert not plan.conflicts and not plan.blocked
    assert (b / "skills/x/SKILL.md").is_file()
    assert not (remote.root / "skills/x").exists()
    assert not apply(b, resolutions={identity: side}).conflicts
    for local in (a, b):
        assert not apply(local).conflicts
        assert (local / "skills/x/SKILL.md").exists() == (side == "local")
        skills = tomllib.loads(
            (local / "projects/newcomer/agent.toml").read_text(encoding="utf-8")
        )["skills"]
        assert set(skills) == ({"aikito", "x"} if side == "local" else {"aikito"})
        assert not build_reconcile_plan(local, remote).changes
        assert not build_reconcile_plan(local, remote).conflicts
    assert (remote.root / "skills/x/SKILL.md").exists() == (side == "local")

    write(b, "memory/notes/rejected.md", "new")
    generation = remote.read().generation
    try:
        apply(b, resolutions={"memory:notes/rejected.md": "remote"})
    except WorkspaceReconcileError as exc:
        assert "requires a conflicting resource" in str(exc)
    else:
        raise AssertionError("Non-conflicting choice was silently accepted")
    assert remote.read().generation == generation
    assert not (remote.root / "memory/notes/rejected.md").exists()
    assert (b / "memory/notes/rejected.md").read_text(encoding="utf-8") == "new"


if __name__ == "__main__":
    base = Path(sys.argv[1]).resolve()
    for kind in ("skill", "project-skill"):
        identity = "skill:x" if kind == "skill" else "project-skill:newcomer/x"
        for side in ("local", "remote"):
            exercise(base / f"{kind}-{side}", identity, side)
    print("[SUCCESS] Reference conflict resolution smoke passed")
