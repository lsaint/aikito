"""Explicit local approval of immutable requests selected on the account website.

Polling never exports keys. A random, expiring confirmation token binds the exact
request displayed locally; partial or unknown submissions are not retried.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
import time
from pathlib import Path

from .skill_state import WorkspaceWriterLock
from .workspace.access_catalog import (
    AccessRequestError,
    collections,
    exchange,
    publish_catalog,
    target,
)
from .workspace.device_keys import DeviceKeyStore
from .workspace.encrypted_discovery import unlock_encrypted_snapshot
from .workspace.recipient_keys import fetch_recipient
from .workspace.remote_export import submit_bound_export
from .workspace.remote_factory import BoundRemoteReader, _open_remote
from .workspace.remote_wire import _canonical
from .workspace.resource_state import state_path
from .workspace.scoped_export import _authorization, export_bound_scoped_keys

ConsoleAccessError = AccessRequestError


class ConsoleAccess:
    def __init__(self, workspace, home):
        self.workspace, self.home = workspace, home
        self.token = secrets.token_urlsafe(32)
        self._shown = {}
        self._consumed = set()
        self._lock = threading.Lock()
        self._last_catalog = None

    def pending(self):
        with self._lock:
            binding, origin, store = target(self.workspace)
            if self._last_catalog is None or time.monotonic() - self._last_catalog > 60:
                with WorkspaceWriterLock(self.home):
                    publish_catalog(self.workspace, self.home)
                self._last_catalog = time.monotonic()
            result = exchange(self.workspace, self.home, "requests")
            allowed = set(collections(self.workspace))
            journal = state_path(
                self.workspace, ".local/state/aikito/access-confirmations.json"
            )
            if journal.exists():
                consumed = json.loads(journal.read_text())
                self._consumed.update(
                    key
                    for key, deadline in consumed.items()
                    if type(deadline) is int and deadline > time.time()
                )
            pending = []
            current = {}
            remote_requests = result.get("requests", [])
            if not isinstance(remote_requests, list) or len(remote_requests) > 32:
                raise ConsoleAccessError("Invalid approval response")
            for request in remote_requests:
                if not re.fullmatch(
                    r"[a-f0-9]{64}", str(request.get("request_id", ""))
                ):
                    raise ConsoleAccessError("Invalid approval identity")
                if (
                    request.get("cancelled")
                    or request.get("expires_at", 0) <= time.time()
                ):
                    continue
                if (
                    request.get("store_id") != store
                    or request.get("sync_id") != binding.sync_id
                ):
                    continue
                items = request.get("items", [])
                # A partially consumed group must be reviewed online, never resumed
                # automatically with newly generated or reused one-time packages.
                if (
                    not items
                    or len(items) > 32
                    or any(item.get("status") != "pending" for item in items)
                ):
                    continue
                identities = set()
                grant_ids = set()
                for item in items:
                    candidate = item["authorization"]
                    permission = (
                        "write"
                        if candidate.get("permissions") == ["read", "write"]
                        else "read"
                    )
                    auth = _authorization(
                        _canonical(candidate), int(time.time()), permission
                    )
                    if (
                        auth["collection_id"] not in allowed
                        or auth["collection_id"] in identities
                        or any(
                            auth[k] != request[v]
                            for k, v in (
                                ("account_id", "account_id"),
                                ("store_id", "store_id"),
                                ("sync_id", "sync_id"),
                                ("vault_id", "vault_id"),
                            )
                        )
                    ):
                        raise ConsoleAccessError(
                            "Approval does not match this workspace"
                        )
                    if (
                        auth["expires_at"] > int(time.time()) + 30 * 86400
                        or auth["grant_id"] in grant_ids
                    ):
                        raise ConsoleAccessError(
                            "Invalid authorization lifetime or repeated grant"
                        )
                    identities.add(auth["collection_id"])
                    grant_ids.add(auth["grant_id"])
                digest = hashlib.sha256(_canonical(request)).hexdigest()
                if digest in self._consumed:
                    continue
                old = self._shown.get(digest)
                confirmation = (
                    old[0]
                    if old and old[1] > time.monotonic()
                    else secrets.token_urlsafe(32)
                )
                current[digest] = (
                    confirmation,
                    time.monotonic() + 300,
                    binding,
                    request,
                )
                pending.append(
                    {**request, "recipient": origin, "confirmation": confirmation}
                )
            self._shown = current
            return {"origin": origin, "requests": pending}

    def perform(self, identity, action, body):
        if action not in {"confirm", "reject"} or set(body) != {"confirmation"}:
            raise ConsoleAccessError("Invalid confirmation")
        with self._lock:
            selected = next(
                (
                    (digest, entry)
                    for digest, entry in self._shown.items()
                    if entry[3]["request_id"] == identity
                    and secrets.compare_digest(entry[0], str(body["confirmation"]))
                ),
                None,
            )
            if selected is None or selected[1][1] <= time.monotonic():
                raise ConsoleAccessError("Refresh the request before confirming")
            digest, (_, _, binding, request) = selected
            self._consumed.add(digest)
            if len(self._consumed) > 1024:
                raise ConsoleAccessError("Restart the console to review more requests")
        with WorkspaceWriterLock(self.home):
            current, origin, _store = target(self.workspace)
            if current != binding or request["expires_at"] <= time.time():
                raise ConsoleAccessError("Remote binding changed or request expired")
            journal = state_path(
                self.workspace,
                ".local/state/aikito/access-confirmations.json",
                create=True,
            )
            consumed = json.loads(journal.read_text()) if journal.exists() else {}
            now = int(time.time())
            consumed = {
                key: expiry
                for key, expiry in consumed.items()
                if type(expiry) is int and expiry > now
            }
            if digest in consumed:
                raise ConsoleAccessError(
                    "This request was already attempted; check its online status"
                )
            if len(consumed) >= 1024:
                raise ConsoleAccessError("Too many outstanding confirmation records")
            consumed[digest] = request["expires_at"]
            temporary = journal.with_name(journal.name + "." + secrets.token_hex(8))
            descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(_canonical(consumed))
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, journal)
                if os.name != "nt":
                    directory = os.open(journal.parent, os.O_RDONLY)
                    try:
                        os.fsync(directory)
                    finally:
                        os.close(directory)
            finally:
                temporary.unlink(missing_ok=True)
            # Recheck cancellation and the complete immutable body immediately
            # before accessing device keys, independent of the browser payload.
            remote = exchange(self.workspace, self.home, "requests")["requests"]
            if not any(_canonical(value) == _canonical(request) for value in remote):
                raise ConsoleAccessError("Request changed; refresh its status")
            if action == "reject":
                return exchange(
                    self.workspace, self.home, f"requests/{identity}/reject", post=True
                )
            reader = BoundRemoteReader(
                _open_remote(binding.endpoint, binding.auth, None, home=self.home),
                binding.sync_id,
            )
            vault = unlock_encrypted_snapshot(
                reader,
                reader.read(),
                DeviceKeyStore(home=self.home, workspace=self.workspace),
            )
            if vault.vault_id != request["vault_id"]:
                raise ConsoleAccessError("Vault identity changed")
            results = []
            with tempfile.TemporaryDirectory(prefix="aikito-approval-") as folder:
                folder = Path(folder)
                recipient = folder / "recipient.json"
                fetch_recipient(
                    self.workspace, self.home, origin=origin, output=recipient
                )
                for item in request["items"]:
                    auth = item["authorization"]
                    permission = (
                        "write" if auth["permissions"] == ["read", "write"] else "read"
                    )
                    document, package = folder / "request.json", folder / "sealed.json"
                    document.write_bytes(_canonical(auth))
                    export_bound_scoped_keys(
                        self.workspace,
                        self.home,
                        collection_id=auth["collection_id"],
                        recipient=recipient,
                        authorization=document,
                        output=package,
                        permission=permission,
                    )
                    results.append(
                        submit_bound_export(
                            self.workspace,
                            self.home,
                            origin=origin,
                            package=package,
                            authorization=document,
                            expected_account=request["account_id"],
                            collection=auth["collection_id"],
                            permission=permission,
                        )
                    )
                    package.unlink(missing_ok=True)
            return {"status": "active", "grants": results}
