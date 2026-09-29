"""Run the engine through only the portable store interface."""

from __future__ import annotations

import sys
from pathlib import Path

from aikito.workspace.reconcile import build_reconcile_plan, run_reconciliation
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE
from aikito.workspace.remote_store import SnapshotExpired
from workspace_reconcile_backend import files
from workspace_reconcile_smoke import _workspace


class StoreFacade:
    """Expose the protocol without any filesystem lifecycle internals."""

    __slots__ = ("__backend",)

    def __init__(self, backend):
        self.__backend = backend

    def validate_replica(self, local):
        self.__backend.validate_replica(local)

    def read(self):
        return self.__backend.read()

    def fetch(self, expected, ids):
        return self.__backend.fetch(expected, ids)

    def commit(self, expected, mutations):
        return self.__backend.commit(expected, mutations)

    def recover(self):
        return self.__backend.recover()


def exercise(base: Path):
    left, right = _workspace(base / "left"), _workspace(base / "right")
    backend = FilesystemRemote.create(base / "center")
    store = StoreFacade(backend)
    home = base / "home"
    assert not any(hasattr(store, name) for name in ("root", "lock", "content"))
    run_reconciliation(left, store, home, dry_run=False)
    run_reconciliation(right, store, home, dry_run=False)
    old = store.read()
    (left / "memory/notes/portable-store.md").write_text(
        "portable store\n", encoding="utf-8"
    )
    run_reconciliation(left, store, home, dry_run=False)
    before = files(right), files(backend.root)
    try:
        store.fetch(old, [])
    except SnapshotExpired:
        pass
    else:
        raise AssertionError("Stale empty fetch succeeded")
    assert before == (files(right), files(backend.root))
    lock = (backend.root / REMOTE_STATE).parent / "remote.lock"
    lock.unlink(missing_ok=True)
    before = files(right), files(backend.root)
    plan = build_reconcile_plan(right, store)
    store.fetch(plan.remote_snapshot, list(plan.remote_snapshot.resources))
    assert before == (files(right), files(backend.root))
    run_reconciliation(right, store, home, dry_run=False)
    assert (right / "memory/notes/portable-store.md").read_bytes() == (
        left / "memory/notes/portable-store.md"
    ).read_bytes()
    generation = store.read().generation
    assert not run_reconciliation(right, store, home, dry_run=False).changes
    assert store.read().generation == generation


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Portable RemoteStore engine checks passed")
