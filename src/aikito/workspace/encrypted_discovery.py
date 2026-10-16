"""Discover authenticated logical resources through a bounded read-only session.

Private resources bootstrap project Collection tags. An authenticated local map
retains verified descriptor identities across processes; it contains no payloads
or keys and never becomes Replica Base. A partial view cannot authorize writes,
absence or deletions. Every remote operation is pinned to one Store identity.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from collections.abc import Mapping

from ..skill_state import WorkspaceWriterLock
from . import remote_limits
from .device_keys import DeviceKeyStore
from .encrypted_remote import (
    MAX_METADATA_BYTES,
    EncryptedRemoteError,
    EncryptedVault,
    _hkdf,
    _parse,
    _require,
    metadata_resource,
    verify_metadata,
    metadata_vault_id,
)
from .payload import FilePayload, ResourceDescriptor, encode_payload, payload_hash
from .reconcile_budget import check_manifest, fetch_response_size
from .remote_factory import open_bound_reader
from .remote_format import FORMAT_RESOURCE_ID, EncryptedStoreUnsupported
from .remote_protocol import encode_fetch_request
from .remote_store import RemoteSnapshot
from .remote_wire import _canonical, _descriptor, _decode_descriptor
from .resource_state import decode_resources, encode_resources, state_path
from .resource_write import missing_references
from .resources import Resource
from .transactions import atomic_text, entry_type, WorkspaceCoreError

DISCOVERY_STATE = ".local/state/aikito/encrypted-discovery.json"
MAX_DISCOVERY_RESOURCES = 32
_OPAQUE = re.compile(r"e1:[0-9a-f]{32}:[0-9a-f]{64}\Z")


class EncryptedStoreLocked(EncryptedRemoteError):
    code = "encrypted_store_locked"
    message = "Recover the device key before inspecting this encrypted store."


class DiscoveryBudgetExceeded(EncryptedRemoteError):
    code = "encrypted_discovery_budget_exceeded"
    message = "Encrypted resource discovery cannot progress within the round budget."


@dataclass(frozen=True)
class DiscoveredSnapshot:
    snapshot: RemoteSnapshot
    resources: Mapping[str, Resource] = field(repr=False)
    remaining: int
    fetched: int
    checkpoint: bytes = field(repr=False)
    descriptors: Mapping[str, ResourceDescriptor] = field(
        default_factory=dict, repr=False
    )

    def __post_init__(self):
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))

    @property
    def complete(self):
        return self.remaining == 0


def _fetch(reader, snapshot, identities):
    if (
        len(encode_fetch_request(snapshot, identities))
        > remote_limits.MAX_REMOTE_REQUEST_BYTES
        or fetch_response_size(snapshot, identities)
        > remote_limits.MAX_REMOTE_RESPONSE_BYTES
    ):
        raise DiscoveryBudgetExceeded()
    payloads = reader.fetch(snapshot, identities)
    _require(set(payloads) == set(identities))
    for identity, payload in payloads.items():
        descriptor = snapshot.resources[identity]
        _require(type(payload) is FilePayload)
        _require(
            len(encode_payload(payload)) == descriptor.size
            and payload_hash(payload) == descriptor.content_hash
        )
    return payloads


def _checkpoint_key(vault):
    # This domain belongs to local state, not the remote encryption protocol.
    return _hkdf(
        vault.vrk,
        bytes.fromhex(vault.vault_id),
        {"domain": "aikito.discovery-state", "version": 1},
    )


def _load_checkpoint(encoded, vault, sync_id):
    if encoded is None:
        return {}
    _require(len(encoded) <= 2 * remote_limits.MAX_REMOTE_RESPONSE_BYTES)
    value = _parse(encoded, ("version", "sync_id", "vault_id", "entries", "mac"))
    _require(type(value["version"]) is int and value["version"] == 2)
    _require(value["sync_id"] == sync_id and value["vault_id"] == vault.vault_id)
    _require(type(value["mac"]) is str and type(value["entries"]) is dict)
    body = {k: v for k, v in value.items() if k != "mac"}
    mac = hmac.new(_checkpoint_key(vault), _canonical(body), hashlib.sha256).hexdigest()
    _require(hmac.compare_digest(value["mac"], mac))
    entries = {}
    for identity, entry in value["entries"].items():
        fields = {"descriptor", "resource", "plaintext"}
        _require(type(entry) is dict and set(entry) == fields)
        resources = decode_resources(entry["resource"])
        _require(len(resources) == 1)
        resource = next(iter(resources.values()))
        descriptor = _decode_descriptor(entry["descriptor"])
        _require(
            type(descriptor) is ResourceDescriptor
            and identity == vault.opaque_id(resource.id)
        )
        keys = vault.collection(
            "private"
            if resource.kind != "project-memory"
            else f"project/{resource.name.split('/')[0]}/memory"
        )
        _require(
            (descriptor.fingerprint, descriptor.mode_fingerprint)
            == keys.fingerprints(resource.fingerprint, resource.mode_fingerprint)
        )
        _require(
            descriptor.references
            == tuple(sorted({vault.opaque_id(ref) for ref in resource.references}))
        )
        plain = _decode_descriptor(entry["plaintext"])
        _require(type(plain) is ResourceDescriptor)
        _require(
            (plain.fingerprint, plain.references, plain.mode_fingerprint)
            == (resource.fingerprint, resource.references, resource.mode_fingerprint)
        )
        entries[identity] = (descriptor, resource, plain)
    return entries


def _encode_checkpoint(entries, vault, sync_id):
    body = {
        "version": 2,
        "sync_id": sync_id,
        "vault_id": vault.vault_id,
        "entries": {
            identity: {
                "descriptor": _descriptor(descriptor),
                "resource": encode_resources({resource.id: resource}),
                "plaintext": _descriptor(plain),
            }
            for identity, (descriptor, resource, plain) in entries.items()
        },
    }
    mac = hmac.new(_checkpoint_key(vault), _canonical(body), hashlib.sha256).hexdigest()
    return _canonical({**body, "mac": mac})


def discover_encrypted(
    reader,
    device_keys: DeviceKeyStore,
    *,
    checkpoint: bytes | None = None,
    maximum: int = MAX_DISCOVERY_RESOURCES,
    _vault: EncryptedVault | None = None,
    _header: ResourceDescriptor | None = None,
) -> DiscoveredSnapshot:
    """Read one round; verify every descriptor before it enters the logical map.

    The maximum counts business resources, in addition to one format-header
    fetch. Total fetch request/response bytes for both calls share the wire cap.
    Already authenticated unchanged descriptors need no payload download.
    """
    try:
        _require(type(maximum) is int and 0 < maximum <= MAX_DISCOVERY_RESOURCES)
        snapshot = reader.read()
        check_manifest(snapshot)
        if FORMAT_RESOURCE_ID not in snapshot.resources or any(
            k.startswith("format:") and k != FORMAT_RESOURCE_ID
            for k in snapshot.resources
        ):
            raise EncryptedStoreUnsupported()
        cached_header = (
            _vault is not None
            and _header is not None
            and snapshot.resources[FORMAT_RESOURCE_ID] == _header
        )
        vault = (
            _vault
            if cached_header
            else unlock_encrypted_snapshot(reader, snapshot, device_keys)
        )
        business = {
            k: v for k, v in snapshot.resources.items() if k != FORMAT_RESOURCE_ID
        }
        _require(all(_OPAQUE.fullmatch(k) for k in business))
        _require(
            all(
                d.size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES
                for d in business.values()
            )
        )
        _require(all(set(d.references) <= business.keys() for d in business.values()))
        cached = _load_checkpoint(checkpoint, vault, snapshot.sync_id)
        entries = {k: v for k, v in cached.items() if business.get(k) == v[0]}
        # Only project identities authenticated in private can introduce a tag.
        private_tag = vault.collection("private").tag
        collections = {private_tag: "private"}
        for descriptor, resource, plain in entries.values():
            if resource.kind == "project":
                collection = f"project/{resource.name}/memory"
                tag = vault.collection(collection).tag
                _require(tag not in collections or collections[tag] == collection)
                collections[tag] = collection
        remaining = business.keys() - entries.keys()
        private = sorted(k for k in remaining if k.split(":")[1] == private_tag)
        # Defer project resources until all private identities have been verified.
        candidates = private if private else sorted(remaining)
        if not private:
            _require(all(k.split(":")[1] in collections for k in candidates))
        request_cost = (
            0
            if cached_header
            else len(encode_fetch_request(snapshot, (FORMAT_RESOURCE_ID,)))
        )
        response_cost = (
            0 if cached_header else fetch_response_size(snapshot, (FORMAT_RESOURCE_ID,))
        )
        selected = []
        for identity in candidates:
            proposed = selected + [identity]
            if (
                request_cost + len(encode_fetch_request(snapshot, proposed))
                <= remote_limits.MAX_REMOTE_REQUEST_BYTES
                and response_cost + fetch_response_size(snapshot, proposed)
                <= remote_limits.MAX_REMOTE_RESPONSE_BYTES
            ):
                selected.append(identity)
                if len(selected) == maximum:
                    break
        if candidates and not selected:
            raise DiscoveryBudgetExceeded()
        payloads = _fetch(reader, snapshot, tuple(selected)) if selected else {}
        for identity in selected:
            collection = collections[identity.split(":")[1]]
            resource, payload = vault.decrypt(
                identity,
                business[identity],
                payloads[identity],
                snapshot.sync_id,
                collection,
            )
            plain = ResourceDescriptor(
                resource.fingerprint,
                payload_hash(payload),
                len(encode_payload(payload)),
                resource.references,
                resource.mode_fingerprint,
            )
            entries[identity] = (business[identity], resource, plain)
        resources = {resource.id: resource for _, resource, _ in entries.values()}
        _require(len(resources) == len(entries))
        left = len(business) - len(resources)
        if not left:
            _require(not missing_references(resources))
        return DiscoveredSnapshot(
            snapshot,
            resources,
            left,
            len(selected),
            _encode_checkpoint(entries, vault, snapshot.sync_id),
            {resource.id: plain for _, resource, plain in entries.values()},
        )
    except (EncryptedRemoteError, EncryptedStoreUnsupported):
        raise
    except (TypeError, KeyError, AttributeError, WorkspaceCoreError):
        raise EncryptedRemoteError() from None


def inspect_bound_workspace(workspace: Path, *, home: Path) -> DiscoveredSnapshot:
    """Persist only authenticated discovery progress under the workspace lock."""
    with WorkspaceWriterLock(home):
        path = state_path(workspace, DISCOVERY_STATE)
        checkpoint = None
        if entry_type(path) == "file":
            with path.open("rb") as stream:
                checkpoint = stream.read(
                    2 * remote_limits.MAX_REMOTE_RESPONSE_BYTES + 1
                )
        result = discover_encrypted(
            open_bound_reader(workspace, home=home),
            DeviceKeyStore(home=home, workspace=workspace),
            checkpoint=checkpoint,
        )
        path = state_path(workspace, DISCOVERY_STATE, create=True)
        atomic_text(path, result.checkpoint.decode("utf-8"))
        return result


def unlock_encrypted_snapshot(reader, snapshot, device_keys):
    """Authenticate a pinned header before reading business data or retrying writes."""
    if FORMAT_RESOURCE_ID not in snapshot.resources or any(
        k.startswith("format:") and k != FORMAT_RESOURCE_ID for k in snapshot.resources
    ):
        raise EncryptedStoreUnsupported()
    header_descriptor = snapshot.resources[FORMAT_RESOURCE_ID]
    _require(
        header_descriptor.size
        <= len(encode_payload(FilePayload(bytes(MAX_METADATA_BYTES))))
    )
    _require(
        not header_descriptor.references and header_descriptor.mode_fingerprint is None
    )
    header = _fetch(reader, snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID]
    _require(metadata_resource(header.data)[0] == header_descriptor)
    vault_id = metadata_vault_id(header.data)
    key = device_keys.load(vault_id)
    if key is None:
        raise EncryptedStoreLocked()
    verify_metadata(header.data, key)
    return EncryptedVault(vault_id, key)
