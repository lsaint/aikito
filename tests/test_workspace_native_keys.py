"""Explicit protection, immutable Vault keys and unavailable-backend recovery."""

import base64
import json
import os
import subprocess
from unittest.mock import Mock

import pytest

from aikito.workspace import native_keys as native
from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import KeyStorageUnavailable

VAULT = "00112233445566778899aabbccddeeff"
KEY = bytes(range(32))


class MemoryNative:
    backend = "macos-keychain"

    def __init__(self):
        self.key = None
        self.available = True

    def protect(self, vault, key):
        if not self.available or (self.key is not None and self.key != key):
            raise KeyStorageUnavailable()
        self.key = key
        return self.backend, None

    def load(self, vault, backend, protected):
        if not self.available or self.key is None:
            raise KeyStorageUnavailable()
        return self.key


@pytest.fixture
def store(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    return DeviceKeyStore(tmp_path / "keys", home=home, native=MemoryNative())


def test_default_file_never_touches_native_backend(store):
    store.native.load = Mock(side_effect=AssertionError("No implicit native lookup"))
    store.native.protect = Mock(side_effect=AssertionError("No implicit native write"))
    path = store.save(VAULT, KEY)
    assert store.load(VAULT) == KEY and store.storage(VAULT) == "file"
    assert path.read_bytes() == base64.b64encode(KEY) + b"\n"


def test_explicit_native_transition_is_private_key_immutable_and_idempotent(store):
    path = store.save(VAULT, KEY)
    store.set_storage(VAULT, "native")
    body = json.loads(path.read_bytes())
    assert body["version"] == 2 and body["backend"] == "macos-keychain"
    assert body["protected_key"] is None and body["vault_id"] == VAULT
    assert base64.b64encode(KEY) not in path.read_bytes()
    assert store.load(VAULT) == KEY
    before = path.read_bytes()
    store.set_storage(VAULT, "native")
    store.save(VAULT, KEY)
    assert before == path.read_bytes()
    with pytest.raises(KeyStorageUnavailable):
        store.save(VAULT, bytes(32))
    assert path.read_bytes() == before
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600


def test_explicit_file_transition_keeps_native_item_but_does_not_use_it(store):
    store.save(VAULT, KEY)
    store.set_storage(VAULT, "native")
    store.set_storage(VAULT, "file")
    assert store.native.key == KEY
    store.native.available = False
    assert store.load(VAULT) == KEY and store.storage(VAULT) == "file"


@pytest.mark.parametrize("failure", ["unavailable", "different", "readback", "publish"])
def test_native_failure_preserves_original_file(store, monkeypatch, failure):
    path = store.save(VAULT, KEY)
    before = path.read_bytes()
    if failure == "unavailable":
        store.native.available = False
    elif failure == "different":
        store.native.key = bytes(32)
    elif failure == "readback":
        monkeypatch.setattr(store.native, "load", lambda *args: bytes(32))
    else:
        monkeypatch.setattr(
            "aikito.workspace.device_keys.os.replace",
            Mock(side_effect=OSError("Simulated publish failure")),
        )
    with pytest.raises(KeyStorageUnavailable):
        store.set_storage(VAULT, "native")
    assert path.read_bytes() == before and store.load(VAULT) == KEY
    assert not list(store.root.glob(".key-*"))


def test_unavailable_native_never_falls_back_and_recovery_must_be_explicit(store):
    path = store.save(VAULT, KEY)
    store.set_storage(VAULT, "native")
    protected = path.read_bytes()
    store.native.available = False
    for operation in (
        lambda: store.load(VAULT),
        lambda: store.save(VAULT, KEY),
        lambda: store.set_storage(VAULT, "file"),
    ):
        with pytest.raises(KeyStorageUnavailable):
            operation()
        assert path.read_bytes() == protected
    with pytest.raises(KeyStorageUnavailable):
        store.recover_file(VAULT, bytes(32))
    assert path.read_bytes() == protected
    store.recover_file(VAULT, KEY)
    assert store.load(VAULT) == KEY and store.storage(VAULT) == "file"


@pytest.mark.parametrize(
    "field,value",
    [
        ("vault_id", "f" * 32),
        ("version", True),
        ("backend", "unknown"),
        ("commitment", "bad"),
        ("protected_key", "unexpected"),
    ],
)
def test_tampered_native_record_is_retained_and_not_used(store, field, value):
    path = store.save(VAULT, KEY)
    store.set_storage(VAULT, "native")
    body = json.loads(path.read_bytes())
    body[field] = value
    path.write_text(json.dumps(body, sort_keys=True, separators=(",", ":")))
    before = path.read_bytes()
    with pytest.raises(KeyStorageUnavailable):
        store.load(VAULT)
    assert path.read_bytes() == before


def test_macos_write_uses_only_stdin_and_escapes_security_parser(monkeypatch):
    observed = []

    def execute(args, **kwargs):
        observed.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(native.subprocess, "run", execute)
    secret = "token\"with\\quotes;and'punctuation"
    native.macos_command(
        ["add-generic-password", "-s", "aikito", "-a", "TOKEN", "-w", secret]
    )
    args, options = observed[0]
    assert args == ["/usr/bin/security", "-i"] and secret not in str(args)
    assert '\\"' in options["input"] and "\\\\" in options["input"]
    assert options["input"].count("\n") == 1 and options["timeout"] == 3.0


@pytest.mark.parametrize("invalid", ["newline\nsecret", "null\0secret", "x" * 4100])
def test_macos_rejects_command_injection_and_truncation_before_exec(
    monkeypatch, invalid
):
    execute = Mock()
    monkeypatch.setattr(native.subprocess, "run", execute)
    with pytest.raises(KeyStorageUnavailable):
        native.macos_command(["add-generic-password", "-w", invalid])
    execute.assert_not_called()


def test_failed_native_commands_never_expose_captured_secret_output(monkeypatch):
    secret = "captured-secret-not-for-logs"
    monkeypatch.setattr(
        native.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 1, secret, secret),
    )
    with pytest.raises(KeyStorageUnavailable) as caught:
        native.macos_command(["help"])
    assert secret not in str(caught.value)


def test_macos_missing_is_distinct_from_locked_or_denied(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "darwin")
    probe = Mock(side_effect=KeyStorageUnavailable())
    monkeypatch.setattr(native, "_mac_unlocked", probe)
    execute = Mock()
    monkeypatch.setattr(native, "_run", execute)
    provider = native.NativeKeyStorage()
    with pytest.raises(KeyStorageUnavailable):
        provider.load(VAULT, "macos-keychain", None)
    execute.assert_not_called()
    monkeypatch.setattr(native, "_mac_unlocked", lambda *args: None)
    monkeypatch.setattr(
        native,
        "_run",
        lambda *args, **kwargs: subprocess.CompletedProcess([], 44, "", "missing"),
    )
    assert provider._lookup(VAULT) is None
    with pytest.raises(KeyStorageUnavailable):
        provider.load(VAULT, "macos-keychain", None)


@pytest.mark.parametrize("stdout,code", [("(<true>,)", 0), ("", 1), ("unexpected", 0)])
def test_linux_probe_stops_locked_absent_or_invalid_services(monkeypatch, stdout, code):
    monkeypatch.setattr(native.shutil, "which", lambda name: "/usr/bin/" + name)
    replies = iter(
        [
            subprocess.CompletedProcess(
                [], 0, "(objectpath '/org/freedesktop/secrets/collection/login',)\n", ""
            ),
            subprocess.CompletedProcess([], code, stdout, ""),
        ]
    )
    monkeypatch.setattr(native, "_run", lambda *args, **kwargs: next(replies))
    with pytest.raises(KeyStorageUnavailable):
        native._linux_collection()


def test_linux_missing_tools_do_not_invoke_service(monkeypatch):
    monkeypatch.setattr(native.shutil, "which", lambda name: None)
    execute = Mock()
    monkeypatch.setattr(native, "_run", execute)
    with pytest.raises(KeyStorageUnavailable):
        native._linux_collection()
    execute.assert_not_called()


def test_linux_store_never_places_secret_in_argv(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "linux")
    monkeypatch.setattr(
        native, "_linux_collection", lambda: "/org/freedesktop/secrets/collection/login"
    )
    monkeypatch.setattr(native.shutil, "which", lambda name: "/usr/bin/" + name)
    saved = None

    def execute(args, **kwargs):
        nonlocal saved
        if "lookup" in args:
            return subprocess.CompletedProcess(
                args, 1 if saved is None else 0, saved or "", ""
            )
        assert "store" in args and base64.b64encode(KEY).decode() not in str(args)
        saved = kwargs["input"]
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(native, "_run", execute)
    provider = native.NativeKeyStorage()
    backend, protected = provider.protect(VAULT, KEY)
    assert backend == "linux-secret-service" and protected is None
    assert provider.load(VAULT, backend, protected) == KEY


def test_dpapi_is_user_scoped_with_vault_entropy_and_frees_native_memory(monkeypatch):
    monkeypatch.setattr(native.sys, "platform", "win32")
    allocations = []
    calls = []

    class Operation:
        def __call__(
            self, source, description, entropy, reserved, prompt, flags, output
        ):
            assert flags == 1 and prompt is None and reserved is None
            assert ctypes_bytes(entropy) == ("aikito.device-key.v2:" + VAULT).encode()
            content = b"protected" if self is protect else KEY
            buffer = (native.ctypes.c_ubyte * len(content)).from_buffer_copy(content)
            allocations.append(buffer)
            output._obj.size, output._obj.data = len(content), buffer
            calls.append(self)
            return 1

    protect, unprotect = Operation(), Operation()

    class Free:
        def __call__(self, pointer):
            calls.append("free")

    crypt = type(
        "Crypt", (), {"CryptProtectData": protect, "CryptUnprotectData": unprotect}
    )()
    kernel = type("Kernel", (), {"LocalFree": Free()})()
    monkeypatch.setattr(
        native.ctypes,
        "WinDLL",
        lambda name, **kwargs: crypt if name == "crypt32" else kernel,
        raising=False,
    )
    assert native._dpapi(KEY, VAULT, protect=True) == b"protected"
    assert native._dpapi(b"protected", VAULT, protect=False) == KEY
    assert calls == [protect, "free", unprotect, "free"]


def ctypes_bytes(pointer):
    return native.ctypes.string_at(pointer._obj.data, pointer._obj.size)
