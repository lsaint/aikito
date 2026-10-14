"""Device key files are private, immutable, and outside managed resources."""

import base64
import os
from unittest.mock import patch

import pytest

from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import KeyStorageUnavailable

VAULT = "00112233445566778899aabbccddeeff"
KEY = bytes(range(32))


@pytest.fixture
def store(tmp_path, monkeypatch):
    # Unit tests isolate OS ACL tooling; the platform smoke exercises real ACLs.
    if os.name == "nt":
        monkeypatch.setattr(
            "aikito.workspace.device_keys.check_credential_permissions",
            lambda path: (True, "private"),
        )
        monkeypatch.setattr(
            "aikito.workspace.device_keys.secure_file_permissions", lambda path: True
        )
        monkeypatch.setattr(
            "aikito.workspace.device_keys.secure_directory_permissions",
            lambda path: True,
        )
    return DeviceKeyStore(tmp_path / "keys", workspace=tmp_path / "workspace")


def test_read_and_path_create_nothing(store):
    assert store.load(VAULT) is None
    assert store.path(VAULT).name == VAULT + ".key"
    assert not store.root.exists()


def test_record_is_private_and_idempotent(store):
    path = store.save(VAULT, KEY)
    before = path.read_bytes()
    assert store.save(VAULT, KEY) == path
    assert store.load(VAULT) == KEY
    assert path.read_bytes() == before == base64.b64encode(KEY) + b"\n"
    assert list(store.root.iterdir()) == [path]
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        assert store.root.stat().st_mode & 0o777 == 0o700


def test_different_key_never_replaces_existing_record(store):
    path = store.save(VAULT, KEY)
    with pytest.raises(KeyStorageUnavailable):
        store.save(VAULT, bytes(32))
    assert store.load(VAULT) == KEY
    assert path.read_bytes() == base64.b64encode(KEY) + b"\n"


@pytest.mark.parametrize("vault", ["../secret", "A" * 32, "", "0" * 31])
def test_invalid_id_creates_nothing(store, vault):
    with pytest.raises(KeyStorageUnavailable):
        store.save(vault, KEY)
    assert not store.root.exists()


def test_workspace_config_override_is_rejected(tmp_path):
    with pytest.raises(KeyStorageUnavailable):
        DeviceKeyStore(tmp_path / "workspace/keys", workspace=tmp_path / "workspace")
    assert list(tmp_path.iterdir()) == []


def test_failed_publication_leaves_no_partial_record(store):
    with (
        patch(
            "aikito.workspace.device_keys.os.link", side_effect=OSError("unavailable")
        ),
        pytest.raises(KeyStorageUnavailable),
    ):
        store.save(VAULT, KEY)
    assert list(store.root.iterdir()) == []


def test_malformed_record_is_retained(store):
    path = store.save(VAULT, KEY)
    path.write_bytes(b"invalid key\n")
    with pytest.raises(KeyStorageUnavailable):
        store.load(VAULT)
    assert path.read_bytes() == b"invalid key\n"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission mode")
def test_insecure_record_is_rejected_without_repair(store):
    path = store.save(VAULT, KEY)
    path.chmod(0o644)
    with pytest.raises(KeyStorageUnavailable):
        store.load(VAULT)
    assert path.stat().st_mode & 0o777 == 0o644


def test_symlink_and_hardlink_records_are_rejected(store, tmp_path):
    path = store.save(VAULT, KEY)
    target = tmp_path / "copy"
    path.rename(target)
    try:
        path.symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are unavailable")
    with pytest.raises(KeyStorageUnavailable):
        store.load(VAULT)
    path.unlink()
    os.link(target, path)
    with pytest.raises(KeyStorageUnavailable):
        store.load(VAULT)


def test_publication_race_never_overwrites_winning_key(store):
    real_link = os.link
    winner = bytes([42]) * 32

    def competing_link(source, destination):
        # Another process has already atomically published a different vault key.
        destination.write_bytes(base64.b64encode(winner) + b"\n")
        if os.name != "nt":
            destination.chmod(0o600)
        real_link(source, destination)

    with (
        patch("aikito.workspace.device_keys.os.link", side_effect=competing_link),
        pytest.raises(KeyStorageUnavailable),
    ):
        store.save(VAULT, KEY)
    assert store.load(VAULT) == winner
    assert list(store.root.iterdir()) == [store.path(VAULT)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission mode")
def test_insecure_directory_is_rejected_without_repair(store):
    store.root.mkdir(mode=0o755)
    with pytest.raises(KeyStorageUnavailable):
        store.save(VAULT, KEY)
    assert store.root.stat().st_mode & 0o777 == 0o755
    assert list(store.root.iterdir()) == []
