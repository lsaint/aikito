"""Fenced, restartable representation changes within an existing Store.

The remote lease and complete current graph determine progress; a new owner can
reconstruct it without another device's journal. Parents are copied before their
children; the old graph is deleted in reverse order. Local journals are encrypted
because explicit rollback requests necessarily contain plaintext wire payloads.
Business files and Replica Base are never reset or rewritten by migration.
"""

from __future__ import annotations

import re
import secrets
import subprocess
import uuid
from dataclasses import dataclass
from contextlib import nullcontext

from .. import __version__
from ..skill_state import WorkspaceWriterLock
from . import remote_limits
from .device_keys import DeviceKeyStore
from .encrypted_discovery import DISCOVERY_STATE
from .encrypted_remote import (
    MAX_METADATA_BYTES,
    EncryptedRemoteError,
    EncryptedVault,
    _crypto,
    _hkdf,
    _require,
    _b64,
    _unb64,
    _metadata_mac,
    _parse,
    create_recovery_metadata,
    encode_recovery_code,
    metadata_resource,
    metadata_vault_id,
    verify_metadata,
    collection_for_id,
)
from .payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
    validate_payload,
    credential_payload,
)
from .payload_io import capture_resources
from .reconcile import _center_resources
from .reconcile_budget import atomic_groups, check_manifest, fetch_response_size
from .remote_binding import load_remote_binding
from .remote_factory import _open_remote, BoundRemoteReader
from .remote_format import FORMAT_RESOURCE_ID, require_plaintext_snapshot
from .remote_protocol import (
    encode_fetch_request,
    commit_envelope_size,
    mutation_size,
    encode_commit_request as wire_request,
)
from .remote_store import RemoteSnapshot, ReceiptCursor, SnapshotExpired
from .remote_wire import (
    _canonical,
    _descriptor,
    _decode_descriptor,
    build_commit_request,
    encode_commit_request,
    decode_commit_request,
    validate_commit_result,
)
from .replica_state import load_replica_state, encode_cursor, decode_cursor
from .resource_state import (
    state_path,
    decode_resources,
    encode_resources,
    LOCAL_CONFIG,
    SYNC_KINDS,
    PENDING_COMMIT_STATE,
)
from .resource_write import ResourceContent, missing_references
from .resources import snapshot_workspace
from .transactions import atomic_text, atomic_unlink, entry_type

MIGRATION_STATE = ".local/state/aikito/encrypted-migration.json"
MAX_MIGRATION_ROUNDS = 1024
MAX_MIGRATION_RESOURCES = 32
# Set only after a guard release and its compatibility CI are verified.
MINIMUM_GUARD_RELEASE: str | None = None
_OPAQUE = re.compile(r"e1:[0-9a-f]{32}:[0-9a-f]{64}\Z")


class MigrationReleaseRequired(EncryptedRemoteError):
    code = "encrypted_migration_release_required"
    message = "Migration requires a published format-guard release and compatibility verification."


class MigrationPreflightFailed(EncryptedRemoteError):
    code = "encrypted_migration_preflight_failed"
    message = "Migration requires a synchronized workspace, no pending work, and a clean committed Git rollback baseline."


class MigrationFenced(EncryptedRemoteError):
    code = "encrypted_migration_fenced"
    message = "Migration ownership changed. Use explicit takeover after verifying the current Vault."


class MigrationPendingUnresolved(EncryptedRemoteError):
    code = "encrypted_migration_pending"
    message = (
        "Resolve the pending migration with apply before previewing another round."
    )


class MigrationBudgetExceeded(EncryptedRemoteError):
    code = "encrypted_migration_budget_exceeded"
    message = (
        "A migration atomic group or temporary manifest cannot fit the wire budget."
    )


@dataclass(frozen=True)
class MigrationResult:
    status: str
    rounds: int
    resources: int
    epoch: int | None


def require_migration_release():
    if MINIMUM_GUARD_RELEASE is None:
        raise MigrationReleaseRequired()

    def version(value):
        _require(type(value) is str and re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value))
        return tuple(map(int, value.split(".")))

    if version(__version__) < version(MINIMUM_GUARD_RELEASE):
        raise MigrationReleaseRequired()


def rewrite_metadata(encoded, key, *, migration=None):
    metadata = verify_metadata(encoded, key)
    metadata["format"] = "migrating" if migration is not None else "encrypted-v1"
    metadata["migration"] = migration
    metadata["metadata_mac"] = _metadata_mac(metadata, key)
    result = _canonical(metadata)
    verify_metadata(result, key)
    return result


def encrypted_size(resource, size):
    # All Base64 lengths are exact; no payload bytes are allocated for sizing.
    inner = {
        "fingerprint": resource.fingerprint,
        "key_version": 1,
        "logical_id": resource.id,
        "logical_parts": [{"path": p.path, "table": p.table} for p in resource.parts],
        "mode_fingerprint": resource.mode_fingerprint,
        "payload": "",
        "references": list(resource.references),
        "resource_type": resource.kind,
        "schema_version": 1,
    }
    ciphertext_size = len(_canonical(inner)) + 4 * ((size + 2) // 3) + 16
    envelope = {
        "algorithm": "hkdf-sha256+a256gcm",
        "ciphertext": "",
        "key_version": 1,
        "nonce": "A" * 16,
        "salt": "A" * 44,
        "version": 1,
    }
    outer_size = len(_canonical(envelope)) + 4 * ((ciphertext_size + 2) // 3)
    return len(encode_payload(FilePayload(b""))) + 4 * ((outer_size + 2) // 3)


def _fetch(reader, snapshot, identities):
    if (
        len(encode_fetch_request(snapshot, identities))
        > remote_limits.MAX_REMOTE_REQUEST_BYTES
        or fetch_response_size(snapshot, identities)
        > remote_limits.MAX_REMOTE_RESPONSE_BYTES
    ):
        raise MigrationBudgetExceeded()
    payloads = reader.fetch(snapshot, identities)
    _require(set(payloads) == set(identities))
    for identity, payload in payloads.items():
        descriptor = snapshot.resources[identity]
        _require(
            payload_hash(payload) == descriptor.content_hash
            and len(encode_payload(payload)) == descriptor.size
        )
    return payloads


def _git_baseline(workspace):
    def git(*args):
        result = subprocess.run(
            ["git", "--no-optional-locks", "-C", str(workspace), *args],
            capture_output=True,
            check=False,
        )
        if result.returncode:
            raise MigrationPreflightFailed()
        return result.stdout

    head = git("rev-parse", "--verify", "HEAD").strip().decode("ascii")
    if git("status", "--porcelain", "--untracked-files=all"):
        raise MigrationPreflightFailed()
    return head


def _plain_descriptor(resource, payload):
    return ResourceDescriptor(
        resource.fingerprint,
        payload_hash(payload),
        len(encode_payload(payload)),
        resource.references,
        resource.mode_fingerprint,
    )


class Migration:
    def __init__(
        self,
        remote,
        sync_id,
        workspace,
        home,
        *,
        rollback=False,
        takeover=False,
        dry_run=False,
        deliver_recovery=None,
    ):
        self.remote, self.reader = remote, BoundRemoteReader(remote, sync_id)
        self.sync_id, self.workspace, self.home = sync_id, workspace, home
        self.keys = DeviceKeyStore(home=home, workspace=workspace)
        self.rollback, self.takeover, self.dry_run = rollback, takeover, dry_run
        self.deliver = deliver_recovery
        self.path = state_path(workspace, MIGRATION_STATE)
        self.vault, self.header, self.header_descriptor = None, None, None
        self.state = None
        if entry_type(self.path) == "file":
            self._load()

    def _seal(self, body):
        aes, _, _, _ = _crypto()
        salt, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
        key = _hkdf(
            self.vault.vrk,
            salt,
            {
                "domain": "aikito.migration-journal-key",
                "vault_id": self.vault.vault_id,
                "version": 1,
            },
        )
        aad = _canonical(
            {
                "domain": "aikito.migration-journal",
                "sync_id": self.sync_id,
                "vault_id": self.vault.vault_id,
                "version": 1,
            }
        )
        ciphertext = aes(key).encrypt(nonce, _canonical(body), aad)
        return _canonical(
            {
                "version": 1,
                "vault_id": self.vault.vault_id,
                "salt": _b64(salt),
                "nonce": _b64(nonce),
                "ciphertext": _b64(ciphertext),
            }
        )

    def _load(self):
        with self.path.open("rb") as stream:
            encoded = stream.read(8 * remote_limits.MAX_REMOTE_REQUEST_BYTES + 1)
        outer = _parse(encoded, ("version", "vault_id", "salt", "nonce", "ciphertext"))
        _require(type(outer["version"]) is int and outer["version"] == 1)
        key = self.keys.load(outer["vault_id"])
        _require(key is not None)
        self.vault = EncryptedVault(outer["vault_id"], key)
        aes, _, _, invalid = _crypto()
        dek = _hkdf(
            key,
            _unb64(outer["salt"], 32),
            {
                "domain": "aikito.migration-journal-key",
                "vault_id": self.vault.vault_id,
                "version": 1,
            },
        )
        aad = _canonical(
            {
                "domain": "aikito.migration-journal",
                "sync_id": self.sync_id,
                "vault_id": self.vault.vault_id,
                "version": 1,
            }
        )
        try:
            plain = aes(dek).decrypt(
                _unb64(outer["nonce"], 12), _unb64(outer["ciphertext"]), aad
            )
        except invalid:
            raise EncryptedRemoteError() from None
        self.state = _parse(
            plain,
            (
                "version",
                "client_id",
                "cursor",
                "lease",
                "direction",
                "git_head",
                "entries",
                "pending",
            ),
        )
        _require(type(self.state["version"]) is int and self.state["version"] == 1)
        _require(
            type(self.state["client_id"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", self.state["client_id"])
        )
        _require(
            self.state["direction"] in ("encrypt", "rollback")
            and type(self.state["entries"]) is dict
        )
        decode_cursor(self.state["cursor"])

    def _save(self):
        if not self.dry_run:
            self.path = state_path(self.workspace, MIGRATION_STATE, create=True)
            atomic_text(self.path, self._seal(self.state).decode())

    def _new_state(self, lease=None):
        self.state = {
            "version": 1,
            "client_id": uuid.uuid4().hex,
            "cursor": None,
            "lease": lease,
            "direction": "rollback" if self.rollback else "encrypt",
            "git_head": _git_baseline(self.workspace),
            "entries": {},
            "pending": None,
        }

    def _read_header(self, snapshot):
        _require(snapshot.sync_id == self.sync_id)
        if any(
            k.startswith("format:") and k != FORMAT_RESOURCE_ID
            for k in snapshot.resources
        ):
            raise EncryptedRemoteError()
        descriptor = snapshot.resources.get(FORMAT_RESOURCE_ID)
        if descriptor is None:
            self.header, self.header_descriptor = None, None
            return None
        if descriptor != self.header_descriptor:
            _require(
                descriptor.size
                <= len(encode_payload(FilePayload(bytes(MAX_METADATA_BYTES))))
            )
            payload = _fetch(self.reader, snapshot, (FORMAT_RESOURCE_ID,))[
                FORMAT_RESOURCE_ID
            ]
            _require(
                type(payload) is FilePayload
                and metadata_resource(payload.data)[0] == descriptor
            )
            key = self.keys.load(metadata_vault_id(payload.data))
            _require(key is not None)
            vault = EncryptedVault(metadata_vault_id(payload.data), key)
            if self.vault is not None:
                _require(vault.vault_id == self.vault.vault_id)
            self.vault = vault
            verify_metadata(payload.data, key)
            self.header, self.header_descriptor = payload.data, descriptor
        return verify_metadata(self.header, self.vault.vrk)

    def _owned(self, metadata):
        if (
            metadata is None
            or metadata["migration"] != self.state["lease"]
            or metadata["format"] != "migrating"
        ):
            raise MigrationFenced()

    def _entry(self, identity, descriptor, resource, plain):
        return {
            "descriptor": _descriptor(descriptor),
            "resource": encode_resources({resource.id: resource}),
            "plaintext": _descriptor(plain),
        }

    def _entries(self, snapshot):
        result = {}
        for identity, raw in self.state["entries"].items():
            _require(
                type(raw) is dict
                and set(raw) == {"descriptor", "resource", "plaintext"}
            )
            descriptor = _decode_descriptor(raw["descriptor"])
            if snapshot.resources.get(identity) != descriptor:
                continue
            resources = decode_resources(raw["resource"])
            _require(len(resources) == 1)
            resource = next(iter(resources.values()))
            plain = _decode_descriptor(raw["plaintext"])
            _require(
                plain is not None
                and (plain.fingerprint, plain.references, plain.mode_fingerprint)
                == (
                    resource.fingerprint,
                    resource.references,
                    resource.mode_fingerprint,
                )
            )
            _require(
                identity == resource.id or identity == self.vault.opaque_id(resource.id)
            )
            result[identity] = (resource, plain)
        return result

    def _inspect(self, snapshot):
        """Validate at most one bounded discovery batch without importing files."""
        business = {
            k: d for k, d in snapshot.resources.items() if k != FORMAT_RESOURCE_ID
        }
        _require(
            all(
                d.size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES
                and set(d.references) <= business.keys()
                for d in business.values()
            )
        )
        plain = {k: d for k, d in business.items() if not _OPAQUE.fullmatch(k)}
        logical_plain = _center_resources(
            RemoteSnapshot(snapshot.sync_id, snapshot.revision, plain)
        )
        entries = self._entries(snapshot)
        tags = {self.vault.collection("private").tag: "private"}
        for resource in list(logical_plain.values()) + [
            resource for resource, _ in entries.values()
        ]:
            if resource.kind == "project":
                collection = f"project/{resource.name}/memory"
                tags[self.vault.collection(collection).tag] = collection
        remaining = set(business) - entries.keys()
        private = sorted(
            k
            for k in remaining
            if _OPAQUE.fullmatch(k)
            and k.split(":")[1] == self.vault.collection("private").tag
        )
        candidates = sorted(k for k in remaining if k in plain) + private
        if not candidates:
            candidates = sorted(remaining)
            _require(all(k.split(":")[1] in tags for k in candidates))
        selected = []
        for identity in candidates:
            proposed = selected + [identity]
            if (
                len(encode_fetch_request(snapshot, proposed))
                + len(encode_fetch_request(snapshot, (FORMAT_RESOURCE_ID,)))
                <= remote_limits.MAX_REMOTE_REQUEST_BYTES
                and fetch_response_size(snapshot, proposed)
                + fetch_response_size(snapshot, (FORMAT_RESOURCE_ID,))
                <= remote_limits.MAX_REMOTE_RESPONSE_BYTES
            ):
                selected.append(identity)
                if len(selected) == MAX_MIGRATION_RESOURCES:
                    break
        if candidates and not selected:
            raise MigrationBudgetExceeded()
        payloads = _fetch(self.reader, snapshot, tuple(selected)) if selected else {}
        for identity in selected:
            payload = payloads[identity]
            if identity in plain:
                resource = logical_plain[identity]
                validate_payload(resource, payload)
                _require(not credential_payload(payload))
                original = business[identity]
            else:
                resource, payload = self.vault.decrypt(
                    identity,
                    business[identity],
                    payload,
                    snapshot.sync_id,
                    tags[identity.split(":")[1]],
                )
                original = _plain_descriptor(resource, payload)
            entries[identity] = (resource, original)
            self.state["entries"][identity] = self._entry(
                identity, business[identity], resource, original
            )
        self.state["entries"] = {
            k: v
            for k, v in self.state["entries"].items()
            if k in business and _decode_descriptor(v["descriptor"]) == business[k]
        }
        self._save()
        if len(entries) != len(business):
            return None
        logical, descriptions = {}, {}
        for resource, descriptor in entries.values():
            if resource.id in logical:
                _require(
                    resource == logical[resource.id]
                    and descriptor == descriptions[resource.id]
                )
            logical[resource.id], descriptions[resource.id] = resource, descriptor
        _require(not missing_references(logical))
        return logical, descriptions

    def _preflight_plain(self, snapshot, header):
        require_plaintext_snapshot(snapshot)
        _require(
            entry_type(state_path(self.workspace, PENDING_COMMIT_STATE)) == "missing"
        )
        state, _ = load_replica_state(self.workspace)
        local = snapshot_workspace(self.workspace)
        resources = _center_resources(snapshot)
        relevant = {
            k: r
            for k, r in local.resources.items()
            if r.kind in SYNC_KINDS
            and not (r.kind == "config" and r.name in LOCAL_CONFIG)
        }
        if (
            local.findings
            or state is None
            or state.revision != snapshot.revision
            or dict(state.base) != resources
            or relevant != resources
        ):
            raise MigrationPreflightFailed()
        _require(not missing_references(resources))
        content = ResourceContent.from_workspace(local)
        for identity, resource in resources.items():
            payload = capture_resources(content, [identity])[identity]
            if _plain_descriptor(resource, payload) != snapshot.resources[identity]:
                raise MigrationPreflightFailed()
            self.state["entries"][identity] = self._entry(
                identity,
                snapshot.resources[identity],
                resource,
                snapshot.resources[identity],
            )
        self._budget_preflight(snapshot, resources, snapshot.resources, header)
        return resources, dict(snapshot.resources)

    def _budget_preflight(self, snapshot, resources, descriptions, header):
        metadata = verify_metadata(header, self.vault.vrk)
        lease = {
            "epoch": metadata["migration"]["epoch"] if metadata["migration"] else 1,
            "owner_client_id": self.state["client_id"],
            "token": "0" * 32,
        }
        descriptor, _ = metadata_resource(
            rewrite_metadata(header, self.vault.vrk, migration=lease)
        )
        peak = dict(snapshot.resources)
        peak.update(descriptions)
        peak[FORMAT_RESOURCE_ID] = descriptor
        cipher = {}
        for identity, resource in resources.items():
            keys = self.vault.collection(collection_for_id(identity))
            fingerprint, mode = keys.fingerprints(
                resource.fingerprint, resource.mode_fingerprint
            )
            size = encrypted_size(resource, descriptions[identity].size)
            if size > remote_limits.MAX_RESOURCE_PAYLOAD_BYTES:
                raise MigrationBudgetExceeded()
            after = ResourceDescriptor(
                fingerprint,
                "0" * 64,
                size,
                tuple(
                    sorted({self.vault.opaque_id(ref) for ref in resource.references})
                ),
                mode,
            )
            cipher[identity] = after
            peak[self.vault.opaque_id(identity)] = after
        worst = RemoteSnapshot(snapshot.sync_id, snapshot.revision + 1, peak)
        check_manifest(worst)
        dependencies = {k: set(r.references) for k, r in resources.items()}
        groups = atomic_groups(dependencies)
        cursor = ReceiptCursor("0" * 32, "0" * 64)
        start_cost = commit_envelope_size(snapshot, cursor) + mutation_size(
            FORMAT_RESOURCE_ID, snapshot.resources.get(FORMAT_RESOURCE_ID), descriptor
        )
        if start_cost > remote_limits.MAX_REMOTE_REQUEST_BYTES:
            raise MigrationBudgetExceeded()
        for group in groups:
            if len(group) > MAX_MIGRATION_RESOURCES:
                raise MigrationBudgetExceeded()
            forward = sum(
                mutation_size(self.vault.opaque_id(k), None, cipher[k]) for k in group
            )
            reverse = sum(mutation_size(k, None, descriptions[k]) for k in group)
            cost = (
                commit_envelope_size(worst, cursor)
                + max(forward, reverse)
                + max(0, len(group) - 1)
            )
            # Include both header and source fetches in the same request budget.
            opaque_group = tuple(self.vault.opaque_id(k) for k in group)
            cost += max(
                len(encode_fetch_request(worst, group)),
                len(encode_fetch_request(worst, opaque_group)),
            ) + len(encode_fetch_request(worst, (FORMAT_RESOURCE_ID,)))
            response = max(
                fetch_response_size(worst, group),
                fetch_response_size(worst, opaque_group),
            ) + fetch_response_size(worst, (FORMAT_RESOURCE_ID,))
            if (
                cost > remote_limits.MAX_REMOTE_REQUEST_BYTES
                or response > remote_limits.MAX_REMOTE_RESPONSE_BYTES
            ):
                raise MigrationBudgetExceeded()

    def _persist_request(self, snapshot, mutations, stage):
        request = build_commit_request(
            self.state["client_id"],
            uuid.uuid4().hex,
            snapshot,
            tuple(mutations),
            previous_receipt=decode_cursor(self.state["cursor"]),
        )
        if len(wire_request(request)) > remote_limits.MAX_REMOTE_REQUEST_BYTES:
            raise MigrationBudgetExceeded()
        self.state["pending"] = {
            "stage": stage,
            "request": _b64(encode_commit_request(request)),
        }
        self._save()
        result = self.remote.commit(request)
        self._ack(request, result)

    def _ack(self, request, result):
        validate_commit_result(request, result)
        pending = self.state["pending"]
        if pending is not None and pending["stage"] == "copy":
            known = {}
            for entry in self.state["entries"].values():
                known.update(decode_resources(entry["resource"]))
            opaque = {self.vault.opaque_id(k): k for k in known}
            for mutation in request.mutations:
                if _OPAQUE.fullmatch(mutation.id):
                    identity = opaque[mutation.id]
                    resource, payload = self.vault.decrypt(
                        mutation.id,
                        mutation.after,
                        mutation.payload,
                        self.sync_id,
                        collection_for_id(identity),
                    )
                else:
                    resource, payload = known[mutation.id], mutation.payload
                    validate_payload(resource, payload)
                self.state["entries"][mutation.id] = self._entry(
                    mutation.id,
                    mutation.after,
                    resource,
                    _plain_descriptor(resource, payload),
                )
        self.state["cursor"] = encode_cursor(
            ReceiptCursor(request.request_id, request.mutation_digest)
        )
        self.state["pending"] = None
        self._save()

    def _recover(self, snapshot, metadata):
        pending = self.state["pending"]
        if pending is None:
            return
        _require(type(pending) is dict and set(pending) == {"stage", "request"})
        request = decode_commit_request(_unb64(pending["request"]))
        _require(
            request.client_id == self.state["client_id"]
            and request.expected.sync_id == self.sync_id
        )
        result = self.remote.resolve_commit(
            self.sync_id, request.client_id, request.request_id, request.mutation_digest
        )
        if result is None:
            metadata = self._read_header(snapshot)
            if pending["stage"] == "start":
                if metadata is not None:
                    raise MigrationFenced()
            elif pending["stage"] == "claim":
                before = request.expected.resources[FORMAT_RESOURCE_ID]
                if snapshot.resources.get(FORMAT_RESOURCE_ID) != before:
                    raise MigrationFenced()
            else:
                self._owned(metadata)
            try:
                result = self.remote.commit(request)
            except SnapshotExpired:
                self.state["pending"] = None
                self._save()
                raise
        self._ack(request, result)
        return pending["stage"]

    def _batch(self, snapshot, logical, descriptions):
        business = {
            k: v for k, v in snapshot.resources.items() if k != FORMAT_RESOURCE_ID
        }
        destination = (lambda k: k) if self.rollback else self.vault.opaque_id
        source = self.vault.opaque_id if self.rollback else (lambda k: k)
        missing = {k for k in logical if destination(k) not in business}
        dependencies = {
            k: {ref for ref in logical[k].references if ref in missing} for k in missing
        }
        if missing:
            candidates = atomic_groups(dependencies)
            stage = "copy"
        else:
            deleting = {k for k in logical if source(k) in business}
            dependencies = {
                k: {other for other in deleting if k in logical[other].references}
                for k in deleting
            }
            candidates = atomic_groups(dependencies)
            stage = "delete"
        chosen = None
        for group in candidates:
            if stage == "copy" and any(
                destination(ref) not in business and ref not in group
                for k in group
                for ref in logical[k].references
            ):
                continue
            if stage == "delete" and any(
                source(other) in business and other not in group
                for k in group
                for other in logical
                if k in logical[other].references
            ):
                continue
            chosen = group
            break
        if chosen is None:
            return False
        if len(chosen) > MAX_MIGRATION_RESOURCES:
            raise MigrationBudgetExceeded()
        mutations = []
        if stage == "copy":
            ids = tuple(source(k) for k in chosen)
            if (
                len(encode_fetch_request(snapshot, ids))
                + len(encode_fetch_request(snapshot, (FORMAT_RESOURCE_ID,)))
                > remote_limits.MAX_REMOTE_REQUEST_BYTES
                or fetch_response_size(snapshot, ids)
                + fetch_response_size(snapshot, (FORMAT_RESOURCE_ID,))
                > remote_limits.MAX_REMOTE_RESPONSE_BYTES
            ):
                raise MigrationBudgetExceeded()
            payloads = _fetch(self.reader, snapshot, ids)
            for identity in chosen:
                resource, payload = logical[identity], payloads[source(identity)]
                if self.rollback:
                    decoded, payload = self.vault.decrypt(
                        source(identity),
                        business[source(identity)],
                        payload,
                        self.sync_id,
                        collection_for_id(identity),
                    )
                    _require(decoded == resource)
                    after = _plain_descriptor(resource, payload)
                else:
                    validate_payload(resource, payload)
                    _require(not credential_payload(payload))
                    _, after, payload = self.vault.encrypt(
                        resource, payload, self.sync_id
                    )
                mutations.append(
                    ResourceMutation(destination(identity), None, after, payload)
                )
        else:
            mutations = [
                ResourceMutation(source(k), business[source(k)], None, None)
                for k in chosen
            ]
        prospective = dict(snapshot.resources)
        for mutation in mutations:
            if mutation.after is None:
                prospective.pop(mutation.id)
            else:
                prospective[mutation.id] = mutation.after
        _require(
            all(set(d.references) <= prospective.keys() for d in prospective.values())
        )
        check_manifest(RemoteSnapshot(self.sync_id, snapshot.revision + 1, prospective))
        self._persist_request(snapshot, mutations, stage)
        return True

    def run(self, maximum=MAX_MIGRATION_ROUNDS):
        _require(type(maximum) is int and maximum > 0)
        snapshot = self.reader.read()
        check_manifest(snapshot)
        recovered = None
        if self.state is not None and self.state["pending"] is not None:
            if self.dry_run:
                raise MigrationPendingUnresolved()
            try:
                recovered = self._recover(snapshot, None)
            except MigrationFenced:
                if not self.takeover:
                    raise
            if recovered == "finish":
                direction = self.state["direction"]
                atomic_unlink(self.path)
                if direction == "rollback":
                    cache = state_path(self.workspace, DISCOVERY_STATE)
                    if entry_type(cache) == "file":
                        atomic_unlink(cache)
                return MigrationResult(
                    "rolled-back" if direction == "rollback" else "encrypted",
                    0,
                    len(self.state["entries"]),
                    self.state["lease"]["epoch"],
                )
            snapshot = self.reader.read()
        metadata = self._read_header(snapshot)
        if self.state is not None:
            if (
                self.takeover
                and metadata is not None
                and metadata["format"] == "migrating"
            ):
                # Preserve fenced uncertainty for audit; the new owner reconstructs
                # current truth rather than replaying another ownership epoch.
                if not self.dry_run:
                    atomic_text(
                        self.path.with_name(
                            self.path.name + ".fenced-" + uuid.uuid4().hex
                        ),
                        self.path.read_text(),
                    )
                self._new_state()
            else:
                self.rollback = self.rollback or self.state["direction"] == "rollback"
                self.state["direction"] = "rollback" if self.rollback else "encrypt"
                snapshot = self.reader.read()
                metadata = self._read_header(snapshot)
        if metadata is None:
            if self.takeover:
                raise MigrationPreflightFailed()
            if self.state is not None:
                raise MigrationFenced()
            if self.rollback:
                raise MigrationPreflightFailed()
            self.vault = EncryptedVault(secrets.token_hex(16), secrets.token_bytes(32))
            self._new_state()
            lease = {
                "epoch": 1,
                "owner_client_id": self.state["client_id"],
                "token": secrets.token_hex(16),
            }
            code = secrets.token_bytes(32)
            header = create_recovery_metadata(
                self.vault.vrk,
                self.vault.vault_id,
                self.sync_id,
                code,
                format="migrating",
                migration=lease,
            )
            logical, descriptions = self._preflight_plain(snapshot, header)
            if self.dry_run:
                return MigrationResult("preflight", 0, len(logical), 1)
            _require(self.deliver is not None)
            self.keys.save(self.vault.vault_id, self.vault.vrk)
            self.deliver(encode_recovery_code(code))
            self.state["lease"] = lease
            descriptor, payload = metadata_resource(header)
            self._persist_request(
                snapshot,
                (ResourceMutation(FORMAT_RESOURCE_ID, None, descriptor, payload),),
                "start",
            )
        elif self.state is None:
            if (
                metadata["format"] == "migrating"
                and not self.takeover
                and not self.dry_run
            ):
                raise MigrationFenced()
            if (
                metadata["format"] == "encrypted-v1"
                and not self.rollback
                and not self.dry_run
            ):
                _require(
                    all(
                        k == FORMAT_RESOURCE_ID or _OPAQUE.fullmatch(k)
                        for k in snapshot.resources
                    )
                )
                return MigrationResult(
                    "encrypted", 0, len(snapshot.resources) - 1, None
                )
            self._new_state()
        elif not self.takeover and metadata["format"] == "migrating":
            self._owned(metadata)
        for round_index in range(maximum):
            snapshot = self.reader.read()
            check_manifest(snapshot)
            metadata = self._read_header(snapshot)
            _require(metadata is not None)
            inspected = self._inspect(snapshot)
            if inspected is None:
                continue
            logical, descriptions = inspected
            if self.dry_run:
                self._budget_preflight(snapshot, logical, descriptions, self.header)
                return MigrationResult(
                    "preflight",
                    round_index + 1,
                    len(logical),
                    metadata["migration"]["epoch"] if metadata["migration"] else None,
                )
            if metadata["format"] == "encrypted-v1":
                _require(self.rollback)
            if (
                metadata["migration"] != self.state["lease"]
                or metadata["format"] == "encrypted-v1"
            ):
                if metadata["format"] == "migrating" and not self.takeover:
                    raise MigrationFenced()
                self._budget_preflight(snapshot, logical, descriptions, self.header)
                epoch = (
                    metadata["migration"]["epoch"] + 1 if metadata["migration"] else 1
                )
                lease = {
                    "epoch": epoch,
                    "owner_client_id": self.state["client_id"],
                    "token": secrets.token_hex(16),
                }
                updated = rewrite_metadata(self.header, self.vault.vrk, migration=lease)
                descriptor, payload = metadata_resource(updated)
                self.state["lease"] = lease
                self._persist_request(
                    snapshot,
                    (
                        ResourceMutation(
                            FORMAT_RESOURCE_ID,
                            snapshot.resources[FORMAT_RESOURCE_ID],
                            descriptor,
                            payload,
                        ),
                    ),
                    "claim",
                )
                self.takeover = False
                continue
            self._owned(metadata)
            if self._batch(snapshot, logical, descriptions):
                continue
            if self.rollback:
                mutation = ResourceMutation(
                    FORMAT_RESOURCE_ID,
                    snapshot.resources[FORMAT_RESOURCE_ID],
                    None,
                    None,
                )
            else:
                updated = rewrite_metadata(self.header, self.vault.vrk)
                descriptor, payload = metadata_resource(updated)
                mutation = ResourceMutation(
                    FORMAT_RESOURCE_ID,
                    snapshot.resources[FORMAT_RESOURCE_ID],
                    descriptor,
                    payload,
                )
            self._persist_request(snapshot, (mutation,), "finish")
            if self.rollback:
                cache = state_path(self.workspace, DISCOVERY_STATE)
                if entry_type(cache) == "file":
                    atomic_unlink(cache)
            atomic_unlink(self.path)
            return MigrationResult(
                "rolled-back" if self.rollback else "encrypted",
                round_index + 1,
                len(logical),
                self.state["lease"]["epoch"],
            )
        self._save()
        return MigrationResult(
            "paused",
            maximum,
            len(self.state["entries"]),
            self.state["lease"]["epoch"] if self.state["lease"] else None,
        )


def run_migration(
    workspace,
    home,
    *,
    rollback=False,
    takeover=False,
    dry_run=False,
    maximum=MAX_MIGRATION_ROUNDS,
    deliver_recovery=None,
):
    if not dry_run:
        require_migration_release()
    binding = load_remote_binding(workspace)
    _require(binding is not None)
    remote = _open_remote(binding.endpoint, binding.auth, None, home=home)
    with nullcontext() if dry_run else WorkspaceWriterLock(home):
        migration = Migration(
            remote,
            binding.sync_id,
            workspace,
            home,
            rollback=rollback,
            takeover=takeover,
            dry_run=dry_run,
            deliver_recovery=deliver_recovery,
        )
        return migration.run(maximum)
