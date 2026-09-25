"""Exercise the private workspace reconciliation API with synthetic workspaces."""

from __future__ import annotations

import sys
from pathlib import Path

from aikito.workspace_reconcile import baseline_workspaces, run_reconciliation


def _workspace(root: Path) -> Path:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects",
        "skills",
        "global",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    return root


def main() -> None:
    base = Path(sys.argv[1]).resolve()
    left, right = _workspace(base / "left"), _workspace(base / "right")
    home = base / "home"
    baseline_workspaces(left, right, home)
    note = Path("memory/notes/reconcile-smoke.md")
    (left / note).write_text("# Local reconciliation\n", encoding="utf-8")
    preview = run_reconciliation(left, right, home, dry_run=True)
    assert len(preview.changes) == 1 and not (right / note).exists()
    run_reconciliation(left, right, home, dry_run=False)
    assert (right / note).read_bytes() == (left / note).read_bytes()


if __name__ == "__main__":
    main()
