"""Authenticated encrypted resources without changing the storage contract.

Vault keys never enter resource payloads. Callers receive opaque descriptors
and portable ciphertext, or validated logical resources after decryption.
Cryptography is imported only when an encrypted operation is requested.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
from dataclasses import dataclass, field, replace

from . import remote_limits
from .payload import (
    FilePayload,
    ResourceDescriptor,
    ResourcePayload,
    decode_payload,
    encode_payload,
    payload_hash,
    validate_payload,
    credential_payload,
)
from .remote_format import EncryptedStoreUnsupported
from .remote_store import StoreError
from .remote_wire import _canonical, decode_state_json
from .resource_state import local_resource_for_id
from .resources import Resource

SUITE = "hkdf-sha256+a256gcm"
MAX_METADATA_BYTES = 65536
RECOVERY_PREFIX = "aikito-recovery-v1:"


class EncryptedRemoteError(StoreError):
    code = "encrypted_content_invalid"
    message = "Encrypted content is invalid."

    def __init__(self):
        super().__init__(f"{self.code}: {self.message}")


class EncryptedDependencyMissing(EncryptedRemoteError):
    code = "encrypted_dependency_missing"
    message = (
        "Install encrypted support with python -m pip install 'aikito[encrypted]'."
    )


class RecoveryKeyInvalid(EncryptedRemoteError):
    code = "recovery_key_invalid"
    message = "The recovery code or recovery header is invalid."


class KeyStorageUnavailable(EncryptedRemoteError):
    code = "key_storage_unavailable"
    message = "Device key storage is unavailable or insecure."


def _crypto():
    # Optional imports must not make the core CLI or format guard depend on AES.
    try:
        from cryptography.exceptions import InvalidTag
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        from cryptography.hazmat.primitives.kdf.hkdf import HKDF
    except ImportError:
        raise EncryptedDependencyMissing() from None
    return AESGCM, HKDF, hashes.SHA256, InvalidTag


def require_encrypted_support() -> None:
    _crypto()


def _require(condition):
    if not condition:
        raise EncryptedRemoteError()


def validate_vault_id(vault_id: str) -> None:
    _require(type(vault_id) is str and re.fullmatch(r"[0-9a-f]{32}", vault_id))


def _key(value):
    _require(type(value) is bytes and len(value) == 32)


def _sync(sync_id):
    _require(type(sync_id) is str and bool(sync_id))


def _b64(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _unb64(value, size=None):
    try:
        _require(type(value) is str)
        result = base64.b64decode(value, validate=True)
        _require(_b64(result) == value and (size is None or len(result) == size))
        return result
    except (ValueError, TypeError):
        raise EncryptedRemoteError() from None


def _domain(name, **values):
    return {"domain": name, "version": 1, **values}


def _hkdf(ikm, salt, info):
    _, HKDF, SHA256, _ = _crypto()
    return HKDF(algorithm=SHA256(), length=32, salt=salt, info=_canonical(info)).derive(
        ikm
    )


def _mac(key, value):
    return hmac.new(key, _canonical(value), hashlib.sha256).digest()


def _parse(encoded, fields):
    try:
        _require(type(encoded) is bytes)
        value = decode_state_json(encoded)
        _require(type(value) is dict and set(value) == set(fields))
        pending = [value]
        while pending:
            item = pending.pop()
            if type(item) is dict:
                pending.extend(item.values())
            elif type(item) is list:
                pending.extend(item)
            elif type(item) is int:
                _require(0 <= item <= 2**53 - 1)
            else:
                _require(type(item) in (str, bool, type(None)))
        _require(_canonical(value) == encoded)
        return value
    except (ValueError, TypeError, RecursionError, UnicodeError):
        raise EncryptedRemoteError() from None


def _version(value):
    _require(
        type(value.get("version")) is int and type(value.get("key_version")) is int
    )
    if (
        value["version"] != 1
        or value["key_version"] != 1
        or value["algorithm"] != SUITE
    ):
        raise EncryptedStoreUnsupported()


def collection_for_id(logical_id: str) -> str:
    _require(type(logical_id) is str)
    kind, separator, name = logical_id.partition(":")
    _require(separator and name)
    if kind == "project-memory":
        project, separator, member = name.partition("/")
        _require(project and separator and member)
        local_resource_for_id(f"project:{project}", "")
        return f"project/{project}/memory"
    return "private"


@dataclass(frozen=True)
class CollectionKeys:
    content: bytes = field(repr=False)
    resource_id: bytes = field(repr=False)
    fingerprint: bytes = field(repr=False)
    tag_key: bytes = field(repr=False)

    @property
    def tag(self):
        return _mac(self.tag_key, _domain("aikito.collection-tag"))[:16].hex()

    def opaque_id(self, logical_id):
        digest = _mac(
            self.resource_id, _domain("aikito.resource-id", logical_id=logical_id)
        ).hex()
        return f"e1:{self.tag}:{digest}"

    def fingerprints(self, logical, mode):
        fingerprint = _mac(
            self.fingerprint, _domain("aikito.fingerprint", logical_fingerprint=logical)
        ).hex()
        keyed_mode = (
            None
            if mode is None
            else _mac(
                self.fingerprint,
                _domain("aikito.mode-fingerprint", logical_mode_fingerprint=mode),
            ).hex()
        )
        return fingerprint, keyed_mode


def derive_collection(vrk: bytes, vault_id: str, collection_id: str) -> CollectionKeys:
    _key(vrk)
    validate_vault_id(vault_id)
    _require(type(collection_id) is str)
    if collection_id != "private":
        parts = collection_id.split("/")
        _require(len(parts) == 3 and parts[0] == "project" and parts[2] == "memory")
        local_resource_for_id(f"project:{parts[1]}", "")
    salt = bytes.fromhex(vault_id)
    master = _hkdf(
        vrk,
        salt,
        _domain("aikito.collection", collection_id=collection_id, key_version=1),
    )
    return CollectionKeys(
        *(
            _hkdf(
                master,
                salt,
                _domain("aikito.collection-key", key_version=1, purpose=purpose),
            )
            for purpose in ("content", "resource-id", "fingerprint", "tag")
        )
    )


def _logical(resource, payload):
    canonical = local_resource_for_id(resource.id, resource.fingerprint)
    if canonical.kind not in {"mcp", "subagent"}:
        _require(resource.references == canonical.references)
    if resource.mode_fingerprint is not None:
        _require(
            resource.kind == "skill"
            and type(resource.mode_fingerprint) is str
            and re.fullmatch(r"[0-9a-f]{64}", resource.mode_fingerprint)
        )
    canonical = replace(
        canonical,
        references=resource.references,
        mode_fingerprint=resource.mode_fingerprint,
    )
    validate_payload(canonical, payload)
    _require(not credential_payload(payload))
    return canonical


def _inner(resource, payload):
    return {
        "fingerprint": resource.fingerprint,
        "key_version": 1,
        "logical_id": resource.id,
        "logical_parts": [
            {"path": part.path, "table": part.table} for part in resource.parts
        ],
        "mode_fingerprint": resource.mode_fingerprint,
        "payload": _b64(encode_payload(payload)),
        "references": list(resource.references),
        "resource_type": resource.kind,
        "schema_version": 1,
    }


@dataclass(frozen=True)
class EncryptedVault:
    vault_id: str
    vrk: bytes = field(repr=False)

    def __post_init__(self):
        validate_vault_id(self.vault_id)
        _key(self.vrk)

    def collection(self, collection_id):
        return derive_collection(self.vrk, self.vault_id, collection_id)

    def opaque_id(self, logical_id):
        return self.collection(collection_for_id(logical_id)).opaque_id(logical_id)

    def _aad(self, sync_id, resource_id, tag, descriptor):
        _sync(sync_id)
        return _domain(
            "aikito.resource-aad",
            algorithm=SUITE,
            collection_tag=tag,
            fingerprint=descriptor.fingerprint,
            key_version=1,
            mode_fingerprint=descriptor.mode_fingerprint,
            references=list(descriptor.references),
            resource_id=resource_id,
            sync_id=sync_id,
            vault_id=self.vault_id,
        )

    def encrypt(
        self,
        resource: Resource,
        payload: ResourcePayload,
        sync_id: str,
        *,
        existing=None,
    ):
        AESGCM, _, _, _ = _crypto()
        try:
            resource = _logical(resource, payload)
            collection_id = collection_for_id(resource.id)
            if existing is not None:
                old_resource, old_payload = self.decrypt(
                    *existing, sync_id, collection_id
                )
                if old_resource == resource and encode_payload(
                    old_payload
                ) == encode_payload(payload):
                    return existing
            keys = self.collection(collection_id)
            remote_id = keys.opaque_id(resource.id)
            fingerprint, mode = keys.fingerprints(
                resource.fingerprint, resource.mode_fingerprint
            )
            references = tuple(
                sorted({self.opaque_id(ref) for ref in resource.references})
            )
            descriptor = ResourceDescriptor(fingerprint, "0" * 64, 0, references, mode)
            salt, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
            dek = _hkdf(keys.content, salt, _domain("aikito.content-aead"))
            ciphertext = AESGCM(dek).encrypt(
                nonce,
                _canonical(_inner(resource, payload)),
                _canonical(self._aad(sync_id, remote_id, keys.tag, descriptor)),
            )
            envelope = {
                "algorithm": SUITE,
                "ciphertext": _b64(ciphertext),
                "key_version": 1,
                "nonce": _b64(nonce),
                "salt": _b64(salt),
                "version": 1,
            }
            outer = FilePayload(_canonical(envelope))
            size = len(encode_payload(outer))
            _require(size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES)
            return (
                remote_id,
                replace(descriptor, content_hash=payload_hash(outer), size=size),
                outer,
            )
        except EncryptedRemoteError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise EncryptedRemoteError() from None

    def decrypt(
        self,
        remote_id: str,
        descriptor: ResourceDescriptor,
        outer: FilePayload,
        sync_id: str,
        collection_id: str,
    ):
        AESGCM, _, _, InvalidTag = _crypto()
        try:
            _require(
                type(outer) is FilePayload and type(descriptor) is ResourceDescriptor
            )
            _require(descriptor.size <= remote_limits.MAX_RESOURCE_PAYLOAD_BYTES)
            _require(
                len(encode_payload(outer)) == descriptor.size
                and payload_hash(outer) == descriptor.content_hash
            )
            _require(descriptor.references == tuple(sorted(set(descriptor.references))))
            envelope = _parse(
                outer.data,
                ("algorithm", "ciphertext", "key_version", "nonce", "salt", "version"),
            )
            _version(envelope)
            keys = self.collection(collection_id)
            _require(
                type(remote_id) is str
                and re.fullmatch(r"e1:[0-9a-f]{32}:[0-9a-f]{64}", remote_id)
            )
            _require(remote_id.split(":")[1] == keys.tag)
            dek = _hkdf(
                keys.content,
                _unb64(envelope["salt"], 32),
                _domain("aikito.content-aead"),
            )
            plaintext = AESGCM(dek).decrypt(
                _unb64(envelope["nonce"], 12),
                _unb64(envelope["ciphertext"]),
                _canonical(self._aad(sync_id, remote_id, keys.tag, descriptor)),
            )
            inner = _parse(
                plaintext,
                (
                    "fingerprint",
                    "key_version",
                    "logical_id",
                    "logical_parts",
                    "mode_fingerprint",
                    "payload",
                    "references",
                    "resource_type",
                    "schema_version",
                ),
            )
            _require(
                type(inner["key_version"]) is int
                and inner["key_version"] == 1
                and type(inner["schema_version"]) is int
                and inner["schema_version"] == 1
            )
            _require(
                type(inner["references"]) is list
                and all(type(ref) is str for ref in inner["references"])
            )
            resource = local_resource_for_id(inner["logical_id"], inner["fingerprint"])
            _require(
                resource.kind == inner["resource_type"]
                and collection_for_id(resource.id) == collection_id
            )
            resource = replace(
                resource,
                references=tuple(inner["references"]),
                mode_fingerprint=inner["mode_fingerprint"],
            )
            raw = _unb64(inner["payload"])
            payload = decode_payload(raw, hashlib.sha256(raw).hexdigest())
            resource = _logical(resource, payload)
            _require(inner == _inner(resource, payload))
            _require(remote_id == keys.opaque_id(resource.id))
            _require(
                (descriptor.fingerprint, descriptor.mode_fingerprint)
                == keys.fingerprints(resource.fingerprint, resource.mode_fingerprint)
            )
            _require(
                descriptor.references
                == tuple(sorted({self.opaque_id(ref) for ref in resource.references}))
            )
            return resource, payload
        except (EncryptedDependencyMissing, EncryptedStoreUnsupported):
            raise
        except (ValueError, TypeError, AttributeError, KeyError, InvalidTag):
            raise EncryptedRemoteError() from None


def encode_recovery_code(code: bytes) -> str:
    _key(code)
    return RECOVERY_PREFIX + code.hex()


def decode_recovery_code(text: str) -> bytes:
    try:
        _require(type(text) is str)
        text = text.strip()
        _require(text.startswith(RECOVERY_PREFIX))
        suffix = text[len(RECOVERY_PREFIX) :]
        _require(re.fullmatch(r"[0-9a-f]{64}", suffix))
        return bytes.fromhex(suffix)
    except (ValueError, TypeError):
        raise RecoveryKeyInvalid() from None


def _metadata(encoded):
    _require(type(encoded) is bytes and len(encoded) <= MAX_METADATA_BYTES)
    metadata = _parse(
        encoded,
        (
            "algorithm",
            "format",
            "key_version",
            "metadata_mac",
            "migration",
            "recovery",
            "vault_id",
            "version",
        ),
    )
    _version(metadata)
    validate_vault_id(metadata["vault_id"])
    _require(
        type(metadata["metadata_mac"]) is str
        and re.fullmatch(r"[0-9a-f]{64}", metadata["metadata_mac"])
    )
    _require(metadata["format"] in ("encrypted-v1", "migrating"))
    migration = metadata["migration"]
    if metadata["format"] == "encrypted-v1":
        _require(migration is None)
    else:
        _require(
            type(migration) is dict
            and set(migration) == {"epoch", "owner_client_id", "token"}
        )
        _require(type(migration["epoch"]) is int and migration["epoch"] > 0)
        _require(
            type(migration["owner_client_id"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", migration["owner_client_id"])
        )
        _require(
            type(migration["token"]) is str
            and re.fullmatch(r"[0-9a-f]{32}", migration["token"])
        )
    recovery = metadata["recovery"]
    _require(
        type(recovery) is dict
        and set(recovery) == {"ciphertext", "nonce", "recovery_id", "salt", "version"}
    )
    _require(type(recovery["version"]) is int and recovery["version"] == 1)
    _require(
        type(recovery["recovery_id"]) is str
        and re.fullmatch(r"[0-9a-f]{32}", recovery["recovery_id"])
    )
    _unb64(recovery["ciphertext"], 48)
    _unb64(recovery["nonce"], 12)
    _unb64(recovery["salt"], 32)
    return metadata


def _metadata_mac(metadata, vrk):
    metadata_key = _hkdf(
        vrk, bytes.fromhex(metadata["vault_id"]), _domain("aikito.metadata-key")
    )
    return _mac(
        metadata_key,
        {key: value for key, value in metadata.items() if key != "metadata_mac"},
    ).hex()


def verify_metadata(encoded: bytes, vrk: bytes):
    _key(vrk)
    metadata = _metadata(encoded)
    _require(
        hmac.compare_digest(metadata["metadata_mac"], _metadata_mac(metadata, vrk))
    )
    return metadata


def _recovery_aad(metadata, sync_id):
    _sync(sync_id)
    return _domain(
        "aikito.recovery-aad",
        algorithm=SUITE,
        key_version=1,
        recovery_id=metadata["recovery"]["recovery_id"],
        sync_id=sync_id,
        vault_id=metadata["vault_id"],
    )


def create_recovery_metadata(
    vrk: bytes,
    vault_id: str,
    sync_id: str,
    recovery_code: bytes,
    *,
    format="encrypted-v1",
    migration=None,
) -> bytes:
    AESGCM, _, _, _ = _crypto()
    _key(vrk)
    _key(recovery_code)
    validate_vault_id(vault_id)
    metadata = {
        "algorithm": SUITE,
        "format": format,
        "key_version": 1,
        "metadata_mac": "0" * 64,
        "migration": migration,
        "recovery": {"recovery_id": secrets.token_hex(16), "version": 1},
        "vault_id": vault_id,
        "version": 1,
    }
    salt, nonce = secrets.token_bytes(32), secrets.token_bytes(12)
    kek = _hkdf(recovery_code, salt, _domain("aikito.recovery-key"))
    wrapped = AESGCM(kek).encrypt(
        nonce, vrk, _canonical(_recovery_aad(metadata, sync_id))
    )
    metadata["recovery"].update(
        ciphertext=_b64(wrapped), nonce=_b64(nonce), salt=_b64(salt)
    )
    metadata["metadata_mac"] = _metadata_mac(metadata, vrk)
    encoded = _canonical(metadata)
    verify_metadata(encoded, vrk)
    return encoded


def recover_vault(encoded: bytes, sync_id: str, recovery_code: bytes) -> EncryptedVault:
    AESGCM, _, _, InvalidTag = _crypto()
    try:
        _key(recovery_code)
        metadata = _metadata(encoded)
        recovery = metadata["recovery"]
        kek = _hkdf(
            recovery_code, _unb64(recovery["salt"], 32), _domain("aikito.recovery-key")
        )
        vrk = AESGCM(kek).decrypt(
            _unb64(recovery["nonce"], 12),
            _unb64(recovery["ciphertext"], 48),
            _canonical(_recovery_aad(metadata, sync_id)),
        )
        verify_metadata(encoded, vrk)
        return EncryptedVault(metadata["vault_id"], vrk)
    except EncryptedStoreUnsupported:
        raise
    except (ValueError, TypeError, KeyError, InvalidTag):
        raise RecoveryKeyInvalid() from None


def replace_recovery_code(
    encoded: bytes, vrk: bytes, sync_id: str, code: bytes
) -> bytes:
    metadata = verify_metadata(encoded, vrk)
    return create_recovery_metadata(
        vrk,
        metadata["vault_id"],
        sync_id,
        code,
        format=metadata["format"],
        migration=metadata["migration"],
    )


def metadata_resource(encoded: bytes):
    _metadata(encoded)
    payload = FilePayload(encoded)
    return ResourceDescriptor(
        hashlib.sha256(encoded).hexdigest(),
        payload_hash(payload),
        len(encode_payload(payload)),
    ), payload
