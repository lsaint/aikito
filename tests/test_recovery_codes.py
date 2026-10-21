"""Recovery backups preserve all records without exposing secrets or unsafe paths."""

import json
import os
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from aikito.workspace import recovery_codes
from aikito.workspace.encrypted_remote import encode_recovery_code


@pytest.fixture
def location(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    return home, workspace


def save(location, number, code=None):
    home, workspace = location
    return recovery_codes.save_recovery_code(
        f"{number:032x}",
        code or encode_recovery_code(bytes([number]) * 32),
        home=home,
        workspace=workspace,
        endpoint="https://example.com/v1/stores/auto/remote",
    )


def test_multiple_records_update_without_duplicates_and_keep_labels(location):
    path = save(location, 1)
    before = path.stat().st_mtime_ns
    assert save(location, 1) == path
    assert path.stat().st_mtime_ns == before
    save(location, 2)
    replacement = encode_recovery_code(bytes([3]) * 32)
    save(location, 1, replacement)
    text = path.read_text(encoding="utf-8")
    assert text.count("sync_id: ") == 2
    assert encode_recovery_code(bytes([1]) * 32) not in text
    assert replacement in text and encode_recovery_code(bytes([2]) * 32) in text
    assert "service: https://example.com\n" in text
    assert "workspace: " + json.dumps(str(location[1]), ensure_ascii=False) in text
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert path.parent.stat().st_mode & 0o777 == 0o700


def test_shared_space_retains_multiple_workspace_labels(location, tmp_path):
    path = save(location, 1)
    other = tmp_path / "another-工作区"
    other.mkdir()
    save((location[0], other), 1)
    text = path.read_text(encoding="utf-8")
    assert text.count("sync_id: ") == 1
    assert text.count("workspace: ") == 2
    assert str(other) in text


def test_concurrent_workspaces_do_not_lose_records(location):
    with ThreadPoolExecutor(max_workers=4) as executor:
        paths = list(executor.map(lambda number: save(location, number), range(1, 9)))
    text = paths[0].read_text(encoding="utf-8")
    assert text.count("sync_id: ") == 8
    for number in range(1, 9):
        assert f"sync_id: {number:032x}\n" in text


def test_malformed_file_is_not_overwritten(location):
    path = save(location, 1)
    path.write_text("unrecognized recovery record\n", encoding="utf-8")
    with pytest.raises(ValueError, match="format"):
        save(location, 2)
    assert path.read_text(encoding="utf-8") == "unrecognized recovery record\n"


def test_failed_atomic_replace_preserves_previous_records(location, monkeypatch):
    path = save(location, 1)
    before = path.read_bytes()
    monkeypatch.setattr(
        recovery_codes.os, "replace", Mock(side_effect=OSError("disk failure"))
    )
    with pytest.raises(OSError):
        save(location, 2)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".recovery-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission and link protection")
@pytest.mark.parametrize("unsafe", ["symlink", "hardlink", "permissions"])
def test_unsafe_existing_file_is_rejected(location, tmp_path, unsafe):
    path = save(location, 1)
    before = path.read_bytes()
    if unsafe == "permissions":
        path.chmod(0o644)
    else:
        original = tmp_path / "original.txt"
        path.rename(original)
        if unsafe == "symlink":
            path.symlink_to(original)
        else:
            os.link(original, path)
    with pytest.raises(ValueError):
        save(location, 2)
    assert path.read_bytes() == before


def test_cannot_store_inside_workspace(location, monkeypatch):
    home, workspace = location
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace / "config"))
    monkeypatch.setenv("APPDATA", str(workspace / "config"))
    with pytest.raises(ValueError, match="outside the workspace"):
        save(location, 1)
    assert not (workspace / "config").exists()


@pytest.mark.parametrize(
    "valid_id",
    [
        "sync-demo-001",
        "00112233445566778899aabbccddeeff",
        "store.alpha_1",
    ],
)
def test_valid_sync_id_accepted(location, valid_id):
    home, workspace = location
    path = recovery_codes.save_recovery_code(
        valid_id,
        encode_recovery_code(bytes(32)),
        home=home,
        workspace=workspace,
    )
    assert f"sync_id: {valid_id}\n" in path.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "invalid_id",
    [
        "",
        "sync id with space",
        "sync\nid",
        "a" * 129,
    ],
)
def test_invalid_sync_id_rejected(location, invalid_id):
    home, workspace = location
    with pytest.raises(ValueError, match="Invalid synchronization identity"):
        recovery_codes.save_recovery_code(
            invalid_id,
            encode_recovery_code(bytes(32)),
            home=home,
            workspace=workspace,
        )
