"""One explicitly selected project Memory key, sealed to a recipient.

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

from ..compat import secure_file_permissions, is_windows
from . import remote_limits
from .device_keys import DeviceKeyStore
from .encrypted_discovery import _fetch, unlock_encrypted_snapshot
from .encrypted_remote import (
    EncryptedRemoteError,
    EncryptedDependencyMissing,
    _parse,
    _require,
    _b64,
    _unb64,
    validate_vault_id,
    decrypt_collection_content,
)
from .reconcile_budget import check_manifest, fetch_response_size
from .remote_binding import load_remote_binding
from .remote_factory import BoundRemoteReader, _open_remote
from .remote_format import FORMAT_RESOURCE_ID
from .remote_protocol import encode_fetch_request
from .remote_wire import _canonical
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
_CONTEXT_FIELDS = tuple(k for k in _AUTH_FIELDS if k != "collection_id") + (
    "algorithm",
    "key_version",
    "collection_tag",
    "parent_project_id",
    "recipient_public_key_sha256",
)


class ScopedExportInvalid(EncryptedRemoteError):
    code = "scoped_export_invalid"
    message = "Scoped export input, recipient or output is invalid."


class ScopedExportDenied(EncryptedRemoteError):
    code = "scoped_export_denied"
    message = "Only an explicitly selected existing project Memory collection with read permission can be exported."


def _hpke():
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives.hpke import Suite, KEM, KDF, AEAD
        from cryptography.hazmat.primitives.asymmetric.x25519 import (
            X25519PublicKey,
            X25519PrivateKey,
        )
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
    if type(collection_id) is not str:
        raise ScopedExportDenied()
    parts = collection_id.split("/")
    if len(parts) != 3 or parts[0] != "project" or parts[2] != "memory":
        raise ScopedExportDenied()
    project = local_resource_for_id(f"project:{parts[1]}", "")
    return project


def _authorization(encoded, now):
    _require(type(encoded) is bytes and len(encoded) <= MAX_EXPORT_BYTES)
    auth = _parse(encoded, _AUTH_FIELDS)
    _require(type(auth["version"]) is int and auth["version"] == 1)
    for name in ("account_id", "store_id", "recipient_key_id", "grant_id"):
        _require(
            type(auth[name]) is str
            and re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", auth[name])
        )
    _require(type(auth["sync_id"]) is str and bool(auth["sync_id"]))
    validate_vault_id(auth["vault_id"])
    _collection(auth["collection_id"])
    if auth["permissions"] != ["read"]:
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


def _info(context):
    return _canonical({"domain": "aikito.scoped-read-export", "context": context})


def export_scoped_read(
    reader, device_keys, *, collection_id, recipient, authorization, now=None
):
    """Authenticate the header, parent and one sample; return only sealed material."""
    suite, PublicKey, _, _ = _hpke()
    now = _now(now)
    project = _collection(collection_id)
    auth = _authorization(authorization, now)
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
    keys = vault.collection(collection_id)
    parent = vault.opaque_id(project.id)
    candidates = sorted(
        k for k in snapshot.resources if k.startswith(f"e1:{keys.tag}:")
    )
    if parent not in snapshot.resources or not candidates:
        raise ScopedExportDenied()
    selected = (parent, candidates[0])
    _require(
        all(
            snapshot.resources[k].size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES
            for k in selected
        )
    )
    header = (FORMAT_RESOURCE_ID,)
    _require(
        len(encode_fetch_request(snapshot, selected))
        + len(encode_fetch_request(snapshot, header))
        <= remote_limits.MAX_REMOTE_REQUEST_BYTES
        and fetch_response_size(snapshot, selected)
        + fetch_response_size(snapshot, header)
        <= remote_limits.MAX_REMOTE_RESPONSE_BYTES
    )
    payloads = _fetch(reader, snapshot, selected)
    verified, _ = vault.decrypt(
        parent,
        snapshot.resources[parent],
        payloads[parent],
        snapshot.sync_id,
        "private",
    )
    _require(verified.id == project.id and verified.kind == "project")
    sample, _ = vault.decrypt(
        selected[1],
        snapshot.resources[selected[1]],
        payloads[selected[1]],
        snapshot.sync_id,
        collection_id,
    )
    _require(sample.kind == "project-memory" and sample.references == (project.id,))
    context = {k: v for k, v in auth.items() if k != "collection_id"}
    context.update(
        algorithm=EXPORT_SUITE,
        key_version=1,
        collection_tag=keys.tag,
        parent_project_id=parent,
        recipient_public_key_sha256=hashlib.sha256(public).hexdigest(),
    )
    plain = _canonical(
        {
            "version": 1,
            "collection_id": collection_id,
            "content_key": _b64(keys.content),
        }
    )
    try:
        ciphertext = suite.encrypt(
            plain, PublicKey.from_public_bytes(public), info=_info(context)
        )
    except (ValueError, TypeError):
        raise ScopedExportInvalid() from None
    return _canonical(
        {"version": 1, "context": context, "ciphertext": _b64(ciphertext)}
    )


@dataclass(frozen=True)
class ScopedReadKey:
    context: dict = field(repr=False)
    collection_id: str = field(repr=False)
    content_key: bytes = field(repr=False)

    def __post_init__(self):
        object.__setattr__(self, "context", MappingProxyType(dict(self.context)))

    def decrypt(self, remote_id, descriptor, payload, *, now=None):
        """Verify authenticated content within this scope, without full-vault claims."""
        if _now(now) >= self.context["expires_at"]:
            raise ScopedExportDenied()
        if descriptor.references != (self.context["parent_project_id"],):
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
        _require(resource.kind == "project-memory")
        return resource, plain


def open_scoped_read(encoded, private_key, *, authorization, now=None):
    """Open against a separately trusted request; this does not consume its nonce."""
    suite, _, PrivateKey, InvalidTag = _hpke()
    now = _now(now)
    auth = _authorization(authorization, now)
    _require(type(encoded) is bytes and len(encoded) <= MAX_EXPORT_BYTES)
    package = _parse(encoded, ("version", "context", "ciphertext"))
    _require(type(package["version"]) is int and package["version"] == 1)
    context = package["context"]
    _require(type(context) is dict and set(context) == set(_CONTEXT_FIELDS))
    _require(
        all(
            context[k] == auth[k] and type(context[k]) is type(auth[k])
            for k in auth
            if k != "collection_id"
        )
    )
    _require(
        context["algorithm"] == EXPORT_SUITE
        and type(context["key_version"]) is int
        and context["key_version"] == 1
    )
    _require(
        type(context["collection_tag"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", context["collection_tag"])
    )
    _require(
        type(context["parent_project_id"]) is str
        and re.fullmatch(r"e1:[0-9a-f]{32}:[0-9a-f]{64}", context["parent_project_id"])
    )
    _require(context["parent_project_id"].split(":")[1] != context["collection_tag"])
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
        and body["version"] == 1
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


def export_bound_scoped_read(
    workspace, home, *, collection_id, recipient, authorization, output
):
    _hpke()
    _collection(collection_id)
    if os.path.lexists(output):
        raise ScopedExportInvalid()
    documents = dict(
        recipient=_read_document(recipient), authorization=_read_document(authorization)
    )
    # Validate public inputs before opening a remote or loading any device key.
    _authorization(documents["authorization"], _now(None))
    _recipient(documents["recipient"])
    binding = load_remote_binding(workspace)
    _require(binding is not None)
    reader = BoundRemoteReader(
        _open_remote(binding.endpoint, binding.auth, None, home=home), binding.sync_id
    )
    encoded = export_scoped_read(
        reader,
        DeviceKeyStore(home=home, workspace=workspace),
        collection_id=collection_id,
        **documents,
    )
    _publish(output, encoded)
