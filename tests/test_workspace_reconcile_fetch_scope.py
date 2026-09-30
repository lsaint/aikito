"""Planning must not turn a metadata snapshot into a full content download."""

from __future__ import annotations

from pathlib import Path

import pytest

from aikito.workspace.reconcile import build_reconcile_plan, run_reconciliation
from aikito.workspace.remote import FilesystemRemote
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from workspace_remote_store_smoke import StoreFacade


class RecordingStore(StoreFacade):
    def __init__(self, backend):
        super().__init__(backend)
        self.requests = []

    def fetch(self, expected, ids):
        batch = tuple(ids)
        self.requests.append(batch)
        return super().fetch(expected, batch)


@pytest.fixture(params=("filesystem", "memory"))
def replicas(request, tmp_path):
    backend = (
        FilesystemRemote.create(tmp_path / "center")
        if request.param == "filesystem"
        else InMemoryRemote()
    )
    store = RecordingStore(backend)
    left, right = _workspace(tmp_path / "left"), _workspace(tmp_path / "right")
    return left, right, store, tmp_path / "home"


def _apply(local: Path, store: RecordingStore, home: Path) -> None:
    run_reconciliation(local, store, home, dry_run=False)


def test_sixty_resource_noop_round_fetches_nothing(replicas):
    left, right, store, home = replicas
    for index in range(60):
        (left / f"memory/notes/{index:02}.md").write_text(str(index))
    _apply(left, store, home)
    _apply(right, store, home)
    assert len(store.read().resources) >= 60
    store.requests.clear()
    revision = store.read().revision
    assert not build_reconcile_plan(left, store).changes
    assert not build_reconcile_plan(right, store).changes
    _apply(right, store, home)
    assert store.requests == []
    assert store.read().revision == revision


def test_plan_fetches_only_the_changed_download(replicas):
    left, right, store, home = replicas
    for index in range(60):
        (left / f"memory/notes/{index:02}.md").write_text(str(index))
    _apply(left, store, home)
    _apply(right, store, home)
    (left / "memory/notes/00.md").write_text("updated")
    _apply(left, store, home)
    store.requests.clear()
    plan = build_reconcile_plan(right, store)
    assert {item.id for item in plan.changes} == {"memory:notes/00.md"}
    assert store.requests == [("memory:notes/00.md",)]


def test_deletion_only_plan_fetches_nothing(replicas):
    left, right, store, home = replicas
    (left / "memory/notes/deleted.md").write_text("remove")
    (left / "config.toml").write_text("[feature]\nflag = true\n")
    _apply(left, store, home)
    _apply(right, store, home)
    assert "config:feature.flag" in store.read().resources
    (left / "memory/notes/deleted.md").unlink()
    (left / "config.toml").unlink()
    store.requests.clear()
    plan = build_reconcile_plan(left, store)
    assert {item.id for item in plan.changes} == {
        "memory:notes/deleted.md",
        "config:feature.flag",
    }
    assert store.requests == []


def test_shared_config_change_fetches_only_its_own_fields(replicas):
    left, right, store, home = replicas
    (left / "memory/notes/unrelated.md").write_text("unchanged")
    (left / "config.toml").write_text(
        '"feature.flag" = true\n[update]\ncheck = false\n'
    )
    _apply(left, store, home)
    _apply(right, store, home)
    (left / "config.toml").write_text(
        '"feature.flag" = false\n[update]\ncheck = false\n'
    )
    _apply(left, store, home)
    store.requests.clear()
    plan = build_reconcile_plan(right, store)
    assert "config:feature.flag" in {item.id for item in plan.changes}
    assert all(item.id.startswith("config:") for item in plan.changes)
    assert store.requests == [("config:feature.flag", "config:update.check")]


def test_resolution_fetches_only_newly_chosen_download(replicas):
    left, right, store, home = replicas
    note = "memory/notes/conflict.md"
    (left / note).write_text("base")
    _apply(left, store, home)
    _apply(right, store, home)
    (left / note).write_text("local")
    (right / note).write_text("remote")
    _apply(right, store, home)
    store.requests.clear()
    assert build_reconcile_plan(left, store).conflicts
    assert store.requests == []
    plan = build_reconcile_plan(
        left, store, resolutions={"memory:notes/conflict.md": "remote"}
    )
    assert not plan.conflicts
    assert store.requests == [("memory:notes/conflict.md",)]
