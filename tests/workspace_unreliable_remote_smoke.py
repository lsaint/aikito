"""Transport-fault acceptance plus real local-completion process exits.

The shared scenarios run on both stores. Only the filesystem store is reopened
across killed processes; memory storage makes no cross-process promise.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aikito.skill_state import WorkspaceWriterLock
from aikito.workspace import pending_commit, transactions
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    recover_reconciliation,
    run_reconciliation,
)
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_store import SnapshotExpired
from aikito.workspace.remote_wire import build_commit_request, encode_commit_request
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.resource_state import PENDING_COMMIT_STATE, REPLICA_STATE
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from workspace_unreliable_remote import Fault, Gate, UnreliableRemote

CHECKPOINTS = ("replica", "committed", "cleanup", "before-clear", "after-clear")


def put(name, data=b"remote", before=None):
    payload = FilePayload(data)
    return ResourceMutation(
        f"memory:notes/{name}.md",
        before,
        ResourceDescriptor(hashlib.sha256(data).hexdigest(), payload_hash(payload)),
        payload,
    )


def submit(backend, client, *mutations):
    return backend.commit(
        build_commit_request(client, client, backend.read(), mutations)
    )


def confirm(local, remote, home):
    with WorkspaceWriterLock(home):
        return recover_pending(local, remote, home)


def report(root, **fields):
    (root / "checked.json").write_text(json.dumps(fields))


def delivery_scenarios(root, factory):
    for name, fault in (
        ("before-send", Fault.UNAVAILABLE),
        ("response-loss", Fault.LOSE_RESPONSE),
        ("duplicate", Fault.DUPLICATE_DELIVERY),
        ("resolve-failure", Fault.LOSE_RESPONSE),
    ):
        case = root / name
        local, home = _workspace(case / "local"), case / "home"
        backend = factory(case / "center")
        remote = UnreliableRemote(backend)
        (local / "memory/notes/upload.md").write_text("upload")
        remote.schedule("commit", fault)
        try:
            run_reconciliation(local, remote, home, dry_run=False)
        except WorkspaceReconcileError:
            assert fault is not Fault.DUPLICATE_DELIVERY
        else:
            assert fault is Fault.DUPLICATE_DELIVERY
        if fault is not Fault.DUPLICATE_DELIVERY:
            store = PendingCommitStore(local, home)
            pending = store.load()
            assert pending is not None and load_replica_state(local)[0].base == {}
            if name == "resolve-failure":
                before = (local / PENDING_COMMIT_STATE).read_bytes()
                remote.schedule("resolve_commit", Fault.UNAVAILABLE)
                try:
                    run_reconciliation(local, remote, home, dry_run=False)
                except WorkspaceReconcileError:
                    pass
                else:
                    raise AssertionError("Expected failed resolution")
                assert before == (local / PENDING_COMMIT_STATE).read_bytes()
                assert len(remote.commits) == 1
            resumed = UnreliableRemote(
                FilesystemRemote(backend.root)
                if isinstance(backend, FilesystemRemote)
                else backend
            )
            run_reconciliation(local, resumed, home, dry_run=False)
            if fault is Fault.UNAVAILABLE:
                assert resumed.commits == (encode_commit_request(pending.request),)
            else:
                assert resumed.commits == ()
        assert backend.read().revision == 1
        assert PendingCommitStore(local, home).load() is None
        report(case, revision=1, deliveries=len(remote.deliveries))

    for competing in (False, True):
        case = root / ("delayed-cas" if competing else "delayed-accepted")
        local, home = _workspace(case / "local"), case / "home"
        backend = factory(case / "center")
        remote = UnreliableRemote(backend)
        (local / "memory/notes/upload.md").write_text("queued")
        remote.schedule("commit", Fault.DELAY_DELIVERY)
        try:
            run_reconciliation(local, remote, home, dry_run=False)
        except WorkspaceReconcileError:
            pass
        else:
            raise AssertionError("Expected delayed request")
        if competing:
            submit(backend, "other", put("other"))
        gate = Gate(arrivals=2)
        remote.schedule("commit", Fault.PAUSE_DELIVERY, gate=gate)
        with ThreadPoolExecutor(max_workers=2) as executor:
            original = executor.submit(remote.deliver_delayed, gate=gate)
            retry = executor.submit(confirm, local, remote, home)
            try:
                gate.wait()
                assert remote.commits[0] == remote.commits[1]
            finally:
                gate.release()
            assert retry.result(timeout=15)
            if competing:
                try:
                    original.result(timeout=15)
                except SnapshotExpired:
                    pass
                else:
                    raise AssertionError("Delayed original escaped CAS")
            else:
                result = original.result(timeout=15)
                assert remote.results == (result, result)
        assert backend.read().revision == 1
        assert ("memory:notes/upload.md" in backend.read().resources) != competing
        assert PendingCommitStore(local, home).load() is None
        report(
            case,
            revision=1,
            identical_deliveries=remote.deliveries[0] == remote.deliveries[1],
        )


def historical_scenario(root, factory):
    case = root / "historical"
    local, home = _workspace(case / "local"), case / "home"
    backend = factory(case / "center")
    for name in ("deleted", "noop", "blocked"):
        (local / f"memory/notes/{name}.md").write_text("base")
    run_reconciliation(local, backend, home, dry_run=False)
    before = load_replica_state(local)[0]
    submit(backend, "download-owner", put("download", b"historical download"))
    (local / "memory/notes/deleted.md").unlink()
    (local / "memory/notes/upload.md").write_text("original upload")
    (local / "memory/notes/blocked.md").write_text(
        "api_key = 'abcdefghijklmnopqrstuv'\n"
    )
    remote = UnreliableRemote(backend)
    remote.schedule("commit", Fault.LOSE_RESPONSE)
    try:
        run_reconciliation(local, remote, home, dry_run=False)
    except WorkspaceReconcileError:
        pass
    else:
        raise AssertionError("Expected response loss")
    pending = PendingCommitStore(local, home).load()
    assert "memory:notes/blocked.md" in pending.excluded_resource_ids
    snapshot = backend.read()
    submit(
        backend,
        "latest-owner",
        put(
            "download",
            b"current download",
            snapshot.resources["memory:notes/download.md"],
        ),
        put("noop", b"current noop", snapshot.resources["memory:notes/noop.md"]),
        ResourceMutation(
            "memory:notes/upload.md",
            snapshot.resources["memory:notes/upload.md"],
            None,
            None,
        ),
    )
    (local / "memory/notes/upload.md").write_text("user changed upload")
    (local / "memory/notes/download.md").write_text("user changed download")
    resumed = UnreliableRemote(
        FilesystemRemote(backend.root)
        if isinstance(backend, FilesystemRemote)
        else backend
    )
    assert confirm(local, resumed, home)
    assert resumed.calls == ("resolve_commit",)
    state = load_replica_state(local)[0]
    assert "memory:notes/deleted.md" not in state.base
    assert "memory:notes/download.md" not in state.base
    for name in ("noop", "blocked"):
        assert (
            state.base[f"memory:notes/{name}.md"]
            == before.base[f"memory:notes/{name}.md"]
        )
    uploaded = next(
        m for m in pending.request.mutations if m.id == "memory:notes/upload.md"
    )
    assert state.base[uploaded.id].fingerprint == uploaded.after.fingerprint
    latest = backend.read().revision
    plan = run_reconciliation(local, resumed, home, dry_run=False)
    assert {item.id for item in plan.conflicts} == {
        "memory:notes/upload.md",
        "memory:notes/download.md",
    }
    assert (
        next(item for item in plan.items if item.id == "memory:notes/blocked.md").action
        == "BLOCKED"
    )
    assert (local / "memory/notes/upload.md").read_text() == "user changed upload"
    assert (local / "memory/notes/download.md").read_text() == "user changed download"
    assert (local / "memory/notes/noop.md").read_bytes() == b"current noop"
    assert backend.read().revision == latest
    report(case, revision=latest, preserved_conflicts=2, blocked_base_preserved=True)


def crash_client(case, checkpoint):
    local, home = case / "local", case / "home"
    remote = UnreliableRemote(FilesystemRemote(case / "center"))
    atomic, cleanup, unlink = (
        transactions.atomic_text,
        transactions._cleanup,
        pending_commit.atomic_unlink,
    )

    def write(path, text):
        atomic(path, text)
        if not remote.results:
            return
        if checkpoint == "replica" and path == local / REPLICA_STATE:
            os._exit(73)
        if (
            checkpoint == "committed"
            and path.is_relative_to(local)
            and "workspace-transactions" in path.parts
            and path.name == "pending.json"
            and json.loads(text)["phase"] == "committed"
        ):
            os._exit(73)

    def clean(roots, txid):
        if checkpoint == "cleanup" and roots == (local,) and remote.results:
            os._exit(73)
        cleanup(roots, txid)

    def clear(path):
        if checkpoint == "before-clear":
            os._exit(73)
        unlink(path)
        if checkpoint == "after-clear":
            os._exit(73)

    transactions.atomic_text, transactions._cleanup, pending_commit.atomic_unlink = (
        write,
        clean,
        clear,
    )
    run_reconciliation(local, remote, home, dry_run=False)
    raise AssertionError("Local crash checkpoint was not reached")


def local_crashes(root):
    for checkpoint in CHECKPOINTS:
        case = root / "local-crashes" / checkpoint
        local, home = _workspace(case / "local"), case / "home"
        backend = FilesystemRemote.create(case / "center")
        run_reconciliation(local, backend, home, dry_run=False)
        submit(backend, "other", put("download", b"download"))
        (local / "memory/notes/upload.md").write_text("upload")
        result = subprocess.run(
            [sys.executable, __file__, str(case), "--crash-at", checkpoint], check=False
        )
        assert result.returncode == 73
        reopened = UnreliableRemote(FilesystemRemote(backend.root))
        retained = checkpoint != "replica"
        if retained:
            (local / "memory/notes/download.md").write_text("edit after completion")
            (local / "memory/notes/upload.md").write_text("edit after upload")
            for operation in ("read", "fetch", "commit", "resolve_commit", "recover"):
                reopened.schedule(operation, Fault.UNAVAILABLE)
            if checkpoint == "after-clear":
                # With no pending left, normal planning may require the remote.
                try:
                    recover_reconciliation(local, reopened, home)
                except WorkspaceReconcileError:
                    pass
                else:
                    raise AssertionError("Expected unavailable normal lifecycle")
                assert reopened.calls == ("recover",)
            else:
                assert not recover_reconciliation(local, reopened, home)
                assert reopened.calls == ()
            assert (
                local / "memory/notes/download.md"
            ).read_text() == "edit after completion"
            assert (local / "memory/notes/upload.md").read_text() == "edit after upload"
            assert load_replica_state(local)[0].completion_marker is not None
        else:
            assert not recover_reconciliation(local, reopened, home)
            assert not (local / "memory/notes/download.md").exists()
            assert "memory:notes/download.md" not in load_replica_state(local)[0].base
            assert load_replica_state(local)[0].completion_marker is not None
        assert PendingCommitStore(local, home).load() is None
        assert backend.read().revision == 3
        report(
            case,
            revision=3,
            retained=retained,
            offline_cleanup=retained and checkpoint != "after-clear",
        )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument(
        "--backend", choices=("filesystem", "memory"), default="filesystem"
    )
    parser.add_argument("--crash-at", choices=CHECKPOINTS)
    args = parser.parse_args()
    root = args.root.resolve()
    if args.crash_at:
        crash_client(root, args.crash_at)
        return
    factory = (
        FilesystemRemote.create
        if args.backend == "filesystem"
        else lambda _: InMemoryRemote()
    )
    delivery_scenarios(root, factory)
    historical_scenario(root, factory)
    if args.backend == "filesystem":
        local_crashes(root)
    report(
        root,
        backend=args.backend,
        network_scenarios=7,
        local_crashes=5 if args.backend == "filesystem" else 0,
    )


if __name__ == "__main__":
    main()
