"""Keep verified recovery codes in one owner-only file outside workspace resources.

Updates share the workspace writer lock and atomically replace the file, so
concurrent devices/workspaces cannot discard each other's recovery records.
Malformed or unsafe existing files are never overwritten.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from ..compat import (
    check_credential_permissions,
    get_workspace_config_dir,
    is_windows,
    secure_directory_permissions,
    secure_file_permissions,
)
from ..skill_state import WorkspaceWriterLock
from .encrypted_remote import decode_recovery_code, encode_recovery_code
from .transactions import entry_type

MAX_RECOVERY_FILE_BYTES = 1024 * 1024


def recovery_file_path(home: Path) -> Path:
    base = get_workspace_config_dir(home).expanduser()
    if not base.is_absolute():
        raise ValueError(
            "Recovery code storage requires an absolute configuration path."
        )
    return base.resolve() / "encrypted-remote" / "recovery.txt"


def _validate_record(sync_id: str, code: str) -> str:
    if (
        not isinstance(sync_id, str)
        or not sync_id
        or any(c.isspace() for c in sync_id)
        or len(sync_id) > 128
    ):
        raise ValueError("Invalid synchronization identity for recovery storage.")
    return encode_recovery_code(decode_recovery_code(code))


def _read(path: Path) -> dict[str, tuple[str, list[str], list[str]]]:
    kind = entry_type(path)
    if kind == "missing":
        return {}
    if kind != "file":
        raise ValueError("Unsafe recovery code file.")
    info = path.lstat()
    if info.st_nlink != 1 or not check_credential_permissions(path)[0]:
        raise ValueError("Recovery code file must be accessible only by its owner.")
    if not is_windows() and info.st_uid != os.getuid():
        raise ValueError("Recovery code file must belong to the current user.")
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(fd, "rb") as stream:
        opened = os.fstat(stream.fileno())
        if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            raise ValueError("Recovery code file changed while opening it.")
        raw = stream.read(MAX_RECOVERY_FILE_BYTES + 1)
    if len(raw) > MAX_RECOVERY_FILE_BYTES:
        raise ValueError("Recovery code file is too large.")
    records = {}
    for block in raw.decode("utf-8").strip().split("\n\n"):
        if not block:
            continue
        lines = block.splitlines()
        if (
            len(lines) < 2
            or not lines[0].startswith("sync_id: ")
            or not lines[1].startswith("code: ")
        ):
            raise ValueError("Invalid recovery code file format.")
        identity, code = lines[0][9:], lines[1][6:]
        if identity in records:
            raise ValueError("Duplicate identity in recovery code file.")
        code = _validate_record(identity, code)
        services, workspaces = [], []
        for line in lines[2:]:
            if line.startswith("service: "):
                value = line[9:]
                if not value or any(c.isspace() for c in value):
                    raise ValueError("Invalid service in recovery code file.")
                services.append(value)
            elif line.startswith("workspace: "):
                value = json.loads(line[11:])
                if not isinstance(value, str) or not value:
                    raise ValueError("Invalid workspace in recovery code file.")
                workspaces.append(value)
            else:
                raise ValueError("Invalid recovery code file format.")
        records[identity] = (code, services, workspaces)
    return records


def _prepare_directory(path: Path) -> None:
    current = Path(path.anchor)
    for part in path.parts[1:]:
        current /= part
        if entry_type(current) == "missing":
            current.mkdir(mode=0o700)
            if not secure_directory_permissions(current):
                raise ValueError("Cannot secure recovery code directory.")
        if entry_type(current) != "directory":
            raise ValueError("Unsafe recovery code directory.")
    if not is_windows() and path.stat().st_uid != os.getuid():
        raise ValueError("Recovery code directory must belong to the current user.")
    if not secure_directory_permissions(path):
        raise ValueError("Cannot secure recovery code directory.")


def save_recovery_code(
    sync_id: str, code: str, *, home: Path, workspace: Path, endpoint: str | None = None
) -> Path:
    """Upsert a successfully generated or verified code; never log its contents."""
    code = _validate_record(sync_id, code)
    path = recovery_file_path(home)
    if path.resolve().is_relative_to(workspace.resolve()):
        raise ValueError("Recovery codes must be stored outside the workspace.")
    with WorkspaceWriterLock(home):
        _prepare_directory(path.parent)
        records = _read(path)
        _, services, workspaces = records.get(sync_id, (None, [], []))
        services, workspaces = list(services), list(workspaces)
        if endpoint:
            parsed = urlsplit(endpoint)
            service = f"{parsed.scheme}://{parsed.netloc}"
            if parsed.username or parsed.password or not parsed.hostname:
                raise ValueError("Invalid service for recovery storage.")
            if service not in services:
                services.append(service)
        location = str(workspace.resolve())
        if location not in workspaces:
            workspaces.append(location)
        updated = (code, services, workspaces)
        if records.get(sync_id) == updated:
            return path
        records[sync_id] = updated
        blocks = []
        for identity, (value, services, workspaces) in records.items():
            lines = [f"sync_id: {identity}", f"code: {value}"]
            lines.extend(f"service: {service}" for service in services)
            lines.extend(
                "workspace: " + json.dumps(location, ensure_ascii=False)
                for location in workspaces
            )
            blocks.append("\n".join(lines))
        content = "\n\n".join(blocks) + "\n"
        if len(content.encode("utf-8")) > MAX_RECOVERY_FILE_BYTES:
            raise ValueError("Recovery code file is too large.")
        temporary = path.with_name(".recovery-" + uuid.uuid4().hex)
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as stream:
                if not secure_file_permissions(temporary):
                    raise ValueError("Cannot secure recovery code file.")
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            if not is_windows():
                fd = os.open(path.parent, os.O_RDONLY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)
    return path
