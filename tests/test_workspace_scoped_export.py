"""Read export isolation, authenticated request binding and safe local publication."""

import json
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest
from test_workspace_encrypted_discovery import Reader, KEY, VAULT, SYNC, KEYS

from aikito.workspace import scoped_export as scoped
from aikito.workspace.encrypted_remote import EncryptedRemoteError, EncryptedVault, _b64
from aikito.workspace.payload import FilePayload, MemberPayload
from aikito.workspace.remote_wire import _canonical
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID

pytest.importorskip("cryptography.hazmat.primitives.hpke")
NOW = 2000000000
COLLECTION = "project/demo/memory"
VECTOR = Path(__file__).parent / "fixtures/scoped_export/scoped-read-v1.json"


def inputs():
    _, _, PrivateKey, _ = scoped._hpke()
    private = PrivateKey.from_private_bytes(bytes([31]) * 32)
    recipient = {
        "version": 1,
        "algorithm": scoped.EXPORT_SUITE,
        "key_id": "reader-1",
        "public_key": _b64(private.public_key().public_bytes_raw()),
    }
    authorization = {
        "version": 1,
        "account_id": "account-1",
        "store_id": "store-1",
        "sync_id": SYNC,
        "vault_id": VAULT,
        "collection_id": COLLECTION,
        "recipient_key_id": "reader-1",
        "permissions": ["read"],
        "grant_id": "grant-1",
        "nonce": _b64(bytes([37]) * 32),
        "expires_at": NOW + 3600,
    }
    return bytes([31]) * 32, recipient, authorization


@pytest.fixture
def setup():
    reader = Reader(
        [
            ("project:demo", MemberPayload()),
            ("project:other", MemberPayload()),
            ("memory:notes/private.md", FilePayload(b"global private payload\n")),
            (
                "project-memory:demo/notes/shared.md",
                FilePayload(b"shared demo payload\n"),
            ),
            (
                "project-memory:other/notes/shared.md",
                FilePayload(b"other project payload\n"),
            ),
        ]
    )
    private, recipient, auth = inputs()
    return reader, private, recipient, auth


def export(setup):
    reader, _, recipient, auth = setup
    return scoped.export_scoped_read(
        reader,
        KEYS,
        collection_id=COLLECTION,
        recipient=_canonical(recipient),
        authorization=_canonical(auth),
        now=NOW,
    )


def test_single_scope_key_has_no_other_material_or_enumeration_key(setup):
    reader, private, _, auth = setup
    before = reader.read()
    encoded = export(setup)
    opened = scoped.open_scoped_read(
        encoded, private, authorization=_canonical(auth), now=NOW
    )
    vault = EncryptedVault(VAULT, KEY)
    assert opened.content_key == vault.collection(COLLECTION).content
    assert opened.context["parent_project_id"] == vault.opaque_id("project:demo")
    assert not any(
        hasattr(opened, name)
        for name in ("vrk", "resource_id", "fingerprint", "tag_key")
    )
    assert opened.content_key.hex() not in repr(opened)
    assert COLLECTION.encode() not in encoded and KEY not in encoded
    identity = vault.opaque_id("project-memory:demo/notes/shared.md")
    resource, payload = opened.decrypt(
        identity, reader.descriptors[identity], reader.payloads[identity], now=NOW
    )
    assert (
        resource.id == "project-memory:demo/notes/shared.md"
        and payload.data == b"shared demo payload\n"
    )
    assert reader.read() == before
    assert reader.calls == [
        (FORMAT_RESOURCE_ID,),
        (vault.opaque_id("project:demo"), identity),
    ]
    assert encoded != export(setup), "Every export must use fresh HPKE randomness"


@pytest.mark.parametrize(
    "logical",
    ["memory:notes/private.md", "project:demo", "project-memory:other/notes/shared.md"],
)
def test_receiver_cannot_read_private_parent_global_or_another_project(setup, logical):
    reader, private, _, auth = setup
    opened = scoped.open_scoped_read(
        export(setup), private, authorization=_canonical(auth), now=NOW
    )
    identity = EncryptedVault(VAULT, KEY).opaque_id(logical)
    with pytest.raises(EncryptedRemoteError):
        opened.decrypt(
            identity, reader.descriptors[identity], reader.payloads[identity], now=NOW
        )


@pytest.mark.parametrize(
    "collection",
    [
        "private",
        "memory",
        "project/demo",
        "project//memory",
        "project/demo/skills",
        "project/../memory",
    ],
)
def test_non_project_memory_export_fails_before_remote_reads(setup, collection):
    _, _, recipient, auth = setup
    reader = Mock(side_effect=AssertionError("No unauthorized remote read"))
    with pytest.raises((EncryptedRemoteError, ValueError)):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=collection,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )
    reader.read.assert_not_called()


@pytest.mark.parametrize(
    "permissions",
    [[], ["memory.write"], ["read", "memory.write"], ["read", "read"], "read"],
)
def test_write_and_unknown_permissions_fail_before_remote(setup, permissions):
    _, _, recipient, auth = setup
    auth["permissions"] = permissions
    reader = Mock()
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=COLLECTION,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )
    reader.read.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("expires_at", NOW),
        ("version", True),
        ("nonce", "bad"),
        ("account_id", "bad\naccount"),
        ("recipient_key_id", "reader-2"),
        ("collection_id", "project/other/memory"),
    ],
)
def test_malformed_expired_or_mismatched_authorization_is_rejected(setup, field, value):
    reader, _, recipient, auth = setup
    auth[field] = value
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=COLLECTION,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )
    assert reader.calls == []


@pytest.mark.parametrize(
    "field",
    [
        "account_id",
        "store_id",
        "sync_id",
        "vault_id",
        "grant_id",
        "nonce",
        "expires_at",
        "recipient_key_id",
        "collection_id",
    ],
)
def test_receiver_binds_every_separately_trusted_request_field(setup, field):
    _, private, _, auth = setup
    encoded = export(setup)
    auth[field] = (
        auth[field] + 1
        if type(auth[field]) is int
        else ("project/other/memory" if field == "collection_id" else "changed")
    )
    with pytest.raises((EncryptedRemoteError, ValueError)):
        scoped.open_scoped_read(
            encoded, private, authorization=_canonical(auth), now=NOW
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("collection_tag", "f" * 32),
        ("parent_project_id", "e1:" + "f" * 32 + ":" + "a" * 64),
        ("algorithm", "unknown"),
        ("key_version", 2),
        ("recipient_public_key_sha256", "0" * 64),
    ],
)
def test_context_tampering_and_wrong_recipient_fail(setup, field, value):
    _, private, _, auth = setup
    raw = json.loads(export(setup))
    raw["context"][field] = value
    with pytest.raises(EncryptedRemoteError):
        scoped.open_scoped_read(
            _canonical(raw), private, authorization=_canonical(auth), now=NOW
        )
    with pytest.raises(EncryptedRemoteError):
        scoped.open_scoped_read(
            export(setup), bytes([1]) * 32, authorization=_canonical(auth), now=NOW
        )


def test_expiry_is_enforced_on_every_read_and_descriptor_changes_fail(setup):
    reader, private, _, auth = setup
    opened = scoped.open_scoped_read(
        export(setup), private, authorization=_canonical(auth), now=NOW
    )
    identity = EncryptedVault(VAULT, KEY).opaque_id(
        "project-memory:demo/notes/shared.md"
    )
    descriptor, payload = reader.descriptors[identity], reader.payloads[identity]
    with pytest.raises(EncryptedRemoteError):
        opened.decrypt(identity, descriptor, payload, now=auth["expires_at"])
    with pytest.raises(EncryptedRemoteError):
        opened.decrypt(
            identity, replace(descriptor, fingerprint="0" * 64), payload, now=NOW
        )


def test_empty_collection_missing_or_tampered_parent_is_not_exported(setup):
    reader, _, _, _ = setup
    vault = EncryptedVault(VAULT, KEY)
    parent = vault.opaque_id("project:demo")
    reader.descriptors.pop(parent)
    with pytest.raises(EncryptedRemoteError):
        export(setup)
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_migrating_header_cannot_authorize_export(setup):
    _, _, recipient, auth = setup
    reader = Reader([], format="migrating")
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=COLLECTION,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )


def test_output_never_overwrites_existing_files_or_leaves_partial_output(
    tmp_path, monkeypatch
):
    output = tmp_path / "export.json"
    scoped._publish(output, b"sealed package")
    assert output.read_bytes() == b"sealed package"
    with pytest.raises(FileExistsError):
        scoped._publish(output, b"replacement")
    assert output.read_bytes() == b"sealed package"
    monkeypatch.setattr(scoped, "secure_file_permissions", lambda path: False)
    target = tmp_path / "denied.json"
    with pytest.raises(EncryptedRemoteError):
        scoped._publish(target, b"never published")
    assert not target.exists() and not list(tmp_path.glob(".scoped-export-*"))


def test_frozen_python_recipient_opens_cross_language_vector():
    vector = json.loads(VECTOR.read_bytes())
    assert vector["test_only"] is True
    opened = scoped.open_scoped_read(
        vector["package"].encode(),
        bytes.fromhex(vector["recipient_private_hex"]),
        authorization=vector["authorization"].encode(),
        now=vector["now"],
    )
    assert opened.content_key.hex() == vector["content_key_hex"]
    assert opened.collection_id == "project/demo/memory"


def test_explicit_write_exports_only_three_collection_subkeys(setup):
    reader, private, recipient, auth = setup
    auth["permissions"] = ["read", "write"]
    encoded = scoped.export_scoped_write(
        reader,
        KEYS,
        collection_id=COLLECTION,
        recipient=_canonical(recipient),
        authorization=_canonical(auth),
        now=NOW,
    )
    packet = json.loads(encoded)
    suite, _, PrivateKey, _ = scoped._hpke()
    body = json.loads(
        suite.decrypt(
            scoped._unb64(packet["ciphertext"]),
            PrivateKey.from_private_bytes(private),
            info=scoped._info(packet["context"], "write"),
        )
    )
    keys = EncryptedVault(VAULT, KEY).collection(COLLECTION)
    assert body == {
        "version": 1,
        "collection_id": COLLECTION,
        "content_key": _b64(keys.content),
        "resource_id_key": _b64(keys.resource_id),
        "fingerprint_key": _b64(keys.fingerprint),
    }
    with pytest.raises(EncryptedRemoteError):
        scoped.open_scoped_read(
            encoded, private, authorization=_canonical(auth), now=NOW
        )
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=COLLECTION,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )


@pytest.mark.parametrize(
    "permissions",
    [
        ["read"],
        ["write"],
        ["memory.write"],
        ["write", "read"],
        ["read", "write", "write"],
    ],
)
def test_write_export_requires_exact_independent_permission(setup, permissions):
    reader, _, recipient, auth = setup
    auth["permissions"] = permissions
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_write(
            reader,
            KEYS,
            collection_id=COLLECTION,
            recipient=_canonical(recipient),
            authorization=_canonical(auth),
            now=NOW,
        )
    assert not reader.calls


def test_frozen_write_export_native_receiver():
    path = (
        Path(__file__).parent / "fixtures/remote_protocol_vectors/scoped-write-v1.json"
    )
    vector = json.loads(path.read_bytes())
    assert vector["test_only"] is True
    auth, packet = json.loads(vector["authorization"]), json.loads(vector["package"])
    assert auth["permissions"] == ["read", "write"]
    suite, _, PrivateKey, _ = scoped._hpke()
    body = json.loads(
        suite.decrypt(
            scoped._unb64(packet["ciphertext"]),
            PrivateKey.from_private_bytes(
                bytes.fromhex(vector["recipient_private_hex"])
            ),
            info=scoped._info(packet["context"], "write"),
        )
    )
    assert set(body) == {
        "version",
        "collection_id",
        "content_key",
        "resource_id_key",
        "fingerprint_key",
    }
    for key in ("content", "resource_id", "fingerprint"):
        assert scoped._unb64(body[key + "_key"]).hex() == vector[key + "_key_hex"]

    keys = EncryptedVault(VAULT, KEY).collection(COLLECTION)
    assert body["content_key"] == _b64(keys.content)
    assert body["resource_id_key"] == _b64(keys.resource_id)
    assert body["fingerprint_key"] == _b64(keys.fingerprint)
