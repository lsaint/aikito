"""Owner-only device keys outside workspace resources and Git.

Records are immutable per vault. New records are published atomically without
replacing an existing key; recovery must never silently switch vault material.
"""

from __future__ import annotations

import base64
import hmac
import hashlib
import re
import os
import stat
import uuid
from pathlib import Path

from ..compat import (
    check_credential_permissions,
    get_workspace_config_dir,
    is_windows,
    secure_directory_permissions,
    secure_file_permissions,
)
from ..skill_state import WorkspaceWriterLock
from .encrypted_remote import (
    KeyStorageUnavailable,
    EncryptedRemoteError,
    validate_vault_id,
    _parse,
)
from .remote_wire import _canonical
from .native_keys import NativeKeyStorage

MAX_KEY_RECORD_BYTES = 65536


def _commitment(vault_id, key):
    return hmac.new(
        key,
        _canonical(
            {"domain": "aikito.device-key-record", "vault_id": vault_id, "version": 2}
        ),
        hashlib.sha256,
    ).hexdigest()


def _decode_record(vault_id, encoded):
    if len(encoded) > MAX_KEY_RECORD_BYTES:
        raise KeyStorageUnavailable()
    if not encoded.startswith(b"{"):
        key = base64.b64decode(encoded.strip(), validate=True)
        if len(key) != 32 or encoded != base64.b64encode(key) + b"\n":
            raise KeyStorageUnavailable()
        return None, key
    record = _parse(
        encoded, ("version", "vault_id", "backend", "commitment", "protected_key")
    )
    if (
        type(record["version"]) is not int
        or record["version"] != 2
        or record["vault_id"] != vault_id
        or record["backend"]
        not in ("macos-keychain", "linux-secret-service", "windows-dpapi")
        or type(record["commitment"]) is not str
        or re.fullmatch(r"[0-9a-f]{64}", record["commitment"]) is None
    ):
        raise KeyStorageUnavailable()
    if record["backend"] == "windows-dpapi":
        if type(record["protected_key"]) is not str or not record["protected_key"]:
            raise KeyStorageUnavailable()
    elif record["protected_key"] is not None:
        raise KeyStorageUnavailable()
    return record, None


class DeviceKeyStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        home: Path | None = None,
        workspace: Path | None = None,
        native=None,
    ):
        self.home = Path.home() if home is None else home
        self.native = NativeKeyStorage() if native is None else native
        base = get_workspace_config_dir(self.home)
        self.root = (
            base / "encrypted-remote" / "keys" if root is None else root
        ).expanduser().resolve()
        if not self.root.is_absolute() or ".." in self.root.parts:
            raise KeyStorageUnavailable()
        if workspace is not None:
            resolved = workspace.resolve()
            if self.root.resolve().is_relative_to(resolved):
                raise KeyStorageUnavailable()

    def path(self, vault_id: str) -> Path:
        try:
            validate_vault_id(vault_id)
            return self.root / f"{vault_id}.key"
        except ValueError:
            raise KeyStorageUnavailable() from None

    def _directories(self, create=False):
        current = Path(self.root.anchor)
        for part in self.root.parts[1:]:
            current /= part
            try:
                info = current.lstat()
            except FileNotFoundError:
                if not create:
                    return False
                current.mkdir(mode=0o700)
                if not secure_directory_permissions(current):
                    raise KeyStorageUnavailable()
                info = current.lstat()
            if not stat.S_ISDIR(info.st_mode) or getattr(
                info, "st_file_attributes", 0
            ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
                raise KeyStorageUnavailable()
        info = self.root.lstat()
        if is_windows():
            if not check_credential_permissions(self.root)[0]:
                raise KeyStorageUnavailable()
        elif info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise KeyStorageUnavailable()
        return True

    def _read(self, vault_id: str) -> bytes | None:
        try:
            path = self.path(vault_id)
            if not self._directories():
                return None
            try:
                info = path.lstat()
            except FileNotFoundError:
                return None
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_nlink != 1
                or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            ):
                raise KeyStorageUnavailable()
            if is_windows():
                if not check_credential_permissions(path)[0]:
                    raise KeyStorageUnavailable()
            elif info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600:
                raise KeyStorageUnavailable()
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
            with os.fdopen(descriptor, "rb") as stream:
                opened = os.fstat(stream.fileno())
                if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                    raise KeyStorageUnavailable()
                encoded = stream.read(MAX_KEY_RECORD_BYTES + 1)
            if len(encoded) > MAX_KEY_RECORD_BYTES:
                raise KeyStorageUnavailable()
            return encoded
        except (OSError, ValueError):
            raise KeyStorageUnavailable() from None

    def load(self, vault_id: str) -> bytes | None:
        try:
            encoded = self._read(vault_id)
            if encoded is None:
                return None
            record, key = _decode_record(vault_id, encoded)
            if record is not None:
                key = self.native.load(
                    vault_id, record["backend"], record["protected_key"]
                )
                if not hmac.compare_digest(
                    record["commitment"], _commitment(vault_id, key)
                ):
                    raise KeyStorageUnavailable()
            return key
        except (OSError, ValueError, EncryptedRemoteError):
            raise KeyStorageUnavailable() from None

    def storage(self, vault_id):
        try:
            encoded = self._read(vault_id)
            if encoded is None:
                return None
            record, _ = _decode_record(vault_id, encoded)
            return "file" if record is None else record["backend"]
        except (OSError, ValueError, EncryptedRemoteError):
            raise KeyStorageUnavailable() from None

    def _replace(self, vault_id, before, encoded):
        temporary = self.root / (".key-" + uuid.uuid4().hex)
        try:
            if self._read(vault_id) != before:
                raise KeyStorageUnavailable()
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                if not secure_file_permissions(temporary):
                    raise KeyStorageUnavailable()
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            if self._read(vault_id) != before:
                raise KeyStorageUnavailable()
            os.replace(temporary, self.path(vault_id))
            if not is_windows():
                fd = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)

    def set_storage(self, vault_id, backend):
        """Explicitly change the active source without changing the Vault key."""
        try:
            if backend not in ("file", "native"):
                raise KeyStorageUnavailable()
            with WorkspaceWriterLock(self.home):
                before = self._read(vault_id)
                key = self.load(vault_id)
                if key is None:
                    raise KeyStorageUnavailable()
                current = self.storage(vault_id)
                if (backend == "file" and current == "file") or (
                    backend == "native" and current != "file"
                ):
                    return self.path(vault_id)
                if backend == "file":
                    encoded = base64.b64encode(key) + b"\n"
                else:
                    selected, protected = self.native.protect(vault_id, key)
                    if not hmac.compare_digest(
                        self.native.load(vault_id, selected, protected), key
                    ):
                        raise KeyStorageUnavailable()
                    encoded = _canonical(
                        {
                            "version": 2,
                            "vault_id": vault_id,
                            "backend": selected,
                            "commitment": _commitment(vault_id, key),
                            "protected_key": protected,
                        }
                    )
                    _decode_record(vault_id, encoded)
                self._replace(vault_id, before, encoded)
                return self.path(vault_id)
        except (OSError, ValueError, RuntimeError, EncryptedRemoteError):
            raise KeyStorageUnavailable() from None

    def recover_file(self, vault_id, key):
        """Use a separately verified recovery key to explicitly restore file storage."""
        try:
            if type(key) is not bytes or len(key) != 32:
                raise KeyStorageUnavailable()
            with WorkspaceWriterLock(self.home):
                before = self._read(vault_id)
                if before is None:
                    return self.save(vault_id, key)
                record, current = _decode_record(vault_id, before)
                if record is None:
                    if not hmac.compare_digest(current, key):
                        raise KeyStorageUnavailable()
                    return self.path(vault_id)
                if not hmac.compare_digest(
                    record["commitment"], _commitment(vault_id, key)
                ):
                    raise KeyStorageUnavailable()
                self._replace(vault_id, before, base64.b64encode(key) + b"\n")
                return self.path(vault_id)
        except (OSError, ValueError, RuntimeError, EncryptedRemoteError):
            raise KeyStorageUnavailable() from None

    def save(self, vault_id: str, vrk: bytes) -> Path:
        temporary = None
        try:
            if type(vrk) is not bytes or len(vrk) != 32:
                raise KeyStorageUnavailable()
            path = self.path(vault_id)
            self._directories(create=True)
            current = self.load(vault_id)
            if current is not None:
                if not hmac.compare_digest(current, vrk):
                    raise KeyStorageUnavailable()
                return path
            temporary = self.root / f".key-{uuid.uuid4().hex}"
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "wb") as stream:
                if not secure_file_permissions(temporary):
                    raise KeyStorageUnavailable()
                stream.write(base64.b64encode(vrk) + b"\n")
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, path)
            except FileExistsError:
                pass
            temporary.unlink()
            temporary = None
            if not is_windows():
                descriptor = os.open(self.root, os.O_RDONLY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
            current = self.load(vault_id)
            if current is None or not hmac.compare_digest(current, vrk):
                raise KeyStorageUnavailable()
            return path
        except (OSError, ValueError):
            raise KeyStorageUnavailable() from None
        finally:
            if temporary is not None:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
