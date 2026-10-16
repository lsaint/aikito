"""Construct a pinned remote without exposing transport or secrets to callers.

Opening is local-only so completed pending requests can be cleared offline.
Every instance verifies identity before any identity-bearing operation and
checks every returned read snapshot. Identity-free recovery may first restore
a backend whose verification read requires recovery.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from pathlib import Path

from ..skill_state import WorkspaceWriterLock
from .http_transport import HTTPTransport
from .payload import ResourcePayload
from .remote_access import (
    RemoteBindingExists,
    RemoteBindingMissing,
)
from .remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
    save_remote_binding,
    validate_binding_endpoint,
)
from .remote_credentials import resolve_credential
from .remote_format import require_encrypted_snapshot
from .remote_store import (
    RemoteSnapshot,
    RemoteStore,
    StoreIdentityMismatch,
)
from .serialized_remote import SerializedRemoteStore


def _open_remote(
    endpoint: str,
    auth: RemoteAuth,
    environ: Mapping[str, str] | None,
    *,
    home: Path | None = None,
) -> RemoteStore:
    validate_binding_endpoint(endpoint)
    token = resolve_credential(auth, environ, home=home)
    return SerializedRemoteStore(
        HTTPTransport(endpoint, authorization="Bearer " + token).exchange
    )


def open_bound_remote(
    workspace: Path,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteStore:
    binding = load_remote_binding(workspace)
    if binding is None:
        raise RemoteBindingMissing("Remote binding is missing")
    from .encrypted_sync import EncryptedRemote

    return EncryptedRemote(
        _open_remote(binding.endpoint, binding.auth, environ, home=home),
        binding.sync_id,
        workspace,
        Path.home() if home is None else home,
    )


def verify_remote_binding(
    binding: RemoteBinding,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteSnapshot:
    snapshot = BoundRemoteReader(
        _open_remote(binding.endpoint, binding.auth, environ, home=home),
        binding.sync_id,
    ).read()
    require_encrypted_snapshot(snapshot, allow_empty=True)
    return snapshot


def bind_remote(
    workspace: Path,
    endpoint: str,
    auth: RemoteAuth,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteBinding:
    home = Path.home() if home is None else home
    remote = _open_remote(endpoint, auth, environ, home=home)
    with WorkspaceWriterLock(home):
        if load_remote_binding(workspace) is not None:
            raise RemoteBindingExists("Remote binding already exists")
        snapshot = remote.read()
        require_encrypted_snapshot(snapshot, allow_empty=True)
        binding = RemoteBinding(1, endpoint, snapshot.sync_id, auth)
        save_remote_binding(workspace, binding, home=home)
        return binding


class BoundRemoteReader:
    """Explicit inspection can read encrypted data but has no write capability."""

    def __init__(self, remote: RemoteStore, sync_id: str):
        self._remote, self._sync_id = remote, sync_id
        self._verified = False

    def read(self) -> RemoteSnapshot:
        self._verified = False
        snapshot = self._remote.read()
        if snapshot.sync_id != self._sync_id:
            raise StoreIdentityMismatch("Remote binding identity mismatch")
        self._verified = True
        return snapshot

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]:
        if not self._verified:
            self.read()
        if expected.sync_id != self._sync_id:
            raise StoreIdentityMismatch("Remote binding identity mismatch")
        return self._remote.fetch(expected, ids)


def open_bound_reader(
    workspace: Path,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> BoundRemoteReader:
    binding = load_remote_binding(workspace)
    if binding is None:
        raise RemoteBindingMissing("Remote binding is missing")
    return BoundRemoteReader(
        _open_remote(binding.endpoint, binding.auth, environ, home=home),
        binding.sync_id,
    )
