"""Backend injection for reconciliation behavior, separate from storage checks.

This is an acceptance driver over the portable RemoteStore protocol. Physical
center paths are used only for filesystem-specific checkpoints.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.reconcile import (
    ReconcilePlan,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.serialized_remote import SerializedRemoteStore


def files(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


class ReconciliationBackend(Protocol):
    def run(self, local: Path, home: Path, **kwargs) -> ReconcilePlan: ...

    def plan(self, local: Path) -> ReconcilePlan: ...

    def apply(self, plan: ReconcilePlan, home: Path) -> None: ...

    def fingerprints(self) -> dict[str, str]: ...

    def revision(self) -> int: ...

    def checkpoint(self) -> object:
        """Capture storage state to detect unintended writes."""
        ...


class StoreBackend:
    def __init__(self, remote):
        self.remote = remote

    def run(self, local: Path, home: Path, **kwargs) -> ReconcilePlan:
        return run_reconciliation(local, self.remote, home, dry_run=False, **kwargs)

    def plan(self, local: Path) -> ReconcilePlan:
        return build_reconcile_plan(local, self.remote)

    def apply(self, plan: ReconcilePlan, home: Path) -> None:
        apply_reconcile_plan(plan, home, remote=self.remote)

    def fingerprints(self) -> dict[str, str]:
        return {
            key: value.fingerprint
            for key, value in self.remote.read().resources.items()
        }

    def revision(self) -> int:
        return self.remote.read().revision


class FilesystemBackend(StoreBackend):
    def __init__(self, root: Path):
        super().__init__(FilesystemRemote.create(root))

    def checkpoint(self) -> object:
        return files(self.remote.root)


class InMemoryBackend(StoreBackend):
    def __init__(self):
        super().__init__(InMemoryRemote())

    def checkpoint(self) -> object:
        snapshot = self.remote.read()
        return snapshot, self.remote.fetch(snapshot, tuple(snapshot.resources))


class SerializedFilesystemBackend(StoreBackend):
    """Filesystem center reached only through real protocol bytes."""

    def __init__(self, root: Path):
        backend = FilesystemRemote.create(root)
        super().__init__(SerializedRemoteStore(LoopbackTransport(backend).exchange))

    def checkpoint(self) -> object:
        snapshot = self.remote.read()
        return snapshot, self.remote.fetch(snapshot, tuple(snapshot.resources))


class SerializedInMemoryBackend(StoreBackend):
    """Memory center reached only through real protocol bytes."""

    def __init__(self):
        backend = InMemoryRemote()
        super().__init__(SerializedRemoteStore(LoopbackTransport(backend).exchange))

    def checkpoint(self) -> object:
        snapshot = self.remote.read()
        return snapshot, self.remote.fetch(snapshot, tuple(snapshot.resources))


BACKEND_FACTORIES = {
    "filesystem": FilesystemBackend,
    "memory": lambda root: InMemoryBackend(),
    "serialized-filesystem": SerializedFilesystemBackend,
    "serialized-memory": lambda root: SerializedInMemoryBackend(),
}
