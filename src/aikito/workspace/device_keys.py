"""Owner-only device keys outside workspace resources and Git.

Records are immutable per vault. New records are published atomically without
replacing an existing key; recovery must never silently switch vault material.
"""

from __future__ import annotations

import base64
import hmac
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
from .encrypted_remote import KeyStorageUnavailable, validate_vault_id


class DeviceKeyStore:
    def __init__(
        self,
        root: Path | None = None,
        *,
        home: Path | None = None,
        workspace: Path | None = None,
    ):
        base = get_workspace_config_dir(Path.home() if home is None else home)
        self.root = (
            base / "encrypted-remote" / "keys" if root is None else root
        ).expanduser()
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

    def load(self, vault_id: str) -> bytes | None:
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
                encoded = stream.read(46)
            raw = base64.b64decode(encoded.strip(), validate=True)
            if len(raw) != 32 or encoded != base64.b64encode(raw) + b"\n":
                raise KeyStorageUnavailable()
            return raw
        except (OSError, ValueError):
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
