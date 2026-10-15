"""Optional OS protection with no plaintext fallback or secret-bearing argv.

Only the fixed service and a validated Vault account are used. Callers serialize
writes and publish a local backend reference after read-back succeeds. Native
records are never automatically deleted because another workspace may use them.
"""

from __future__ import annotations

import base64
import ctypes
import hmac
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .encrypted_remote import KeyStorageUnavailable, validate_vault_id

SERVICE = "aikito.encrypted-remote.v1"
MAX_NATIVE_BYTES = 65536


def _run(args, *, input=None):
    try:
        return subprocess.run(
            args, input=input, capture_output=True, text=True, check=False, timeout=3.0
        )
    except (OSError, subprocess.SubprocessError):
        raise KeyStorageUnavailable() from None


def _quote(value):
    if type(value) is not str or any(ch in value for ch in "\r\n\0"):
        raise KeyStorageUnavailable()
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def macos_command(arguments):
    """Use SecurityTool's quoted stdin grammar, never a shell or verbose echo."""
    line = " ".join(_quote(str(value)) for value in arguments) + "\n"
    if len(line.encode()) >= 4096:
        raise KeyStorageUnavailable()
    result = _run(["/usr/bin/security", "-i"], input=line)
    if result.returncode:
        raise KeyStorageUnavailable()


def _mac_unlocked(keychain=None):
    pointer = ctypes.c_void_p()
    security = ctypes.CDLL("/System/Library/Frameworks/Security.framework/Security")
    core = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )
    security.SecKeychainGetStatus.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    security.SecKeychainGetStatus.restype = ctypes.c_int32
    core.CFRelease.argtypes = [ctypes.c_void_p]
    core.CFRelease.restype = None
    if keychain is None:
        security.SecKeychainCopyDefault.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
        security.SecKeychainCopyDefault.restype = ctypes.c_int32
        status = security.SecKeychainCopyDefault(ctypes.byref(pointer))
    else:
        security.SecKeychainOpen.argtypes = [
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        security.SecKeychainOpen.restype = ctypes.c_int32
        status = security.SecKeychainOpen(str(keychain).encode(), ctypes.byref(pointer))
    if status != 0 or not pointer:
        raise KeyStorageUnavailable()
    try:
        flags = ctypes.c_uint32()
        if (
            security.SecKeychainGetStatus(pointer, ctypes.byref(flags)) != 0
            or not flags.value & 1
        ):
            raise KeyStorageUnavailable()
    finally:
        core.CFRelease(pointer)


def _linux_collection():
    tool = shutil.which("gdbus")
    if tool is None or shutil.which("secret-tool") is None:
        raise KeyStorageUnavailable()
    prefix = [
        tool,
        "call",
        "--session",
        "--dest",
        "org.freedesktop.secrets",
        "--object-path",
    ]
    alias = _run(
        [
            *prefix,
            "/org/freedesktop/secrets",
            "--method",
            "org.freedesktop.Secret.Service.ReadAlias",
            "default",
        ]
    )
    match = re.fullmatch(
        r"\(objectpath '(/org/freedesktop/secrets/collection/[A-Za-z0-9_/]+)',\)\s*",
        alias.stdout,
    )
    if alias.returncode or match is None:
        raise KeyStorageUnavailable()
    path = match.group(1)
    locked = _run(
        [
            *prefix,
            path,
            "--method",
            "org.freedesktop.DBus.Properties.Get",
            "org.freedesktop.Secret.Collection",
            "Locked",
        ]
    )
    if locked.returncode or locked.stdout.strip() != "(<false>,)":
        raise KeyStorageUnavailable()
    return path


class _Blob(ctypes.Structure):
    _fields_ = [("size", ctypes.c_uint32), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _dpapi(data, vault_id, *, protect):
    if sys.platform != "win32" or not 0 < len(data) <= MAX_NATIVE_BYTES:
        raise KeyStorageUnavailable()
    # Public entropy binds a protected blob to its Vault, not to a machine scope.
    entropy = ("aikito.device-key.v2:" + vault_id).encode("ascii")
    source_buffer = (ctypes.c_ubyte * len(data)).from_buffer_copy(data)
    entropy_buffer = (ctypes.c_ubyte * len(entropy)).from_buffer_copy(entropy)
    source = _Blob(len(data), source_buffer)
    extra = _Blob(len(entropy), entropy_buffer)
    result = _Blob()
    crypt32 = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    operation = crypt32.CryptProtectData if protect else crypt32.CryptUnprotectData
    operation.argtypes = [
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.POINTER(_Blob),
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(_Blob),
    ]
    operation.restype = ctypes.c_int
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    try:
        if not operation(
            ctypes.byref(source),
            None,
            ctypes.byref(extra),
            None,
            None,
            1,
            ctypes.byref(result),
        ):
            raise KeyStorageUnavailable()
        if not result.data or not 0 < result.size <= MAX_NATIVE_BYTES:
            raise KeyStorageUnavailable()
        return ctypes.string_at(result.data, result.size)
    finally:
        if result.data:
            kernel32.LocalFree(ctypes.cast(result.data, ctypes.c_void_p))


class NativeKeyStorage:
    def __init__(self, *, keychain: Path | None = None):
        # An explicit isolated keychain is useful for OS integration tests.
        self.keychain = keychain

    @property
    def backend(self):
        if sys.platform == "darwin":
            return "macos-keychain"
        if sys.platform == "win32":
            return "windows-dpapi"
        if sys.platform.startswith("linux"):
            return "linux-secret-service"
        raise KeyStorageUnavailable()

    def _lookup(self, vault_id):
        if self.backend == "macos-keychain":
            _mac_unlocked(self.keychain)
            args = [
                "/usr/bin/security",
                "find-generic-password",
                "-s",
                SERVICE,
                "-a",
                vault_id,
                "-w",
            ]
            if self.keychain is not None:
                args.append(str(self.keychain))
            result = _run(args)
            if result.returncode == 44:
                return None
        elif self.backend == "linux-secret-service":
            _linux_collection()
            result = _run(
                [
                    shutil.which("secret-tool"),
                    "lookup",
                    "service",
                    SERVICE,
                    "account",
                    vault_id,
                ]
            )
            if result.returncode == 1 and not result.stdout and not result.stderr:
                return None
        else:
            raise KeyStorageUnavailable()
        if result.returncode or len(result.stdout) > 46:
            raise KeyStorageUnavailable()
        try:
            encoded = result.stdout.strip()
            key = base64.b64decode(encoded, validate=True)
            if len(key) != 32 or base64.b64encode(key).decode() != encoded:
                raise KeyStorageUnavailable()
            return key
        except ValueError:
            raise KeyStorageUnavailable() from None

    def protect(self, vault_id, key):
        try:
            validate_vault_id(vault_id)
            if type(key) is not bytes or len(key) != 32:
                raise KeyStorageUnavailable()
            if self.backend == "windows-dpapi":
                protected = base64.b64encode(
                    _dpapi(key, vault_id, protect=True)
                ).decode()
            else:
                current = self._lookup(vault_id)
                if current is not None and not hmac.compare_digest(current, key):
                    raise KeyStorageUnavailable()
                if current is None:
                    value = base64.b64encode(key).decode()
                    if self.backend == "macos-keychain":
                        args = [
                            "add-generic-password",
                            "-s",
                            SERVICE,
                            "-a",
                            vault_id,
                            "-w",
                            value,
                        ]
                        if self.keychain is not None:
                            args.append(str(self.keychain))
                        macos_command(args)
                    else:
                        collection = _linux_collection()
                        result = _run(
                            [
                                shutil.which("secret-tool"),
                                "store",
                                "--label=Aikito encrypted remote",
                                "--collection",
                                collection,
                                "service",
                                SERVICE,
                                "account",
                                vault_id,
                            ],
                            input=value,
                        )
                        if result.returncode:
                            raise KeyStorageUnavailable()
                protected = None
            if self.load(vault_id, self.backend, protected) != key:
                raise KeyStorageUnavailable()
            return self.backend, protected
        except (OSError, ValueError, AttributeError):
            raise KeyStorageUnavailable() from None

    def load(self, vault_id, backend, protected):
        try:
            validate_vault_id(vault_id)
            if backend != self.backend:
                raise KeyStorageUnavailable()
            if backend == "windows-dpapi":
                if type(protected) is not str or len(protected) > MAX_NATIVE_BYTES:
                    raise KeyStorageUnavailable()
                encoded = base64.b64decode(protected, validate=True)
                if base64.b64encode(encoded).decode() != protected:
                    raise KeyStorageUnavailable()
                key = _dpapi(encoded, vault_id, protect=False)
            else:
                if protected is not None:
                    raise KeyStorageUnavailable()
                key = self._lookup(vault_id)
            if key is None or len(key) != 32:
                raise KeyStorageUnavailable()
            return key
        except (OSError, ValueError, AttributeError):
            raise KeyStorageUnavailable() from None
