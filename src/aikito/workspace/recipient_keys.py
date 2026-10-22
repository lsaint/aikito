"""Fetch an HTTPS recipient and pin its identity outside workspace resources.

First use trusts the authenticated TLS origin. Later identity changes stop until
explicitly accepted. This discovers a public key; it does not authorize access.
"""

from __future__ import annotations

import hashlib
import http.client
import os
import ssl
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
from .http_transport import validate_endpoint
from .remote_wire import _canonical, decode_state_json
from .scoped_export import _recipient, _publish

RECIPIENT_PATH = "/v1/keys/recipient"
_LIMIT = 4096


class RecipientKeyError(Exception):
    pass


def canonical_origin(value):
    endpoint = validate_endpoint(value)
    if (
        endpoint.scheme != "https"
        or endpoint.path
        or endpoint.query
        or any(c in value for c in "?#")
        or not value.isascii()
        or endpoint.netloc.endswith(":")
    ):
        raise ValueError()
    host = endpoint.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    if endpoint.port not in (None, 443):
        authority += f":{endpoint.port}"
    return "https://" + authority


def _fetch(origin):
    endpoint = validate_endpoint(origin)
    connection = http.client.HTTPSConnection(
        endpoint.hostname,
        endpoint.port or 443,
        timeout=10,
        context=ssl.create_default_context(),
    )
    connection.set_debuglevel(0)
    try:
        connection.request(
            "GET",
            RECIPIENT_PATH,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            if response.status != 200:
                raise ValueError()
            lengths = response.headers.get_all("Content-Length", [])
            transfers = response.headers.get_all("Transfer-Encoding", [])
            content = response.headers.get_all("Content-Type", [])
            encodings = response.headers.get_all("Content-Encoding", [])
            if (
                len(lengths) > 1
                or transfers
                or len(content) != 1
                or content[0] != "application/json"
                or (encodings and encodings != ["identity"])
            ):
                raise ValueError()
            declared = None
            if lengths:
                if not lengths[0].isascii() or not lengths[0].isdecimal():
                    raise ValueError()
                declared = int(lengths[0])
                if declared > _LIMIT:
                    raise ValueError()
            raw = response.read(_LIMIT + 1)
            if len(raw) > _LIMIT or (declared is not None and len(raw) != declared):
                raise ValueError()
            document, public = _recipient(raw)
            if public == bytes(32):
                raise ValueError()
            return document
    finally:
        connection.close()


def _safe(info):
    return not getattr(info, "st_file_attributes", 0) & getattr(
        stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
    )


def _directories(root):
    current = Path(root.anchor)
    for part in root.parts[1:]:
        current /= part
        try:
            info = current.lstat()
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            if not secure_directory_permissions(current):
                raise ValueError()
            info = current.lstat()
        if not stat.S_ISDIR(info.st_mode) or not _safe(info):
            raise ValueError()
    info = root.lstat()
    if is_windows():
        if not check_credential_permissions(root)[0]:
            raise ValueError()
    elif info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError()


def _read(path, origin):
    try:
        before = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not _safe(before):
        raise ValueError()
    if is_windows():
        if not check_credential_permissions(path)[0]:
            raise ValueError()
    elif before.st_uid != os.getuid() or stat.S_IMODE(before.st_mode) != 0o600:
        raise ValueError()
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    with os.fdopen(descriptor, "rb") as stream:
        after = os.fstat(stream.fileno())
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError()
        raw = stream.read(_LIMIT + 1)
    if len(raw) > _LIMIT:
        raise ValueError()
    record = decode_state_json(raw)
    if (
        type(record) is not dict
        or set(record) != {"version", "origin", "recipient"}
        or type(record["version"]) is not int
        or record["version"] != 1
        or record["origin"] != origin
        or _canonical(record) != raw
    ):
        raise ValueError()
    recipient, _ = _recipient(_canonical(record["recipient"]))
    return recipient


def _sync(root):
    if not is_windows():
        descriptor = os.open(root, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def _write(path, encoded):
    temporary = path.with_name(".recipient-pin-" + uuid.uuid4().hex)
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            if not secure_file_permissions(temporary):
                raise ValueError()
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _sync(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fetch_recipient(workspace, home, *, origin, output=None, accept_key_change=False):
    """Persist trust before publishing an immutable recipient document for export.

    Omitting output records trust without exporting a separate public document.
    A failed output write may follow a successful pin; retry with a fresh output
    path. Fetching never resolves Store credentials or changes Remote bindings.
    """
    try:
        origin = canonical_origin(origin)
        root = get_workspace_config_dir(home) / "remote-recipients"
        if not root.is_absolute() or ".." in root.parts:
            raise ValueError()
        if root.resolve().is_relative_to(Path(workspace).resolve()):
            raise ValueError()
        if output is not None:
            output = Path(output)
            if os.path.lexists(output):
                raise ValueError()
        document = _fetch(origin)
        _directories(root)
        path = root / (hashlib.sha256(origin.encode("ascii")).hexdigest() + ".json")
        with WorkspaceWriterLock(home):
            previous = _read(path, origin)
            changed = previous is not None and previous != document
            if changed and not accept_key_change:
                raise RecipientKeyError(
                    "recipient_key_changed: explicit --accept-key-change required"
                )
            if previous != document:
                _write(
                    path,
                    _canonical({"version": 1, "origin": origin, "recipient": document}),
                )
            # Never expose an output before its corresponding trust record exists.
            if output is not None:
                _publish(output, _canonical(document))
        return document
    except RecipientKeyError:
        raise
    except Exception:
        # Provider bodies, URLs, certificate errors and local paths are untrusted.
        raise RecipientKeyError("recipient_key_unavailable") from None


def require_pinned_recipient(workspace, home, origin):
    """Require a fresh HTTPS document matching existing trust; never auto-pin."""
    try:
        origin = canonical_origin(origin)
        root = get_workspace_config_dir(home) / "remote-recipients"
        if (
            not root.is_absolute()
            or ".." in root.parts
            or not root.exists()
            or root.resolve().is_relative_to(Path(workspace).resolve())
        ):
            raise ValueError()
        document = _fetch(origin)
        _directories(root)
        path = root / (hashlib.sha256(origin.encode("ascii")).hexdigest() + ".json")
        with WorkspaceWriterLock(home):
            previous = _read(path, origin)
            if previous is None or previous != document:
                raise RecipientKeyError("recipient_key_changed_or_untrusted")
        return origin, document
    except RecipientKeyError:
        raise
    except Exception:
        raise RecipientKeyError("recipient_key_unavailable") from None
