"""Encrypted reconciliation hides wire identities from the logical planner.

Discovery never authorizes absence until its authenticated view is complete.
Logical payload descriptors live only in authenticated client state; requests
persist opaque descriptors and ciphertext before transmission. Exact wire
receipts confirm only decrypted upload resources, including after process loss.
"""

from __future__ import annotations

import base64
import secrets
import uuid
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import replace

from ..skill_state import WorkspaceWriterLock
from . import remote_limits
from .device_keys import DeviceKeyStore
from .encrypted_discovery import (
    DISCOVERY_STATE,
    DiscoveryBudgetExceeded,
    EncryptedStoreLocked,
    discover_encrypted,
    unlock_encrypted_snapshot,
)
from .encrypted_remote import (
    EncryptedRemoteError,
    EncryptedVault,
    _require,
    collection_for_id,
    create_recovery_metadata,
    encode_recovery_code,
    metadata_resource,
    verify_metadata,
)
from .payload import ResourceMutation, payload_hash
from .payload_io import capture_resources
from .reconcile_budget import fetch_response_size
from .recovery_codes import save_recovery_code
from .remote_binding import load_remote_binding
from .remote_factory import BoundRemoteReader, _open_remote
from .remote_format import (
    FORMAT_RESOURCE_ID,
    require_encrypted_snapshot,
    require_plaintext_snapshot,
)
from .remote_protocol import (
    encode_commit_request as encode_wire_request,
)
from .remote_protocol import (
    encode_fetch_request,
)
from .remote_store import RecoveryRequired, RemoteSnapshot, SnapshotExpired
from .remote_wire import (
    _canonical,
    build_commit_request,
    decode_commit_request,
    decode_state_json,
    encode_commit_request,
    validate_commit_result,
)
from .replica_state import load_replica_state
from .resource_state import PENDING_COMMIT_STATE, local_resource_for_id, state_path
from .transactions import atomic_text, atomic_unlink, entry_type

INITIALIZATION_STATE = ".local/state/aikito/encrypted-initialization.json"
MAX_ENCRYPTED_ROUNDS = 1024


class EncryptedDiscoveryPending(EncryptedRemoteError):
    code = "encrypted_discovery_pending"
    message = "Encrypted discovery must complete before planning writes or deletions."


class EncryptedRemote:
    def __init__(
        self,
        remote,
        sync_id,
        workspace,
        home,
        *,
        persist=True,
        on_progress=None,
    ):
        self.wire = remote
        self.sync_id = sync_id
        self.workspace, self.home = workspace, home
        self.keys = DeviceKeyStore(home=home, workspace=workspace)
        self.reader = BoundRemoteReader(remote, sync_id)
        self.persist = persist
        self.on_progress = on_progress
        path = state_path(workspace, DISCOVERY_STATE)
        self.checkpoint = None
        if entry_type(path) == "file":
            with path.open("rb") as stream:
                self.checkpoint = stream.read(
                    2 * remote_limits.MAX_REMOTE_RESPONSE_BYTES + 1
                )
        self.vault = None
        self.view = None
        self.logical = None
        self.prepared = {}

    def _report(self, stage):
        if self.on_progress:
            self.on_progress(stage)

    def validate_replica(self, local):
        self.wire.validate_replica(local)

    def prepare(self):
        verified = len(self.view.resources) if self.view else 0
        self._report(f"Reading and decrypting remote resources ({verified} verified)")
        view = discover_encrypted(
            self.reader,
            self.keys,
            checkpoint=self.checkpoint,
            _vault=self.vault,
            _header=self.view.snapshot.resources[FORMAT_RESOURCE_ID]
            if self.view
            else None,
        )
        self.view = view
        self.checkpoint = view.checkpoint
        if self.persist:
            atomic_text(
                state_path(self.workspace, DISCOVERY_STATE, create=True),
                view.checkpoint.decode(),
            )
        # Key identity is inside the authenticated checkpoint, not a URL-derived key.
        vault_id = decode_state_json(view.checkpoint)["vault_id"]
        key = self.keys.load(vault_id)
        if key is None:
            raise EncryptedStoreLocked()
        self.vault = EncryptedVault(vault_id, key)
        if view.complete:
            self.logical = RemoteSnapshot(
                view.snapshot.sync_id, view.snapshot.revision, view.descriptors
            )
        return view.complete

    def read(self):
        for _ in range(MAX_ENCRYPTED_ROUNDS):
            if self.prepare() and not self.view.fetched:
                self._report("Comparing local resources")
                return self.logical
        raise DiscoveryBudgetExceeded()

    def fetch(self, expected, ids):
        self._report(f"Downloading and checking {len(ids)} resources")
        if expected != self.logical:
            raise SnapshotExpired("Encrypted logical view changed")
        opaque = {self.vault.opaque_id(identity): identity for identity in ids}
        outer = self.reader.fetch(self.view.snapshot, tuple(opaque))
        _require(set(outer) == set(opaque))
        result = {}
        for remote_id, identity in opaque.items():
            resource = self.view.resources[identity]
            collection = collection_for_id(resource.id)
            actual, payload = self.vault.decrypt(
                remote_id,
                self.view.snapshot.resources[remote_id],
                outer[remote_id],
                expected.sync_id,
                collection,
            )
            _require(actual == resource)
            result[identity] = payload
        return result

    def _encrypted(self, resource, payload):
        signature = (resource, payload_hash(payload))
        old = self.prepared.get(resource.id)
        if old is None or old[0] != signature:
            encrypted = self.vault.encrypt(resource, payload, self.sync_id)
            self.prepared[resource.id] = (signature, encrypted)
        return self.prepared[resource.id][1]

    def prepare_wire_budget(self, proposed, local_content, resources, descriptors):
        encrypted = {}
        for identity, descriptor in descriptors.items():
            if descriptor is None:
                encrypted[identity] = None
            else:
                payload = capture_resources(local_content, [identity])[identity]
                encrypted[identity] = self._encrypted(resources[identity], payload)[1]
        snapshot = self.view.snapshot
        identities = {
            identity: self.vault.opaque_id(identity)
            for identity in self.logical.resources.keys() | descriptors.keys()
        }
        return (
            snapshot,
            encrypted,
            identities,
            len(encode_fetch_request(snapshot, (FORMAT_RESOURCE_ID,))),
            fetch_response_size(snapshot, (FORMAT_RESOURCE_ID,)),
        )

    def encode_request(self, request):
        _require(request.expected == self.logical)
        mutations = []
        for mutation in request.mutations:
            identity = self.vault.opaque_id(mutation.id)
            if mutation.after is None:
                after, payload = None, None
            else:
                resource = replace(
                    local_resource_for_id(mutation.id, mutation.after.fingerprint),
                    references=mutation.after.references,
                    mode_fingerprint=mutation.after.mode_fingerprint,
                )
                actual_id, after, payload = self._encrypted(resource, mutation.payload)
                _require(actual_id == identity)
            mutations.append(
                ResourceMutation(
                    identity, self.view.snapshot.resources.get(identity), after, payload
                )
            )
        return build_commit_request(
            request.client_id,
            request.request_id,
            self.view.snapshot,
            tuple(mutations),
            previous_receipt=request.previous_receipt,
        )

    def _vault(self, vault_id):
        key = self.keys.load(vault_id)
        if key is None:
            raise EncryptedStoreLocked()
        return EncryptedVault(vault_id, key)

    def confirm_request(self, request, vault_id, safe):
        vault = self._vault(vault_id)
        identities = {vault.opaque_id(identity): identity for identity in safe}
        _require(len(identities) == len(safe))
        result = {}
        for mutation in request.mutations:
            _require(mutation.id in identities)
            identity = identities[mutation.id]
            if mutation.after is None:
                result[identity] = None
            else:
                resource, _ = vault.decrypt(
                    mutation.id,
                    mutation.after,
                    mutation.payload,
                    request.expected.sync_id,
                    collection_for_id(identity),
                )
                _require(resource.id == identity)
                result[identity] = resource
        return result

    def commit(self, request):
        self._report(f"Uploading {len(request.mutations)} resource changes")
        # This is already the persisted wire request: never encrypt at send time.
        _require(request.expected.sync_id == self.sync_id)
        current = self.reader.read()
        if (
            self.view is not None
            and current.resources.get(FORMAT_RESOURCE_ID)
            == self.view.snapshot.resources[FORMAT_RESOURCE_ID]
        ):
            vault = self.vault
        else:
            vault = unlock_encrypted_snapshot(self.reader, current, self.keys)
        _require(self.vault is not None and vault.vault_id == self.vault.vault_id)
        return self.wire.commit(request)

    def recover(self):
        try:
            require_encrypted_snapshot(self.reader.read())
        except RecoveryRequired:
            recovered = self.wire.recover()
            require_encrypted_snapshot(self.reader.read())
            return recovered
        return self.wire.recover()

    def recover_encrypted_pending(self, store, pending, state):
        request = pending.request
        try:
            vault = self._vault(pending.vault_id)
            identities = {
                vault.opaque_id(identity) for identity in pending.safe_resource_ids
            }
        except EncryptedRemoteError:
            raise EncryptedRemoteError() from None
        if not {mutation.id for mutation in request.mutations} <= identities:
            raise EncryptedRemoteError()
        _require(request.expected.sync_id == self.sync_id)
        self.reader.read()
        result = self.wire.resolve_commit(
            request.expected.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )
        if result is None:
            current = self.reader.read()
            vault = unlock_encrypted_snapshot(self.reader, current, self.keys)
            _require(vault.vault_id == pending.vault_id)
            try:
                result = self.wire.commit(request)
            except SnapshotExpired as exc:
                store.clear(pending, rejected=exc)
                return
        validate_commit_result(request, result)
        confirmations = self.confirm_request(
            request, pending.vault_id, pending.safe_resource_ids
        )
        base = dict(state.base)
        for identity, resource in confirmations.items():
            if resource is None:
                base.pop(identity, None)
            else:
                base[identity] = resource
        completed = replace(
            state,
            base=base,
            revision=max(state.revision, result.accepted_revision),
            unpaired_ids=frozenset(state.unpaired_ids - confirmations.keys()),
        )
        store.complete(pending, result, completed, confirmed_resources=confirmations)
        store.clear(pending)


def open_encrypted_remote(
    workspace, home, *, persist=True, environ=None, on_progress=None
):
    binding = load_remote_binding(workspace)
    if binding is None:
        raise EncryptedRemoteError()
    remote = _open_remote(binding.endpoint, binding.auth, environ, home=home)
    return EncryptedRemote(
        remote,
        binding.sync_id,
        workspace,
        home,
        persist=persist,
        on_progress=on_progress,
    )


def run_encrypted_reconciliation(
    workspace,
    home,
    *,
    dry_run,
    environ=None,
    on_progress: Callable[[str], None] | None = None,
):
    from .reconcile import (
        WorkspaceReconcileError,
        build_reconcile_plan,
        recover_reconciliation,
        run_reconciliation,
    )

    if on_progress:
        on_progress("Opening remote connection")
    remote = open_encrypted_remote(
        workspace, home, persist=not dry_run, environ=environ, on_progress=on_progress
    )
    with nullcontext() if dry_run else WorkspaceWriterLock(home):
        if not dry_run:
            remote._report("Checking interrupted synchronization")
            recover_reconciliation(workspace, remote, home)
        for _ in range(MAX_ENCRYPTED_ROUNDS):
            if not remote.prepare() or remote.view.fetched:
                continue
            try:
                compared = build_reconcile_plan(
                    workspace, remote, _inspect_payloads=False
                )
                resolutions = {
                    item.id: "local"
                    if item.id in compared.local_snapshot.resources
                    else "remote"
                    for item in compared.conflicts
                }
                return run_reconciliation(
                    workspace, remote, home, dry_run=dry_run, resolutions=resolutions
                )
            except WorkspaceReconcileError as exc:
                if not isinstance(exc.__cause__, EncryptedDiscoveryPending):
                    raise
        raise DiscoveryBudgetExceeded()


def initialize_encrypted_workspace(workspace, home, deliver_recovery):
    """Create an empty Store Vault by durable CAS; never migrate plaintext data.

    The recovery code is saved outside the workspace and delivered to the
    caller's secure channel before any commit can happen. Response-loss recovery reuses the persisted public header
    request; it never regenerates keys, codes, salts or identities.
    """
    with WorkspaceWriterLock(home):
        binding = load_remote_binding(workspace)
        _require(binding is not None)
        remote = _open_remote(binding.endpoint, binding.auth, None, home=home)
        reader = BoundRemoteReader(remote, binding.sync_id)
        snapshot = reader.read()
        _require(entry_type(state_path(workspace, PENDING_COMMIT_STATE)) == "missing")
        path = state_path(workspace, INITIALIZATION_STATE)
        keys = DeviceKeyStore(home=home, workspace=workspace)
        if entry_type(path) == "file":
            with path.open("rb") as stream:
                record = decode_state_json(
                    stream.read(2 * remote_limits.MAX_REMOTE_REQUEST_BYTES + 1)
                )
            _require(
                type(record) is dict
                and set(record) == {"version", "vault_id", "request"}
                and type(record["version"]) is int
                and record["version"] == 1
            )
            request = decode_commit_request(
                base64.b64decode(record["request"], validate=True)
            )
            vault_id = record["vault_id"]
            key = keys.load(vault_id)
            _require(key is not None and request.expected.sync_id == binding.sync_id)
            _require(
                not request.expected.resources
                and len(request.mutations) == 1
                and request.mutations[0].id == FORMAT_RESOURCE_ID
            )
            mutation = request.mutations[0]
            _require(
                mutation.before is None
                and mutation.after == metadata_resource(mutation.payload.data)[0]
            )
            metadata = verify_metadata(mutation.payload.data, key)
            _require(
                metadata["vault_id"] == vault_id
                and metadata["format"] == "encrypted-v1"
            )
        else:
            require_plaintext_snapshot(snapshot)
            _require(not snapshot.resources)
            state, _ = load_replica_state(workspace)
            _require(state is None or not state.base)
            vault_id, key, code = (
                secrets.token_hex(16),
                secrets.token_bytes(32),
                secrets.token_bytes(32),
            )
            encoded = create_recovery_metadata(key, vault_id, snapshot.sync_id, code)
            descriptor, payload = metadata_resource(encoded)
            request = build_commit_request(
                uuid.uuid4().hex,
                uuid.uuid4().hex,
                snapshot,
                (ResourceMutation(FORMAT_RESOURCE_ID, None, descriptor, payload),),
            )
            _require(
                len(encode_wire_request(request))
                <= remote_limits.MAX_REMOTE_REQUEST_BYTES
            )
            keys.save(vault_id, key)
            recovery_code = encode_recovery_code(code)
            save_recovery_code(
                snapshot.sync_id,
                recovery_code,
                home=home,
                workspace=workspace,
                endpoint=binding.endpoint,
            )
            deliver_recovery(recovery_code)
            record = {
                "version": 1,
                "vault_id": vault_id,
                "request": base64.b64encode(encode_commit_request(request)).decode(),
            }
            path = state_path(workspace, INITIALIZATION_STATE, create=True)
            atomic_text(path, _canonical(record).decode())
        result = remote.resolve_commit(
            request.expected.sync_id,
            request.client_id,
            request.request_id,
            request.mutation_digest,
        )
        if result is None:
            try:
                result = remote.commit(request)
            except SnapshotExpired:
                # A definite losing CAS cannot replace the winning Vault.
                atomic_unlink(path)
                raise
        validate_commit_result(request, result)
        atomic_unlink(path)
        return vault_id
