"""Full reconciliation acceptance across a pure bytes boundary."""

from __future__ import annotations

import sys
from pathlib import Path

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
    (base / "checked.txt").write_text(
        "Serialized acceptance passed\n", encoding="utf-8"
    )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Serialized remote acceptance passed")
