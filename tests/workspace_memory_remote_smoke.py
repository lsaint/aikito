"""Exercise existing engine scenarios through a center with no filesystem."""

from __future__ import annotations

import sys
from pathlib import Path

from smoke_assertions import (
    require_absent,
    require_file,
)
from workspace_reconcile_acceptance import exercise_behavior
from workspace_reconcile_backend import InMemoryBackend


def exercise(base: Path):
    backend = InMemoryBackend()
    assert not any(
        hasattr(backend.remote, name) for name in ("root", "lock", "content")
    )
    exercise_behavior(base, backend)
    assert not (base / "center").exists()
    assert backend.remote.recover() is False
    verify_artifacts(base)


def verify_artifacts(base: Path) -> None:
    for relative in (
        "moved-left/memory/notes/accepted-base.md",
        "right/capture-b/accepted-before-local.md",
        "right/capture-b/relocated.md",
        "moved-left/projects/demo/memory/notes/acceptance.md",
        "right/.local/state/aikito/workspace-reconcile/replica.json",
    ):
        require_file(base / relative)
    for relative in ("center",):
        require_absent(base / relative)


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] In-memory RemoteStore engine checks passed")
