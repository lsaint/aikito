"""Core CLI and format guards remain usable without the encrypted extra."""

import builtins
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito.cli_parser import build_parser
from aikito.remote_key_cli import cmd_remote_key
from aikito.workspace.encrypted_remote import EncryptedDependencyMissing, EncryptedVault
from aikito.workspace.remote_format import (
    EncryptedStoreUnsupported,
    require_plaintext_snapshot,
)
from aikito.workspace.remote_store import RemoteSnapshot
from aikito.workspace.payload import ResourceDescriptor


@pytest.fixture
def without_crypto(monkeypatch):
    original = builtins.__import__
    attempts = []

    def blocked(name, *args, **kwargs):
        if name.startswith("cryptography"):
            attempts.append(name)
            raise ModuleNotFoundError("Optional dependency is absent")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    return attempts


def test_core_parser_and_future_format_guard_do_not_import_crypto(without_crypto):
    parser = build_parser()
    assert parser.parse_args(["remote", "key", "path"]).key_action == "path"
    with pytest.raises(EncryptedStoreUnsupported):
        require_plaintext_snapshot(
            RemoteSnapshot(
                "store", 1, {"e2:opaque": ResourceDescriptor("", "0" * 64, 0)}
            )
        )
    assert without_crypto == []


def test_encrypted_operation_has_explicit_install_hint(without_crypto):
    vault = EncryptedVault("0" * 32, bytes(32))
    with pytest.raises(EncryptedDependencyMissing, match=r"aikito\[encrypted\]"):
        vault.collection("private")


def test_missing_extra_does_not_read_recovery_input(
    tmp_path, monkeypatch, without_crypto, capsys
):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "config"))
    monkeypatch.setattr(
        "aikito.remote_key_cli.is_recognized_workspace", lambda path: True
    )
    read = Mock(side_effect=AssertionError("Secret input must not be read"))
    monkeypatch.setattr("aikito.remote_key_cli.os.dup", read)
    args = SimpleNamespace(
        key_action="recover",
        workspace=str(tmp_path / "workspace"),
        header="missing",
        sync_id="store",
        recovery_fd=0,
    )
    with pytest.raises(SystemExit):
        cmd_remote_key(args)
    assert "encrypted_dependency_missing" in capsys.readouterr().err
    read.assert_not_called()
    assert not (tmp_path / "config").exists()
