"""Local consent binds one project, recipient, account and permission per intent.

Browser sessions remain at the recipient origin. Only public challenges and
requests cross the window boundary; existing claim/export/submit operations own
credential delivery and cryptography. Intents expire and never survive restart.
"""

from __future__ import annotations

import re
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from . import console_access_history
from .skill_state import WorkspaceWriterLock
from .workspace.device_keys import DeviceKeyStore
from .workspace.encrypted_discovery import unlock_encrypted_snapshot
from .workspace.http_transport import validate_endpoint
from .workspace.recipient_keys import canonical_origin, fetch_recipient
from .workspace.remote_binding import load_remote_binding
from .workspace.remote_claim import claim_bound_store
from .workspace.remote_export import submit_bound_export
from .workspace.remote_factory import BoundRemoteReader, _open_remote
from .workspace.remote_wire import _canonical
from .workspace.scoped_export import (
    _authorization,
    _collection,
    export_bound_scoped_keys,
)


class ConsoleAccessError(ValueError):
    pass


@dataclass
class AccessIntent:
    origin: str
    collection: str
    permission: str
    sync_id: str
    vault_id: str
    store_id: str
    expires: float
    binding: object
    account: str | None = None
    used: bool = False
    busy: bool = False


class ConsoleAccess:
    def __init__(self, workspace, home):
        self.workspace, self.home = workspace, home
        self.token = secrets.token_urlsafe(32)
        self._intents = {}
        self._lock = threading.Lock()

    def begin(self, project, origin, permission):
        collection = f"project/{project}/memory"
        _collection(collection)
        if not (self.workspace / "projects" / project / "agent.toml").is_file():
            raise ConsoleAccessError("Project not found")
        if permission not in {"read", "write"}:
            raise ConsoleAccessError("Select read or write permission")
        origin = canonical_origin(origin)
        binding = load_remote_binding(self.workspace)
        if binding is None:
            raise ConsoleAccessError("Bind an encrypted Remote first")
        endpoint = validate_endpoint(binding.endpoint)
        match = re.fullmatch(
            r"/v1/stores/([a-z0-9][a-z0-9_-]{0,62})/remote", endpoint.path
        )
        if (
            endpoint.scheme != "https"
            or endpoint.query
            or "?" in binding.endpoint
            or not match
        ):
            raise ConsoleAccessError("A bound HTTPS Store is required")
        reader = BoundRemoteReader(
            _open_remote(binding.endpoint, binding.auth, None, home=self.home),
            binding.sync_id,
        )
        vault = unlock_encrypted_snapshot(
            reader,
            reader.read(),
            DeviceKeyStore(home=self.home, workspace=self.workspace),
        )
        with self._lock:
            self._intents = {
                key: value
                for key, value in self._intents.items()
                if value.expires > time.monotonic()
            }
            if len(self._intents) >= 8:
                raise ConsoleAccessError(
                    "Too many access requests; try again in 15 minutes"
                )
            identity = secrets.token_urlsafe(32)
            intent = AccessIntent(
                origin,
                collection,
                permission,
                binding.sync_id,
                vault.vault_id,
                match[1],
                time.monotonic() + 900,
                binding,
            )
            self._intents[identity] = intent
        return {
            "intent_id": identity,
            "origin": origin,
            "context": {
                "store_id": intent.store_id,
                "sync_id": intent.sync_id,
                "vault_id": intent.vault_id,
                "collection_id": collection,
                "permissions": ["read"] if permission == "read" else ["read", "write"],
            },
        }

    def history(self, project):
        collection = f"project/{project}/memory"
        _collection(collection)
        return {
            "records": console_access_history.history(
                self.workspace, collection, load_remote_binding(self.workspace)
            )
        }

    def observe(self, identity, body):
        if set(body) != {"account_id", "grants"}:
            raise ConsoleAccessError("Invalid access observation")
        with self._lock:
            intent = self._intents.get(identity)
            if (
                intent is None
                or intent.expires <= time.monotonic()
                or (intent.account is not None and intent.account != body["account_id"])
            ):
                raise ConsoleAccessError("Access request expired or account changed")
        with WorkspaceWriterLock(self.home):
            if load_remote_binding(self.workspace) != intent.binding:
                raise ConsoleAccessError("Remote binding changed; reconnect")
            grants = body["grants"]
            if not isinstance(grants, list) or any(
                not isinstance(grant, dict)
                or grant.get("store_id") != intent.store_id
                or grant.get("collection_id") != intent.collection
                for grant in grants
            ):
                raise ConsoleAccessError("Observation does not match this project")
            console_access_history.record(
                self.workspace, intent, body["account_id"], grants
            )
        return {"saved": True}

    def perform(self, identity, action, body):
        if action == "observe":
            return self.observe(identity, body)
        with self._lock:
            intent = self._intents.get(identity)
            if intent is None or intent.expires <= time.monotonic():
                raise ConsoleAccessError("Access request expired; reconnect")
            if intent.busy or intent.used:
                raise ConsoleAccessError(
                    "Access request already submitted; refresh its status"
                )
            if load_remote_binding(self.workspace) != intent.binding:
                raise ConsoleAccessError("Remote binding changed; reconnect")
            if action == "approve":
                if (
                    set(body) != {"account_id"}
                    or not isinstance(body["account_id"], str)
                    or not re.fullmatch(
                        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", body["account_id"]
                    )
                ):
                    raise ConsoleAccessError("Invalid account")
                if intent.account is not None:
                    raise ConsoleAccessError(
                        "Account already confirmed; reconnect to change it"
                    )
                intent.account = body["account_id"]
                return {"approved": True}
            if intent.account is None or action not in {"claim", "submit"}:
                raise ConsoleAccessError("Confirm the account and project first")
            intent.busy = True
            if action == "submit":
                # Uncertain submissions must be checked in the recipient's list,
                # never automatically retried with the same one-time request.
                intent.used = True
        try:
            with WorkspaceWriterLock(self.home):
                if load_remote_binding(self.workspace) != intent.binding:
                    raise ConsoleAccessError("Remote binding changed; reconnect")
                with tempfile.TemporaryDirectory(prefix="aikito-access-") as folder:
                    folder = Path(folder)
                    document = folder / "request.json"
                    document.write_bytes(_canonical(body))
                    if action == "claim":
                        return claim_bound_store(
                            self.workspace, document, intent.account, home=self.home
                        )
                    auth = _authorization(
                        _canonical(body), int(time.time()), intent.permission
                    )
                    if any(
                        auth[key] != value
                        for key, value in {
                            "account_id": intent.account,
                            "store_id": intent.store_id,
                            "sync_id": intent.sync_id,
                            "vault_id": intent.vault_id,
                            "collection_id": intent.collection,
                        }.items()
                    ):
                        raise ConsoleAccessError(
                            "Authorization does not match your confirmed selection"
                        )
                    if auth["expires_at"] > int(time.time()) + 30 * 86400:
                        raise ConsoleAccessError("Authorization exceeds 30 days")
                    recipient, package = (
                        folder / "recipient.json",
                        folder / "sealed.json",
                    )
                    fetch_recipient(
                        self.workspace,
                        self.home,
                        origin=intent.origin,
                        output=recipient,
                    )
                    export_bound_scoped_keys(
                        self.workspace,
                        self.home,
                        collection_id=intent.collection,
                        recipient=recipient,
                        authorization=document,
                        output=package,
                        permission=intent.permission,
                    )
                    receipt = submit_bound_export(
                        self.workspace,
                        self.home,
                        origin=intent.origin,
                        package=package,
                        authorization=document,
                        expected_account=intent.account,
                        collection=intent.collection,
                        permission=intent.permission,
                    )
                    if receipt.get("status") == "active":
                        try:
                            console_access_history.record(
                                self.workspace,
                                intent,
                                intent.account,
                                [{**auth, "status": "active"}],
                            )
                        except (OSError, ValueError, TypeError, KeyError, RuntimeError):
                            # Saving a display hint cannot invalidate a committed grant.
                            return {**receipt, "history_saved": False}
                    return receipt
        finally:
            with self._lock:
                intent.busy = False
