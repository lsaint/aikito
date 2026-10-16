"""Frozen bytes, authenticated contexts, and portable resource semantics."""

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from aikito.workspace import encrypted_remote as crypto
from aikito.workspace.payload import (
    ResourceDescriptor,
    FilePayload,
    MemberPayload,
    TomlPayload,
    TreeEntry,
    TreePayload,
    inspect_payload,
    tree_mode_fingerprint,
    encode_payload,
    payload_hash,
)
from aikito.workspace.remote_format import EncryptedStoreUnsupported
from aikito.workspace.remote_wire import _canonical
from aikito.workspace.resource_state import local_resource_for_id, SYNC_KINDS

pytest.importorskip("cryptography")

VECTOR = json.loads(
    (
        Path(__file__).parent / "fixtures/remote_protocol_vectors/encrypted-v1.json"
    ).read_text()
)
INPUT = VECTOR["inputs"]
VRK = bytes.fromhex(INPUT["vrk_hex"])
VAULT = INPUT["vault_id"]
SYNC = INPUT["sync_id"]


def resource(identity, payload):
    initial = local_resource_for_id(
        identity, "" if isinstance(payload, MemberPayload) else "0" * 64
    )
    fingerprint, references = inspect_payload(initial, payload)
    return replace(
        initial,
        fingerprint=fingerprint,
        references=references,
        mode_fingerprint=tree_mode_fingerprint(payload)
        if isinstance(payload, TreePayload)
        else None,
    )


def random_sequence(monkeypatch, chunks):
    chunks = iter(chunks)

    def next_bytes(size):
        value = next(chunks)
        assert len(value) == size
        return value

    monkeypatch.setattr(crypto.secrets, "token_bytes", next_bytes)


def header(monkeypatch):
    random_sequence(
        monkeypatch,
        [
            bytes.fromhex(INPUT["recovery_id"]),
            bytes.fromhex(INPUT["recovery_salt_hex"]),
            bytes.fromhex(INPUT["recovery_nonce_hex"]),
        ],
    )
    return crypto.create_recovery_metadata(
        VRK, VAULT, SYNC, bytes.fromhex(INPUT["recovery_code_hex"])
    )


def test_all_collection_keys_ids_and_fingerprints_match_frozen_vector():
    for collection, expected in VECTOR["collections"].items():
        keys = crypto.derive_collection(VRK, VAULT, collection)
        for field, name in (
            ("content", "content"),
            ("resource_id", "resource-id"),
            ("fingerprint", "fingerprint"),
            ("tag_key", "tag"),
        ):
            assert getattr(keys, field).hex() == expected["keys"][name]
        assert keys.tag == expected["tag"]
        project = "demo" if collection == "project/demo/memory" else "other"
        identity = (
            "project:demo"
            if collection == "private"
            else f"project-memory:{project}/notes/example.md"
        )
        fingerprint = "" if collection == "private" else VECTOR["inner"]["fingerprint"]
        assert keys.opaque_id(identity) == expected["resource_id"]
        assert keys.fingerprints(fingerprint, None) == (expected["fingerprint"], None)
        assert VRK.hex() not in repr(keys)


def test_frozen_envelope_and_original_payload_match_exactly(monkeypatch):
    payload = FilePayload(bytes.fromhex(INPUT["file_data_hex"]))
    logical = resource(VECTOR["inner"]["logical_id"], payload)
    random_sequence(
        monkeypatch,
        [
            bytes.fromhex(INPUT["content_salt_hex"]),
            bytes.fromhex(INPUT["content_nonce_hex"]),
        ],
    )
    vault = crypto.EncryptedVault(VAULT, VRK)
    identity, descriptor, outer = vault.encrypt(logical, payload, SYNC)
    assert identity == VECTOR["aad"]["resource_id"]
    assert json.loads(outer.data) == VECTOR["envelope"]
    assert descriptor.content_hash == VECTOR["content_hash"]
    assert descriptor.size == VECTOR["size"]
    assert json.loads(encode_payload(outer)) == VECTOR["outer_file"]
    assert vault.decrypt(identity, descriptor, outer, SYNC, "project/demo/memory") == (
        logical,
        payload,
    )


@pytest.mark.parametrize(
    "identity,payload",
    [
        ("memory:notes/unicode.md", FilePayload("你好\n".encode())),
        ("project-memory:demo/notes/note.md", FilePayload(b"note\n")),
        ("config:feature.flag", TomlPayload(("feature", "flag"), "value = true\n")),
        ("project:demo", MemberPayload()),
        (
            "skill:example",
            TreePayload(
                (
                    TreeEntry("SKILL.md", b"# Example\n"),
                    TreeEntry("run.sh", b"echo test\n", True),
                )
            ),
        ),
    ],
)
def test_four_container_types_round_trip_and_reuse_ciphertext(identity, payload):
    vault = crypto.EncryptedVault(VAULT, VRK)
    logical = resource(identity, payload)
    encrypted = vault.encrypt(logical, payload, SYNC)
    assert vault.decrypt(*encrypted, SYNC, crypto.collection_for_id(identity)) == (
        logical,
        payload,
    )
    assert vault.encrypt(logical, payload, SYNC, existing=encrypted) is encrypted
    assert vault.encrypt(logical, payload, SYNC)[2] != encrypted[2]


@pytest.mark.parametrize(
    "field,value",
    [
        ("fingerprint", "1" * 64),
        ("mode_fingerprint", "1" * 64),
        ("references", ("e1:" + "0" * 32 + ":" + "0" * 64,)),
    ],
)
def test_descriptor_changes_fail_authentication(field, value):
    vault = crypto.EncryptedVault(VAULT, VRK)
    payload = FilePayload(b"note\n")
    identity, descriptor, outer = vault.encrypt(
        resource("project-memory:demo/notes/note.md", payload), payload, SYNC
    )
    with pytest.raises(crypto.EncryptedRemoteError):
        vault.decrypt(
            identity,
            replace(descriptor, **{field: value}),
            outer,
            SYNC,
            "project/demo/memory",
        )


@pytest.mark.parametrize("change", ["sync", "vault", "key", "collection", "id"])
def test_wrong_context_is_rejected(change):
    vault = crypto.EncryptedVault(VAULT, VRK)
    payload = FilePayload(b"note\n")
    identity, descriptor, outer = vault.encrypt(
        resource("project-memory:demo/notes/note.md", payload), payload, SYNC
    )
    if change == "vault":
        vault = crypto.EncryptedVault("1" * 32, VRK)
    if change == "key":
        vault = crypto.EncryptedVault(VAULT, bytes(32))
    if change == "id":
        identity = identity[:-1] + ("0" if identity[-1] != "0" else "1")
    with pytest.raises(crypto.EncryptedRemoteError):
        vault.decrypt(
            identity,
            descriptor,
            outer,
            "other" if change == "sync" else SYNC,
            "project/other/memory" if change == "collection" else "project/demo/memory",
        )


@pytest.mark.parametrize(
    "change", ["nonce", "salt", "ciphertext", "extra", "noncanonical"]
)
def test_envelope_tampering_is_rejected_even_with_updated_transport_hash(change):
    vault = crypto.EncryptedVault(VAULT, VRK)
    payload = FilePayload(b"note\n")
    identity, descriptor, outer = vault.encrypt(
        resource("memory:notes/note.md", payload), payload, SYNC
    )
    envelope = json.loads(outer.data)
    if change in {"nonce", "salt", "ciphertext"}:
        value = bytearray(base64.b64decode(envelope[change]))
        value[0] ^= 1
        envelope[change] = base64.b64encode(value).decode()
    elif change == "extra":
        envelope["extra"] = "ignored"
    outer = FilePayload(
        _canonical(envelope) + (b" " if change == "noncanonical" else b"")
    )
    descriptor = replace(
        descriptor, content_hash=payload_hash(outer), size=len(encode_payload(outer))
    )
    with pytest.raises(crypto.EncryptedRemoteError):
        vault.decrypt(identity, descriptor, outer, SYNC, "private")


def test_unknown_envelope_version_is_not_downgraded():
    vault = crypto.EncryptedVault(VAULT, VRK)
    payload = FilePayload(b"note\n")
    identity, descriptor, outer = vault.encrypt(
        resource("memory:notes/note.md", payload), payload, SYNC
    )
    envelope = json.loads(outer.data)
    envelope["version"] = 2
    outer = FilePayload(_canonical(envelope))
    descriptor = replace(
        descriptor, content_hash=payload_hash(outer), size=len(encode_payload(outer))
    )
    with pytest.raises(EncryptedStoreUnsupported):
        vault.decrypt(identity, descriptor, outer, SYNC, "private")


def test_recovery_header_matches_frozen_mac_and_wrapped_key(monkeypatch):
    encoded = header(monkeypatch)
    metadata = json.loads(encoded)
    assert metadata["metadata_mac"] == VECTOR["recovery"]["metadata_mac"]
    assert (
        hashlib.sha256(base64.b64decode(metadata["recovery"]["ciphertext"])).hexdigest()
        == VECTOR["recovery"]["wrapped_sha256"]
    )
    vault = crypto.recover_vault(
        encoded, SYNC, bytes.fromhex(INPUT["recovery_code_hex"])
    )
    assert vault.vrk == VRK and vault.vault_id == VAULT
    descriptor, outer = crypto.metadata_resource(encoded)
    assert descriptor.fingerprint == hashlib.sha256(encoded).hexdigest()
    assert descriptor.content_hash == payload_hash(outer)


@pytest.mark.parametrize(
    "change", ["code", "sync", "mac", "vault", "salt", "duplicate", "bool-version"]
)
def test_recovery_failures_are_uniform_and_secret_safe(monkeypatch, change):
    encoded = header(monkeypatch)
    code = bytes.fromhex(INPUT["recovery_code_hex"])
    sync = SYNC
    metadata = json.loads(encoded)
    if change == "code":
        code = bytes(32)
    if change == "sync":
        sync = "other"
    if change == "mac":
        metadata["metadata_mac"] = "0" * 64
    if change == "vault":
        metadata["vault_id"] = "1" * 32
    if change == "salt":
        metadata["recovery"]["salt"] = base64.b64encode(bytes(32)).decode()
    if change == "bool-version":
        metadata["version"] = True
    encoded = _canonical(metadata)
    if change == "duplicate":
        encoded = encoded[:-1] + b',"version":1}'
    with pytest.raises(crypto.RecoveryKeyInvalid) as caught:
        crypto.recover_vault(encoded, sync, code)
    assert VRK.hex() not in str(caught.value) and code.hex() not in str(caught.value)


def test_regeneration_does_not_revoke_old_code_and_old_header(monkeypatch):
    encoded = header(monkeypatch)
    monkeypatch.undo()
    new_code = bytes([42]) * 32
    replaced = crypto.replace_recovery_code(encoded, VRK, SYNC, new_code)
    assert crypto.recover_vault(replaced, SYNC, new_code).vrk == VRK
    assert (
        crypto.recover_vault(
            encoded, SYNC, bytes.fromhex(INPUT["recovery_code_hex"])
        ).vrk
        == VRK
    )
    with pytest.raises(crypto.RecoveryKeyInvalid):
        crypto.recover_vault(replaced, SYNC, bytes.fromhex(INPUT["recovery_code_hex"]))


def test_all_known_kinds_have_closed_collection_assignment():
    for kind in SYNC_KINDS:
        identity = f"{kind}:demo/notes/note.md"
        assert crypto.collection_for_id(identity) == (
            "project/demo/memory" if kind == "project-memory" else "private"
        )
    assert crypto.collection_for_id("future:resource") == "private"


def test_credentials_round_trip_only_inside_ciphertext():
    payload = FilePayload(
        b'api_key = "test-only-abcdefghijklmnopqrstuvwxyz1234567890"\n'
    )
    vault = crypto.EncryptedVault(VAULT, VRK)
    logical = resource("memory:notes/credential.md", payload)
    identity, descriptor, encrypted = vault.encrypt(logical, payload, SYNC)
    assert payload.data not in encode_payload(encrypted)
    assert vault.decrypt(identity, descriptor, encrypted, SYNC, "private") == (
        logical,
        payload,
    )


def test_encoded_resource_budget_is_enforced(monkeypatch):
    monkeypatch.setattr(crypto.remote_limits, "MAX_RESOURCE_PAYLOAD_BYTES", 100)
    payload = FilePayload(b"note\n")
    with pytest.raises(crypto.EncryptedRemoteError):
        crypto.EncryptedVault(VAULT, VRK).encrypt(
            resource("memory:notes/note.md", payload), payload, SYNC
        )


@pytest.mark.parametrize(
    "change",
    [
        "payload",
        "parts",
        "kind",
        "unknown-id",
        "mode",
        "references",
        "boolean-schema",
        "noncanonical",
        "duplicate",
    ],
)
def test_authenticated_but_invalid_logical_content_is_refused(change):
    # A valid AEAD tag does not authorize paths, credentials or resource semantics.
    inner = json.loads(bytes.fromhex(VECTOR["plaintext_hex"]))
    if change == "payload":
        inner["payload"] = base64.b64encode(
            encode_payload(FilePayload(b"different content\n"))
        ).decode()
    elif change == "parts":
        inner["logical_parts"][0]["path"] = "../../outside.md"
    elif change == "kind":
        inner["resource_type"] = "inbox"
    elif change == "unknown-id":
        inner["logical_id"] = "future:resource"
    elif change == "mode":
        inner["mode_fingerprint"] = "0" * 64
    elif change == "references":
        inner["references"] = ["project:other"]
    elif change == "boolean-schema":
        inner["schema_version"] = True
    plaintext = _canonical(inner)
    if change == "noncanonical":
        plaintext += b" "
    elif change == "duplicate":
        plaintext = plaintext[:-1] + b',"schema_version":1}'
    keys = crypto.derive_collection(VRK, VAULT, "project/demo/memory")
    salt = bytes.fromhex(INPUT["content_salt_hex"])
    dek = crypto._hkdf(
        keys.content, salt, {"domain": "aikito.content-aead", "version": 1}
    )
    aes, _, _, _ = crypto._crypto()
    ciphertext = aes(dek).encrypt(
        bytes.fromhex(INPUT["content_nonce_hex"]),
        plaintext,
        bytes.fromhex(VECTOR["aad_hex"]),
    )
    envelope = {
        **VECTOR["envelope"],
        "ciphertext": base64.b64encode(ciphertext).decode(),
    }
    outer = FilePayload(_canonical(envelope))
    descriptor = ResourceDescriptor(
        VECTOR["aad"]["fingerprint"],
        payload_hash(outer),
        len(encode_payload(outer)),
        tuple(VECTOR["aad"]["references"]),
        None,
    )
    with pytest.raises(crypto.EncryptedRemoteError):
        crypto.EncryptedVault(VAULT, VRK).decrypt(
            VECTOR["aad"]["resource_id"], descriptor, outer, SYNC, "project/demo/memory"
        )


@pytest.mark.parametrize(
    "change", ["hash", "size", "over-budget", "noncanonical-base64"]
)
def test_transport_and_base64_validation_fail_before_acceptance(change, monkeypatch):
    vault = crypto.EncryptedVault(VAULT, VRK)
    outer = FilePayload(_canonical(VECTOR["envelope"]))
    descriptor = ResourceDescriptor(
        VECTOR["aad"]["fingerprint"],
        payload_hash(outer),
        len(encode_payload(outer)),
        tuple(VECTOR["aad"]["references"]),
        None,
    )
    if change == "hash":
        descriptor = replace(descriptor, content_hash="0" * 64)
    elif change == "size":
        descriptor = replace(descriptor, size=descriptor.size + 1)
    elif change == "over-budget":
        monkeypatch.setattr(
            crypto.remote_limits, "MAX_RESOURCE_PAYLOAD_BYTES", descriptor.size - 1
        )
    else:
        envelope = {**VECTOR["envelope"], "salt": VECTOR["envelope"]["salt"] + "="}
        outer = FilePayload(_canonical(envelope))
        descriptor = replace(
            descriptor,
            content_hash=payload_hash(outer),
            size=len(encode_payload(outer)),
        )
    with pytest.raises(crypto.EncryptedRemoteError):
        vault.decrypt(
            VECTOR["aad"]["resource_id"], descriptor, outer, SYNC, "project/demo/memory"
        )


def test_changed_content_never_reuses_verified_old_ciphertext():
    vault = crypto.EncryptedVault(VAULT, VRK)
    old_payload = FilePayload(b"old\n")
    new_payload = FilePayload(b"new\n")
    old = vault.encrypt(
        resource("memory:notes/note.md", old_payload), old_payload, SYNC
    )
    new = vault.encrypt(
        resource("memory:notes/note.md", new_payload), new_payload, SYNC, existing=old
    )
    assert new[0] == old[0] and new[1].fingerprint != old[1].fingerprint
    assert vault.decrypt(*new, SYNC, "private")[1] == new_payload


def test_unsupported_metadata_is_rejected_even_with_a_valid_mac():
    from workspace_encrypted_setup import unsupported_header

    code = bytes.fromhex(INPUT["recovery_code_hex"])
    encoded = unsupported_header(VRK, VAULT, SYNC, code)
    with pytest.raises(crypto.EncryptedRemoteError):
        crypto.verify_metadata(encoded, VRK)
    with pytest.raises(crypto.RecoveryKeyInvalid):
        crypto.recover_vault(encoded, SYNC, code)
    with pytest.raises(crypto.RecoveryKeyInvalid):
        crypto.recover_vault(b" " * (crypto.MAX_METADATA_BYTES + 1), SYNC, code)
