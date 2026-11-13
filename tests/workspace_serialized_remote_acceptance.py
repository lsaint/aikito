"""Full reconciliation acceptance across a pure bytes boundary."""

from __future__ import annotations

import sys
from pathlib import Path

from smoke_assertions import (
    require_absent,
    require_file,
)
from workspace_reconcile_acceptance import exercise_behavior
from workspace_reconcile_backend import (
    SerializedFilesystemBackend,
    SerializedInMemoryBackend,
)


def exercise(base: Path) -> None:
    memory = base / "memory"
    backend = SerializedInMemoryBackend()
    assert not hasattr(backend.remote, "root")
    exercise_behavior(memory, backend)
    assert not (memory / "center").exists()
    assert backend.remote.recover() is False
    filesystem = base / "filesystem"
    exercise_behavior(filesystem, SerializedFilesystemBackend(filesystem / "center"))
    verify_artifacts(base)
    (base / "checked.txt").write_text(
        "Serialized acceptance passed\n", encoding="utf-8"
    )


def verify_artifacts(base: Path) -> None:
    for relative in (
        "memory/moved-left/projects/demo/memory/notes/acceptance.md",
        "filesystem/moved-left/projects/demo/memory/notes/acceptance.md",
        "memory/right/capture-b/relocated.md",
        "filesystem/right/capture-b/relocated.md",
    ):
        require_file(base / relative)
    for relative in ("memory/center",):
        require_absent(base / relative)


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Serialized remote acceptance passed")
