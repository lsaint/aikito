"""Crash checkpoints in the actual filesystem resource/revision/receipt journal."""

from __future__ import annotations

import json
import tempfile
from contextlib import contextmanager
from pathlib import Path

import pytest

from aikito.workspace import transactions
from aikito.workspace.remote import FilesystemRemote, REMOTE_STATE
from aikito.workspace.remote_store import CommitOutcomeUnknown, RecoveryRequired
from aikito.workspace.remote_wire import build_commit_result
from test_workspace_remote_receipts import request, resolve


class SimulatedCrash(BaseException):
    pass


@pytest.mark.parametrize(
    "checkpoint", ["journal", "resource", "manifest", "committed", "cleanup"]
)
def test_receipt_and_resources_recover_together(tmp_path, monkeypatch, checkpoint):
    remote = FilesystemRemote.create(tmp_path / "center")
    req = request(remote)
    resource = remote.root / "memory/notes/one.md"
    atomic = transactions.atomic_text
    replace = transactions.os.replace
    cleanup = transactions._cleanup

    def write(path, text):
        atomic(path, text)
        if "workspace-transactions" in path.parts and path.name == "pending.json":
            phase = json.loads(text)["phase"]
            if (checkpoint == "journal" and phase == "pending") or (
                checkpoint == "committed" and phase == "committed"
            ):
                raise SimulatedCrash

    def move(src, dst):
        replace(src, dst)
        if (checkpoint == "resource" and Path(dst) == resource) or (
            checkpoint == "manifest" and Path(dst) == remote.root / REMOTE_STATE
        ):
            raise SimulatedCrash

    def clean(*args):
        if checkpoint == "cleanup":
            raise SimulatedCrash
        return cleanup(*args)

    with monkeypatch.context() as patch:
        patch.setattr(transactions, "atomic_text", write)
        patch.setattr(transactions.os, "replace", move)
        patch.setattr(transactions, "_cleanup", clean)
        with pytest.raises(SimulatedCrash):
            remote.commit(req)
    reopened = FilesystemRemote(remote.root)
    with pytest.raises(RecoveryRequired):
        resolve(reopened, req)
    assert reopened.recover()
    retained = checkpoint in {"committed", "cleanup"}
    assert reopened.read().revision == int(retained)
    assert resource.exists() == retained
    assert resolve(reopened, req) == (build_commit_result(req) if retained else None)
    receipt = reopened.commit(req)
    assert receipt == resolve(reopened, req)
    assert reopened.commit(req) == receipt
    assert reopened.read().revision == 1


def test_cleanup_error_after_publication_is_unknown_not_rejection(
    tmp_path, monkeypatch
):
    remote = FilesystemRemote.create(tmp_path / "center")
    req = request(remote)
    with monkeypatch.context() as patch:
        patch.setattr(
            transactions,
            "_cleanup",
            lambda *args: (_ for _ in ()).throw(OSError("cleanup unavailable")),
        )
        with pytest.raises(CommitOutcomeUnknown):
            remote.commit(req)
    with pytest.raises(RecoveryRequired):
        resolve(remote, req)
    remote.recover()
    assert resolve(remote, req) == build_commit_result(req)
    assert remote.read().revision == 1


@pytest.mark.parametrize("prefix", ["aikito-center-write-", "aikito-center-payload-"])
def test_staging_cleanup_failure_after_publish_is_unknown(
    tmp_path, monkeypatch, prefix
):
    remote = FilesystemRemote.create(tmp_path / "center")
    req = request(remote)
    original = tempfile.TemporaryDirectory

    @contextmanager
    def staging(*args, **kwargs):
        with original(*args, **kwargs) as directory:
            yield directory
        if kwargs.get("prefix") == prefix:
            raise RuntimeError("staging cleanup unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(tempfile, "TemporaryDirectory", staging)
        with pytest.raises(CommitOutcomeUnknown):
            remote.commit(req)
    assert resolve(remote, req) == build_commit_result(req)
    assert remote.read().revision == 1
