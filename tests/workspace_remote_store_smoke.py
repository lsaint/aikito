"""Run the engine through only the portable store interface."""

from __future__ import annotations

import sys
from pathlib import Path

from smoke_assertions import (
    require_file,
)
from workspace_reconcile_backend import files
from workspace_reconcile_smoke import _workspace

from aikito.workspace.reconcile import build_reconcile_plan, run_reconciliation
from aikito.workspace.remote import REMOTE_STATE, FilesystemRemote
from aikito.workspace.remote_store import SnapshotExpired


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

    def commit(self, request):
        return self.__backend.commit(request)

    def resolve_commit(self, sync_id, client_id, request_id, mutation_digest):
        return self.__backend.resolve_commit(
            sync_id, client_id, request_id, mutation_digest
        )

    def recover(self):
        return self.__backend.recover()


class NoopGuard(StoreFacade):
    def __init__(self, backend):
        super().__init__(backend)
        self.reject_fetch = False

    def fetch(self, expected, ids):
        if self.reject_fetch:
            raise AssertionError("NOOP rounds must not fetch content")
        return super().fetch(expected, ids)


def exercise(base: Path):
    left, right = _workspace(base / "left"), _workspace(base / "right")
    backend = FilesystemRemote.create(base / "center")
    store = NoopGuard(backend)
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
    revision = store.read().revision
    assert not run_reconciliation(right, store, home, dry_run=False).changes
    assert store.read().revision == revision

    store.reject_fetch = True
    assert not build_reconcile_plan(right, store).changes
    assert not run_reconciliation(right, store, home, dry_run=False).changes
    verify_artifacts(base)


def verify_artifacts(base: Path) -> None:
    for relative in (
        "right/memory/notes/portable-store.md",
        "right/.local/state/aikito/workspace-reconcile/replica.json",
        "center/.local/state/aikito/workspace-reconcile/remote.json",
    ):
        require_file(base / relative)


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Portable RemoteStore engine checks passed")
