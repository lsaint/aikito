"""Overwritten versions have independent archives and bounded fetch costs."""

import hashlib
import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aikito.workspace import remote_limits
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    TomlPayload,
    TreeEntry,
    TreePayload,
    decode_payload,
    encode_payload,
    payload_hash,
)
from aikito.workspace.reconcile import ReconcileItem, _archive_overwritten_remote
from aikito.workspace.reconcile_budget import needed_downloads, select_round
from aikito.workspace.remote_protocol import encode_fetch_response
from aikito.workspace.remote_store import RemoteSnapshot


def descriptor(payload):
    return ResourceDescriptor(
        "0" * 64, payload_hash(payload), len(encode_payload(payload))
    )


def item(identity, target="remote"):
    return ReconcileItem(identity, "UPDATE", target, "0" * 64, "1" * 64, "Changed")


def archive(local, payloads):
    plan = SimpleNamespace(
        local=local,
        changes=[item(identity) for identity in payloads],
        remote_snapshot=RemoteSnapshot(
            "store",
            1,
            {identity: descriptor(payload) for identity, payload in payloads.items()},
        ),
        payload_cache=tuple(
            (identity, payload_hash(payload), payload)
            for identity, payload in payloads.items()
        ),
    )
    _archive_overwritten_remote(plan, timestamp="20261014-120000")


def records(local):
    return [
        json.loads(path.read_text())
        for path in (local / ".local/sync-conflicts").glob("**/resource.json")
    ]


def restore(record):
    encoded = json.dumps(
        record["payload"], sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode()
    return decode_payload(encoded, hashlib.sha256(encoded).hexdigest())


def test_same_second_repeated_updates_keep_both_versions(tmp_path):
    identity = "project-memory:demo/notes/example.md"
    archive(tmp_path, {identity: FilePayload(b"first")})
    archive(tmp_path, {identity: FilePayload(b"second")})
    assert {restore(record).data for record in records(tmp_path)} == {
        b"first",
        b"second",
    }
    assert len(list((tmp_path / ".local/sync-conflicts").iterdir())) == 2


def test_shared_fields_keep_identity_and_original_field_path(tmp_path):
    payloads = {
        "config:memory.stale_days": TomlPayload(
            ("memory", "stale_days"), "value = 14\n"
        ),
        "config:memory.retention": TomlPayload(("memory", "retention"), "value = 50\n"),
    }
    archive(tmp_path, payloads)
    assert {record["id"]: restore(record) for record in records(tmp_path)} == payloads
    assert len(list((tmp_path / ".local/sync-conflicts").glob("**/config.toml"))) == 2


def test_tree_archive_preserves_portable_execution_flags(tmp_path):
    payload = TreePayload(
        (TreeEntry("SKILL.md", b"# Skill\n"), TreeEntry("run.sh", b"echo test\n", True))
    )
    archive(tmp_path, {"skill:example": payload})
    assert restore(records(tmp_path)[0]) == payload


def test_archive_write_failure_is_not_ignored(tmp_path):
    with (
        patch(
            "aikito.workspace.reconcile._write_archive_bytes",
            side_effect=OSError("disk full"),
        ),
        pytest.raises(OSError, match="disk full"),
    ):
        archive(tmp_path, {"memory:notes/example.md": FilePayload(b"old")})
    assert records(tmp_path) == []


def test_symlink_archive_directory_is_rejected(tmp_path):
    target = tmp_path / "outside"
    target.mkdir()
    (tmp_path / ".local").mkdir()
    try:
        (tmp_path / ".local/sync-conflicts").symlink_to(
            target, target_is_directory=True
        )
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(ValueError, match="Unsafe state directory"):
        archive(tmp_path, {"memory:notes/example.md": FilePayload(b"old")})
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("second_target", ["local", "remote"])
def test_archive_and_download_payloads_share_one_round_budget(
    monkeypatch, second_target
):
    ids = ("memory:notes/one.md", "memory:notes/two.md")
    old = FilePayload(b"x" * 1500)
    snapshot = RemoteSnapshot(
        "store", 1, {identity: descriptor(old) for identity in ids}
    )
    candidates = [item(ids[0]), item(ids[1], second_target)]
    replacements = {
        identity: descriptor(FilePayload(b"tiny"))
        for identity in ids
        if identity == ids[0] or second_target == "remote"
    }
    one_response = len(encode_fetch_response({ids[0]: old}))
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", one_response + 64)
    selected = select_round(
        candidates, {identity: set() for identity in ids}, snapshot, replacements, None
    )
    assert [candidate.action for candidate in selected] == ["UPDATE", "DEFERRED"]
    assert needed_downloads(selected, snapshot.resources) == {ids[0]}


def test_old_version_that_cannot_fit_is_blocked_before_fetch(monkeypatch):
    identity = "memory:notes/one.md"
    old = FilePayload(b"x" * 1500)
    snapshot = RemoteSnapshot("store", 1, {identity: descriptor(old)})
    monkeypatch.setattr(
        remote_limits,
        "MAX_REMOTE_RESPONSE_BYTES",
        len(encode_fetch_response({identity: old})) - 1,
    )
    selected = select_round(
        [item(identity)],
        {identity: set()},
        snapshot,
        {identity: descriptor(FilePayload(b"tiny"))},
        None,
    )
    assert selected[0].action == "BLOCKED"
    assert needed_downloads(selected, snapshot.resources) == set()


def test_shared_config_archive_is_charged_once(monkeypatch):
    ids = ("config:memory.retention", "config:memory.stale_days")
    old = {
        identity: TomlPayload(
            ("memory", identity.rsplit(".", 1)[1]), 'value = "' + "x" * 1000 + '"\n'
        )
        for identity in ids
    }
    snapshot = RemoteSnapshot(
        "store", 1, {identity: descriptor(payload) for identity, payload in old.items()}
    )
    monkeypatch.setattr(
        remote_limits, "MAX_REMOTE_RESPONSE_BYTES", len(encode_fetch_response(old)) + 64
    )
    selected = select_round(
        [item(identity) for identity in ids],
        {identity: set() for identity in ids},
        snapshot,
        {
            identity: descriptor(TomlPayload(payload.path, "value = 1\n"))
            for identity, payload in old.items()
        },
        None,
    )
    assert all(candidate.action == "UPDATE" for candidate in selected)
    assert needed_downloads(selected, snapshot.resources) == set(ids)
