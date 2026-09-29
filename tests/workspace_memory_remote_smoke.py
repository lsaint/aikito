"""Exercise existing engine scenarios through a center with no filesystem."""

from __future__ import annotations

import sys
from pathlib import Path

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


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] In-memory RemoteStore engine checks passed")
