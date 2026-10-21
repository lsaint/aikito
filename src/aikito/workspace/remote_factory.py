"""Construct a pinned remote without exposing transport or secrets to callers.

Opening is local-only so completed pending requests can be cleared offline.
Every instance verifies identity before any identity-bearing operation and
checks every returned read snapshot. Identity-free recovery may first restore
a backend whose verification read requires recovery.
"""

from __future__ import annotations

from collections.abc import Callable, Collection, Mapping
from pathlib import Path

from ..skill_state import WorkspaceWriterLock
from .device_keys import DeviceKeyStore
from .encrypted_remote import (
    decode_recovery_code,
    metadata_vault_id,
    recover_vault,
)
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
    remove_remote_binding,
    refresh_remote_binding_auth,
    save_remote_binding,
    validate_binding_endpoint,
)
from .remote_credentials import resolve_credential
from .remote_format import FORMAT_RESOURCE_ID, require_encrypted_snapshot
from .remote_store import (
    RecoveryRequired,
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
    rebind: bool = False,
    recovery_code: str | None = None,
    recovery_code_provider: Callable[[str], str] | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteBinding:
    home = Path.home() if home is None else home
    if load_remote_binding(workspace) is not None and not rebind:
        raise RemoteBindingExists("Remote binding already exists")

    remote = _open_remote(endpoint, auth, environ, home=home)
    snapshot = remote.read()
    require_encrypted_snapshot(snapshot, allow_empty=True)

    status = "bound"
    recovered_vault = None
    store = DeviceKeyStore(home=home, workspace=workspace)

    if FORMAT_RESOURCE_ID not in snapshot.resources:
        status = "empty"
    else:
        # Delayed import to avoid circular dependency with encrypted_discovery
        from .encrypted_discovery import _fetch

        reader = BoundRemoteReader(remote, snapshot.sync_id)
        header_data = _fetch(reader, snapshot, (FORMAT_RESOURCE_ID,))[
            FORMAT_RESOURCE_ID
        ].data
        vault_id = metadata_vault_id(header_data)
        existing_key = store.load(vault_id)
        if existing_key is not None:
            status = "ready"
        else:
            code = recovery_code
            if code is None and recovery_code_provider is not None:
                code = recovery_code_provider(vault_id)
            if code is None:
                raise RecoveryRequired(
                    f"Remote store {snapshot.sync_id} is encrypted and requires a recovery code to authorize this device."
                )
            recovered_vault = recover_vault(
                header_data, snapshot.sync_id, decode_recovery_code(code)
            )
            status = "recovered"

    with WorkspaceWriterLock(home):
        current = load_remote_binding(workspace)
        if current is not None and not rebind:
            raise RemoteBindingExists("Remote binding already exists")
        if (
            current is not None
            and current.endpoint == endpoint
            and current.sync_id != snapshot.sync_id
        ):
            raise StoreIdentityMismatch(
                "Synchronization identity changed at the pinned endpoint"
            )
        if recovered_vault is not None:
            store.save(recovered_vault.vault_id, recovered_vault.vrk)
        binding = RemoteBinding(1, endpoint, snapshot.sync_id, auth, status=status)
        if current is not None and (current.endpoint, current.sync_id) == (
            endpoint,
            snapshot.sync_id,
        ):
            # Reauthentication must not discard a commit whose result is unknown.
            refresh_remote_binding_auth(workspace, binding, home=home)
            return binding
        if current is not None:
            remove_remote_binding(workspace, force=True, home=home)
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
