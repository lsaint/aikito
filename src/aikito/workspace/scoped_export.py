"""Explicit collection subkeys, sealed to a recipient without implicit upgrades.

This local export does not establish account ownership, authenticate its sender,
or consume a one-time grant. Receivers must verify those separately. Expiry is
checked at import and use; copied raw keys cannot be cryptographically revoked.
Read material cannot recompute keyed IDs/fingerprints or private parent mappings.
"""

from __future__ import annotations

import hashlib
import os
import re
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

from ..compat import is_windows, secure_file_permissions
from . import remote_limits
from .device_keys import DeviceKeyStore
from .encrypted_discovery import _fetch, unlock_encrypted_snapshot
from .encrypted_remote import (
    EncryptedDependencyMissing,
    EncryptedRemoteError,
    _b64,
    _parse,
    _require,
    _unb64,
    decrypt_collection_content,
    validate_vault_id,
)
from .reconcile_budget import check_manifest, fetch_response_size
from .remote_binding import load_remote_binding
from .remote_factory import BoundRemoteReader, _open_remote
from .remote_format import FORMAT_RESOURCE_ID
from .remote_protocol import encode_fetch_request
from .remote_wire import _canonical
from .resource_collections import collection_for_resource, parse_collection
from .resource_state import local_resource_for_id

EXPORT_SUITE = "hpke-x25519-hkdf-sha256-a256gcm"
MAX_EXPORT_BYTES = 65536
_AUTH_FIELDS = (
    "version",
    "account_id",
    "store_id",
    "sync_id",
    "vault_id",
    "collection_id",
    "recipient_key_id",
    "permissions",
    "grant_id",
    "nonce",
    "expires_at",
)
_CONTEXT_FIELDS = _AUTH_FIELDS + (
    "algorithm",
    "key_version",
    "collection_tag",
    "parent_project_id",
    "reference_bindings",
    "recipient_public_key_sha256",
)


class ScopedExportInvalid(EncryptedRemoteError):
    code = "scoped_export_invalid"
    message = "Scoped export input, recipient or output is invalid."


class ScopedExportDenied(EncryptedRemoteError):
    code = "scoped_export_denied"
    message = "Only explicitly selected collection keys with matching permission can be exported."


class CollectionEmpty(ScopedExportDenied):
    code = "collection_empty"
    message = (
        "Create and synchronize a valid resource before exporting this collection."
    )


def _hpke():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PrivateKey,
            X25519PublicKey,
        )
        from cryptography.hazmat.primitives.hpke import AEAD, KDF, KEM, Suite
    except ImportError:
        raise EncryptedDependencyMissing() from None
    return (
        Suite(KEM.X25519, KDF.HKDF_SHA256, AEAD.AES_256_GCM),
        X25519PublicKey,
        X25519PrivateKey,
        InvalidTag,
    )


def _now(now):
    result = int(time.time()) if now is None else now
    _require(type(result) is int and 0 <= result <= 2**53 - 1)
    return result


def _collection(collection_id):
    collection = parse_collection(collection_id)
    return (
        None
        if collection.parent_id is None
        else local_resource_for_id(collection.parent_id, "")
    )


def _authorization(encoded, now, permission="read"):
    _require(type(encoded) is bytes and len(encoded) <= MAX_EXPORT_BYTES)
    auth = _parse(encoded, _AUTH_FIELDS)
    _require(type(auth["version"]) is int and auth["version"] == 2)
    _require(
        type(auth["account_id"]) is str
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", auth["account_id"])
    )
    for name in ("store_id", "recipient_key_id", "grant_id"):
        _require(
            type(auth[name]) is str
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", auth[name])
        )
    _require(type(auth["sync_id"]) is str and bool(auth["sync_id"]))
    validate_vault_id(auth["vault_id"])
    _collection(auth["collection_id"])
    expected = {"read": ["read"], "write": ["read", "write"]}.get(permission)
    if expected is None or auth["permissions"] != expected:
        raise ScopedExportDenied()
    _unb64(auth["nonce"], 32)
    _require(type(auth["expires_at"]) is int and now < auth["expires_at"] <= 2**53 - 1)
    return auth


def _recipient(encoded):
    _require(type(encoded) is bytes and len(encoded) <= MAX_EXPORT_BYTES)
    document = _parse(encoded, ("version", "algorithm", "key_id", "public_key"))
    _require(type(document["version"]) is int and document["version"] == 1)
    _require(document["algorithm"] == EXPORT_SUITE)
    _require(
        type(document["key_id"]) is str
        and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", document["key_id"])
    )
    key = _unb64(document["public_key"], 32)
    return document, key


def _info(context, permission="read"):
    domain = {
        "read": "aikito.scoped-read-export-v2",
        "write": "aikito.scoped-write-export-v2",
    }[permission]
    return _canonical({"domain": domain, "context": context})


def _validate_context(context, auth):
    fields = set(_AUTH_FIELDS) | {
        "algorithm",
        "key_version",
        "collection_tag",
        "parent_project_id",
        "reference_bindings",
        "recipient_public_key_sha256",
    }
    _require(type(context) is dict and set(context) == fields)
    _require(
        all(
            context[k] == value and type(context[k]) is type(value)
            for k, value in auth.items()
        )
    )
    _require(context["algorithm"] == EXPORT_SUITE)
    _require(type(context["key_version"]) is int and context["key_version"] == 1)
    tag = context["collection_tag"]
    _require(type(tag) is str and re.fullmatch(r"[0-9a-f]{32}", tag))
    parent = context["parent_project_id"]
    project = _collection(auth["collection_id"])
    if project is None:
        _require(parent is None)
    else:
        _require(
            type(parent) is str
            and re.fullmatch(r"e1:[0-9a-f]{32}:[0-9a-f]{64}", parent)
        )
        _require(parent.split(":")[1] != tag)
    _require(
        type(context["recipient_public_key_sha256"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", context["recipient_public_key_sha256"])
    )
    bindings = context["reference_bindings"]
    _require(type(bindings) is list)
    identities = []
    mapping = {}
    for binding in bindings:
        _require(
            type(binding) is dict and set(binding) == {"logical_id", "resource_id"}
        )
        identity, opaque = binding["logical_id"], binding["resource_id"]
        _require(collection_for_resource(identity) != auth["collection_id"])
        _require(
            type(opaque) is str
            and re.fullmatch(r"e1:[0-9a-f]{32}:[0-9a-f]{64}", opaque)
        )
        _require(opaque.split(":")[1] != tag)
        identities.append(identity)
        mapping[identity] = opaque
    _require(identities == sorted(set(identities)))
    if project is not None:
        _require(mapping.get(project.id) == parent)


def _export_context(reader, vault, snapshot, auth, public):
    # All resources in the collection contribute cross-collection bindings. The
    # shared wire budget includes the already fetched authenticated format header.
    collection = parse_collection(auth["collection_id"])
    keys = vault.collection(collection.id)
    candidates = sorted(
        k for k in snapshot.resources if k.startswith(f"e1:{keys.tag}:")
    )
    if not candidates:
        raise CollectionEmpty()
    request_cost = len(encode_fetch_request(snapshot, (FORMAT_RESOURCE_ID,)))
    response_cost = fetch_response_size(snapshot, (FORMAT_RESOURCE_ID,))

    def fetch(identities):
        nonlocal request_cost, response_cost
        request_cost += len(encode_fetch_request(snapshot, identities))
        response_cost += fetch_response_size(snapshot, identities)
        _require(request_cost <= remote_limits.MAX_REMOTE_REQUEST_BYTES)
        _require(response_cost <= remote_limits.MAX_REMOTE_RESPONSE_BYTES)
        _require(
            all(
                snapshot.resources[k].size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES
                for k in identities
            )
        )
        return _fetch(reader, snapshot, identities)

    payloads = fetch(tuple(candidates))
    references = set()
    for identity in candidates:
        resource, _ = vault.decrypt(
            identity,
            snapshot.resources[identity],
            payloads[identity],
            snapshot.sync_id,
            collection.id,
        )
        _require(resource.kind == collection.kind)
        _require(
            set(snapshot.resources[identity].references) <= snapshot.resources.keys()
        )
        references.update(
            ref
            for ref in resource.references
            if collection_for_resource(ref) != collection.id
        )
    if collection.parent_id is not None:
        references.add(collection.parent_id)
    bindings = [
        {"logical_id": identity, "resource_id": vault.opaque_id(identity)}
        for identity in sorted(references)
    ]
    _require(all(binding["resource_id"] in snapshot.resources for binding in bindings))
    context = dict(auth)
    context.update(
        algorithm=EXPORT_SUITE,
        key_version=1,
        collection_tag=keys.tag,
        parent_project_id=None
        if collection.parent_id is None
        else vault.opaque_id(collection.parent_id),
        reference_bindings=bindings,
        recipient_public_key_sha256=hashlib.sha256(public).hexdigest(),
    )
    _validate_context(context, auth)
    _require(len(_canonical(context)) <= MAX_EXPORT_BYTES)
    targets = tuple(binding["resource_id"] for binding in bindings)
    if targets:
        referenced = fetch(targets)
        for binding in bindings:
            identity = binding["logical_id"]
            opaque = binding["resource_id"]
            verified, _ = vault.decrypt(
                opaque,
                snapshot.resources[opaque],
                referenced[opaque],
                snapshot.sync_id,
                collection_for_resource(identity),
            )
            _require(verified.id == identity)
            _require(
                set(snapshot.resources[opaque].references) <= snapshot.resources.keys()
            )
    _require(reader.read() == snapshot)
    return context, keys


def _export_scoped(
    reader,
    device_keys,
    *,
    collection_id,
    recipient,
    authorization,
    permission,
    now=None,
):
    """Authenticate the header, parent and one sample; return only sealed material."""
    suite, PublicKey, _, _ = _hpke()
    now = _now(now)
    auth = _authorization(authorization, now, permission)
    _collection(collection_id)
    document, public = _recipient(recipient)
    if (
        auth["collection_id"] != collection_id
        or auth["recipient_key_id"] != document["key_id"]
    ):
        raise ScopedExportDenied()
    snapshot = reader.read()
    check_manifest(snapshot)
    _require(snapshot.sync_id == auth["sync_id"])
    vault = unlock_encrypted_snapshot(reader, snapshot, device_keys)
    _require(vault.vault_id == auth["vault_id"])
    context, keys = _export_context(reader, vault, snapshot, auth, public)
    material = {
        "version": auth["version"],
        "collection_id": collection_id,
        "content_key": _b64(keys.content),
    }
    if permission == "write":
        material.update(
            resource_id_key=_b64(keys.resource_id),
            fingerprint_key=_b64(keys.fingerprint),
        )
    plain = _canonical(material)
    try:
        ciphertext = suite.encrypt(
            plain, PublicKey.from_public_bytes(public), info=_info(context, permission)
        )
    except (ValueError, TypeError):
        raise ScopedExportInvalid() from None
    encoded = _canonical(
        {"version": auth["version"], "context": context, "ciphertext": _b64(ciphertext)}
    )
    _require(len(encoded) <= MAX_EXPORT_BYTES)
    return encoded


def export_scoped_read(reader, device_keys, **kwargs):
    return _export_scoped(reader, device_keys, permission="read", **kwargs)


def export_scoped_write(reader, device_keys, **kwargs):
    return _export_scoped(reader, device_keys, permission="write", **kwargs)


@dataclass(frozen=True)
class ScopedReadKey:
    context: dict = field(repr=False)
    collection_id: str = field(repr=False)
    content_key: bytes = field(repr=False)

    def __post_init__(self):
        context = dict(self.context)
        if "reference_bindings" in context:
            context["reference_bindings"] = tuple(
                MappingProxyType(dict(binding))
                for binding in context["reference_bindings"]
            )
        object.__setattr__(self, "context", MappingProxyType(context))

    def decrypt(self, remote_id, descriptor, payload, *, now=None):
        """Verify authenticated content within this scope, without full-vault claims."""
        if _now(now) >= self.context["expires_at"]:
            raise ScopedExportDenied()
        resource, plain = decrypt_collection_content(
            self.content_key,
            self.context["vault_id"],
            self.context["collection_tag"],
            remote_id,
            descriptor,
            payload,
            self.context["sync_id"],
            self.collection_id,
        )
        bindings = {
            binding["logical_id"]: binding["resource_id"]
            for binding in self.context["reference_bindings"]
        }
        _require(all(ref in bindings for ref in resource.references))
        _require(
            descriptor.references
            == tuple(sorted({bindings[ref] for ref in resource.references}))
        )
        return resource, plain


def open_scoped_read(encoded, private_key, *, authorization, now=None):
    """Open against a separately trusted request; this does not consume its nonce."""
    suite, _, PrivateKey, InvalidTag = _hpke()
    now = _now(now)
    auth = _authorization(authorization, now)
    _require(type(encoded) is bytes and len(encoded) <= MAX_EXPORT_BYTES)
    package = _parse(encoded, ("version", "context", "ciphertext"))
    _require(type(package["version"]) is int and package["version"] == auth["version"])
    context = package["context"]
    _validate_context(context, auth)
    try:
        _require(type(private_key) is bytes and len(private_key) == 32)
        private = PrivateKey.from_private_bytes(private_key)
        public = private.public_key().public_bytes_raw()
        _require(
            context["recipient_public_key_sha256"] == hashlib.sha256(public).hexdigest()
        )
        plaintext = suite.decrypt(
            _unb64(package["ciphertext"]), private, info=_info(context)
        )
    except (ValueError, TypeError, InvalidTag):
        raise ScopedExportInvalid() from None
    body = _parse(plaintext, ("version", "collection_id", "content_key"))
    _require(
        type(body["version"]) is int
        and body["version"] == auth["version"]
        and body["collection_id"] == auth["collection_id"]
    )
    return ScopedReadKey(
        context, body["collection_id"], _unb64(body["content_key"], 32)
    )


def _read_document(path):
    with Path(path).open("rb") as stream:
        return stream.read(MAX_EXPORT_BYTES + 1)


def _publish(path, encoded):
    # A complete encrypted package is published without overwriting any entry.
    path = Path(path)
    temporary = path.with_name(".scoped-export-" + uuid.uuid4().hex)
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as stream:
            if not secure_file_permissions(temporary):
                raise ScopedExportInvalid()
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        if not is_windows():
            fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
    finally:
        temporary.unlink(missing_ok=True)


def export_bound_scoped_keys(
    workspace,
    home,
    *,
    collection_id,
    recipient,
    authorization,
    output,
    permission="read",
):
    _hpke()
    if os.path.lexists(output):
        raise ScopedExportInvalid()
    documents = {
        "recipient": _read_document(recipient),
        "authorization": _read_document(authorization),
    }
    # Validate public inputs before opening a remote or loading any device key.
    auth = _authorization(documents["authorization"], _now(None), permission)
    _collection(collection_id)
    _require(auth["collection_id"] == collection_id)
    _recipient(documents["recipient"])
    binding = load_remote_binding(workspace)
    _require(binding is not None)
    reader = BoundRemoteReader(
        _open_remote(binding.endpoint, binding.auth, None, home=home), binding.sync_id
    )
    encoded = _export_scoped(
        reader,
        DeviceKeyStore(home=home, workspace=workspace),
        collection_id=collection_id,
        permission=permission,
        **documents,
    )
    _publish(output, encoded)


def export_bound_scoped_read(workspace, home, **kwargs):
    return export_bound_scoped_keys(workspace, home, permission="read", **kwargs)
