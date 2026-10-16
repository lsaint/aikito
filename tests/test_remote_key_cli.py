"""Recovery input is descriptor-only and never printed or implicitly echoed."""

import os
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito.cli_parser import build_parser
from aikito.remote_key_cli import cmd_remote_key
from aikito.workspace.encrypted_remote import RecoveryKeyInvalid, encode_recovery_code


def test_parser_accepts_descriptor_and_public_header_only():
    args = build_parser().parse_args(
        [
            "remote",
            "key",
            "recover",
            "--header",
            "header.json",
            "--sync-id",
            "store",
            "--workspace",
            "workspace",
            "--recovery-fd",
            "0",
        ]
    )
    assert args.recovery_fd == 0 and args.key_action == "recover"
    assert not hasattr(args, "recovery_code")


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    path = tmp_path / "header.json"
    path.write_bytes(b"{}")
    monkeypatch.setattr(
        "aikito.remote_key_cli.is_recognized_workspace", lambda path: True
    )
    monkeypatch.setattr("aikito.remote_key_cli.require_encrypted_support", lambda: None)
    store = Mock()
    monkeypatch.setattr("aikito.remote_key_cli.DeviceKeyStore", lambda **kwargs: store)
    args = SimpleNamespace(
        key_action="recover",
        workspace=str(tmp_path / "workspace"),
        header=str(path),
        sync_id="store",
        recovery_fd=None,
    )
    return args, store


def test_noninteractive_input_is_not_read_without_explicit_descriptor(
    recovery, monkeypatch, capsys
):
    args, store = recovery
    monkeypatch.setattr("aikito.remote_key_cli.sys.stdin.isatty", lambda: False)
    prompt = Mock(side_effect=AssertionError("No implicit echo input"))
    monkeypatch.setattr("aikito.remote_key_cli.getpass.getpass", prompt)
    with pytest.raises(SystemExit):
        cmd_remote_key(args)
    prompt.assert_not_called()
    store.save.assert_not_called()
    assert "unavailable" in capsys.readouterr().err


def test_explicit_input_keeps_original_descriptor_open_and_hides_secrets(
    recovery, monkeypatch, capsys
):
    args, store = recovery
    code = bytes(range(32))
    vrk = bytes([42]) * 32
    monkeypatch.setattr(
        "aikito.remote_key_cli.recover_vault",
        lambda *args: SimpleNamespace(vault_id="0" * 32, vrk=vrk),
    )
    read, write = os.pipe()
    try:
        os.write(write, encode_recovery_code(code).encode())
        os.close(write)
        write = None
        args.recovery_fd = read
        cmd_remote_key(args)
        os.fstat(read)
    finally:
        os.close(read)
        if write is not None:
            os.close(write)
    store.save.assert_called_once_with("0" * 32, vrk)
    output = capsys.readouterr()
    assert (
        code.hex() not in output.out + output.err
        and vrk.hex() not in output.out + output.err
    )


def test_failed_recovery_does_not_save_key(recovery, monkeypatch, capsys):
    args, store = recovery
    monkeypatch.setattr("aikito.remote_key_cli.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr(
        "aikito.remote_key_cli.getpass.getpass",
        lambda *args: encode_recovery_code(bytes(32)),
    )
    recover = Mock(side_effect=RecoveryKeyInvalid())
    monkeypatch.setattr("aikito.remote_key_cli.recover_vault", recover)
    with pytest.raises(SystemExit):
        cmd_remote_key(args)
    store.save.assert_not_called()
    assert "recovery_key_invalid" in capsys.readouterr().err


@pytest.mark.parametrize("action", ["migrate", "sync"])
def test_removed_plaintext_migration_and_duplicate_sync_commands(action):
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["remote", "key", action, "--workspace", "workspace", "--apply"]
        )
