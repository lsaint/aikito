"""Private endpoint and identity pin, excluded from resource transactions.

Atomic readers never create locks or recover journals. Lifecycle writes hold
the workspace writer lock, recover local state and preserve unresolved pending.
Only credential references may be persisted here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import parse_qsl

from ..skill_state import WorkspaceWriterLock
from .http_transport import validate_endpoint
from .pending_commit import PendingCommitStore
from .remote_access import RemoteBindingError
from .remote_store import InvalidContent, StoreIdentityMismatch
from .remote_wire import decode_state_json
from .replica_state import load_replica_state
from .resource_state import REMOTE_BINDING_STATE, state_path, valid_identity
from .transactions import atomic_text, atomic_unlink, entry_type


def validate_binding_endpoint(endpoint: str) -> None:
    try:
        url = validate_endpoint(endpoint)
    except ValueError:
        raise RemoteBindingError("Invalid remote binding endpoint") from None
    if url.scheme != "https" and url.hostname not in {"127.0.0.1", "::1", "localhost"}:
        raise RemoteBindingError("Remote credentials require HTTPS outside loopback")
    for key, _ in parse_qsl(url.query, keep_blank_values=True):
        name = key.lower().replace("-", "_")
        if any(
            part in name
            for part in ("token", "secret", "password", "credential", "auth")
        ) or name in {"key", "api_key", "apikey"}:
            raise RemoteBindingError("Credentials must not appear in endpoint queries")


@dataclass(frozen=True)
class RemoteAuth:
    type: str
    env: str

    def __post_init__(self) -> None:
        if (
            self.type != "bearer_env"
            or type(self.env) is not str
            or not re.fullmatch(r"[A-Z_][A-Z0-9_]*", self.env)
        ):
            raise RemoteBindingError("Invalid remote credential reference")


@dataclass(frozen=True)
class RemoteBinding:
    version: int
    endpoint: str
    sync_id: str
    auth: RemoteAuth

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1:
            raise RemoteBindingError("Unsupported remote binding version")
        validate_binding_endpoint(self.endpoint)
        if not valid_identity(self.sync_id) or not isinstance(self.auth, RemoteAuth):
            raise RemoteBindingError("Invalid remote binding identity or auth")

    def encode(self) -> str:
        return (
            json.dumps(
                {
                    "version": self.version,
                    "endpoint": self.endpoint,
                    "sync_id": self.sync_id,
                    "auth": {"type": self.auth.type, "env": self.auth.env},
                },
                sort_keys=True,
            )
            + "\n"
        )

    @classmethod
    def decode(cls, text: str | bytes) -> RemoteBinding:
        try:
            raw = decode_state_json(text)
            if type(raw) is not dict or set(raw) != {
                "version",
                "endpoint",
                "sync_id",
                "auth",
            }:
                raise RemoteBindingError("Invalid remote binding fields")
            auth = raw["auth"]
            if type(auth) is not dict or set(auth) != {"type", "env"}:
                raise RemoteBindingError("Invalid remote auth fields")
            return cls(
                raw["version"],
                raw["endpoint"],
                raw["sync_id"],
                RemoteAuth(auth["type"], auth["env"]),
            )
        except (InvalidContent, ValueError, TypeError, KeyError, RecursionError):
            # Malformed JSON exceptions can retain input containing secrets.
            raise RemoteBindingError("Invalid remote binding state") from None


def load_remote_binding(workspace: Path) -> RemoteBinding | None:
    path = state_path(workspace, REMOTE_BINDING_STATE)
    if entry_type(path) == "missing":
        return None
    return RemoteBinding.decode(path.read_bytes())


def save_remote_binding(
    workspace: Path, binding: RemoteBinding, *, home: Path | None = None
) -> None:
    """Create a binding only; callers obtain its identity through a remote read."""
    home = Path.home() if home is None else home
    with WorkspaceWriterLock(home):
        if load_remote_binding(workspace) is not None:
            raise RemoteBindingError("Remote binding already exists")
        pending = PendingCommitStore(workspace, home).load()
        replica, _ = load_replica_state(workspace)
        if (replica is not None and replica.sync_id != binding.sync_id) or (
            pending is not None and pending.request.expected.sync_id != binding.sync_id
        ):
            raise StoreIdentityMismatch(
                "Replica belongs to a different resource center"
            )
        atomic_text(
            state_path(workspace, REMOTE_BINDING_STATE, create=True), binding.encode()
        )


def remove_remote_binding(workspace: Path, *, home: Path | None = None) -> None:
    home = Path.home() if home is None else home
    with WorkspaceWriterLock(home):
        binding = load_remote_binding(workspace)
        if PendingCommitStore(workspace, home).load() is not None:
            raise RemoteBindingError("Cannot remove binding with a pending commit")
        if binding is not None:
            atomic_unlink(state_path(workspace, REMOTE_BINDING_STATE))
