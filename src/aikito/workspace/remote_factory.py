"""Construct a pinned remote without exposing transport or secrets to callers.

Opening is local-only so completed pending requests can be cleared offline.
Every instance verifies identity before any identity-bearing operation and
checks every returned read snapshot. Identity-free recovery may first restore
a backend whose verification read requires recovery.
"""

from __future__ import annotations

import os
from collections.abc import Collection, Mapping
from pathlib import Path

from ..skill_state import WorkspaceWriterLock
from .http_transport import HTTPTransport
from .payload import ResourcePayload
from .remote_access import RemoteBindingError
from .remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
    save_remote_binding,
    validate_binding_endpoint,
)
from .remote_store import (
    CommitRequest,
    CommitResult,
    RemoteSnapshot,
    RecoveryRequired,
    RemoteStore,
    StoreIdentityMismatch,
)
from .serialized_remote import SerializedRemoteStore


def resolve_credential(
    auth: RemoteAuth, environ: Mapping[str, str] | None = None
) -> str:
    value = (os.environ if environ is None else environ).get(auth.env)
    if (
        type(value) is not str
        or not value
        or any(not 33 <= ord(c) <= 126 for c in value)
    ):
        raise RemoteBindingError("Remote credential is missing or invalid")
    return value


def _open_remote(
    endpoint: str, auth: RemoteAuth, environ: Mapping[str, str] | None
) -> RemoteStore:
    validate_binding_endpoint(endpoint)
    token = resolve_credential(auth, environ)
    return SerializedRemoteStore(
        HTTPTransport(endpoint, authorization="Bearer " + token).exchange
    )


class BoundRemote:
    """Verify the pin before sending operation identities to an endpoint."""

    def __init__(self, remote: RemoteStore, sync_id: str) -> None:
        self._remote = remote
        self._sync_id = sync_id
        self._verified = False

    def _check_identity(self, sync_id: str) -> None:
        if sync_id != self._sync_id:
            raise StoreIdentityMismatch("Remote binding identity mismatch")

    def _verify(self) -> None:
        if not self._verified:
            self.read()

    def validate_replica(self, local: Path) -> None:
        self._remote.validate_replica(local)

    def read(self) -> RemoteSnapshot:
        self._verified = False
        snapshot = self._remote.read()
        self._check_identity(snapshot.sync_id)
        self._verified = True
        return snapshot

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]:
        self._verify()
        self._check_identity(expected.sync_id)
        return self._remote.fetch(expected, ids)

    def commit(self, request: CommitRequest) -> CommitResult:
        self._verify()
        self._check_identity(request.expected.sync_id)
        result = self._remote.commit(request)
        self._check_identity(result.sync_id)
        return result

    def resolve_commit(
        self, sync_id: str, client_id: str, request_id: str, mutation_digest: str
    ) -> CommitResult | None:
        self._verify()
        self._check_identity(sync_id)
        result = self._remote.resolve_commit(
            sync_id, client_id, request_id, mutation_digest
        )
        if result is not None:
            self._check_identity(result.sync_id)
        return result

    def recover(self) -> bool:
        # Recovery carries no identity, so it may restore an unreadable backend
        # before the pin is verified; the verification still precedes return.
        if not self._verified:
            try:
                self.read()
            except RecoveryRequired:
                recovered = self._remote.recover()
                self.read()
                return recovered
        return self._remote.recover()


def open_bound_remote(
    workspace: Path, *, environ: Mapping[str, str] | None = None
) -> RemoteStore:
    binding = load_remote_binding(workspace)
    if binding is None:
        raise RemoteBindingError("Remote binding is missing")
    return BoundRemote(
        _open_remote(binding.endpoint, binding.auth, environ), binding.sync_id
    )


def verify_remote_binding(
    binding: RemoteBinding, *, environ: Mapping[str, str] | None = None
) -> RemoteSnapshot:
    return BoundRemote(
        _open_remote(binding.endpoint, binding.auth, environ), binding.sync_id
    ).read()


def bind_remote(
    workspace: Path,
    endpoint: str,
    auth: RemoteAuth,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteBinding:
    remote = _open_remote(endpoint, auth, environ)
    home = Path.home() if home is None else home
    with WorkspaceWriterLock(home):
        if load_remote_binding(workspace) is not None:
            raise RemoteBindingError("Remote binding already exists")
        snapshot = remote.read()
        binding = RemoteBinding(1, endpoint, snapshot.sync_id, auth)
        save_remote_binding(workspace, binding, home=home)
        return binding
