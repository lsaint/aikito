"""Collection exports validate complete resources without changing live stores."""

import json
from pathlib import Path
from unittest.mock import Mock

import pytest
from workspace_context_setup import (
    COLLECTIONS,
    KEY,
    KEYS,
    NOW,
    PRIVATE,
    VAULT,
    Reader,
    inputs,
)

from aikito.workspace import scoped_export as scoped
from aikito.workspace.encrypted_remote import (
    EncryptedRemoteError,
    EncryptedVault,
    _b64,
    collection_for_id,
)
from aikito.workspace.payload import FilePayload, decode_payload, encode_payload
from aikito.workspace.remote_wire import _canonical, _decode_descriptor
from aikito.workspace.resource_state import decode_resources
from aikito.workspace.transactions import WorkspaceCoreError
from aikito.workspace.resource_collections import (
    collection_for_resource,
    parse_collection,
)

pytest.importorskip("cryptography.hazmat.primitives.hpke")


def export(reader, collection="skills", permission="read"):
    recipient, auth = inputs(collection, permission)
    action = (
        scoped.export_scoped_read
        if permission == "read"
        else scoped.export_scoped_write
    )
    packet = action(
        reader,
        KEYS,
        collection_id=collection,
        recipient=recipient,
        authorization=auth,
        now=NOW,
    )
    return packet, auth


@pytest.mark.parametrize("collection", COLLECTIONS)
@pytest.mark.parametrize("permission", ["read", "write"])
def test_all_resources_roundtrip_and_minimal_key_material(collection, permission):
    reader = Reader()
    before = reader.read()
    encoded, auth = export(reader, collection, permission)
    packet = json.loads(encoded)
    context = packet["context"]
    scoped._validate_context(context, json.loads(auth))
    assert context["collection_id"] == collection and context["version"] == 2
    parent = parse_collection(collection).parent_id
    assert context["parent_project_id"] == (
        reader.vault.opaque_id(parent) if parent else None
    )
    suite, _, PrivateKey, _ = scoped._hpke()
    body = json.loads(
        suite.decrypt(
            scoped._unb64(packet["ciphertext"]),
            PrivateKey.from_private_bytes(PRIVATE),
            info=scoped._info(context, permission),
        )
    )
    keys = reader.vault.collection(collection)
    assert body["content_key"] == _b64(keys.content)
    assert set(body) == (
        {"version", "collection_id", "content_key"}
        if permission == "read"
        else {
            "version",
            "collection_id",
            "content_key",
            "resource_id_key",
            "fingerprint_key",
        }
    )
    if permission == "write":
        assert body["resource_id_key"] == _b64(keys.resource_id)
        assert body["fingerprint_key"] == _b64(keys.fingerprint)
    else:
        opened = scoped.open_scoped_read(encoded, PRIVATE, authorization=auth, now=NOW)
        for opaque, (resource, payload) in reader.original.items():
            if collection_for_resource(resource.id) == collection:
                assert opened.decrypt(
                    opaque, reader.descriptors[opaque], reader.payloads[opaque], now=NOW
                ) == (resource, payload)
            else:
                with pytest.raises(EncryptedRemoteError):
                    opened.decrypt(
                        opaque,
                        reader.descriptors[opaque],
                        reader.payloads[opaque],
                        now=NOW,
                    )
    assert reader.read() == before
    assert KEY.hex().encode() not in encoded
    assert export(reader, collection, permission)[0] != encoded


def test_subagent_bindings_cover_both_resources_not_only_sample():
    reader = Reader()
    packet, _ = export(reader, "subagents")
    bindings = json.loads(packet)["context"]["reference_bindings"]
    assert [b["logical_id"] for b in bindings] == ["agent:claude", "agent:codex"]
    assert {b["resource_id"] for b in bindings} == {
        reader.vault.opaque_id("agent:claude"),
        reader.vault.opaque_id("agent:codex"),
    }
    assert b"[agents." not in packet


@pytest.mark.parametrize(
    "collection",
    [
        "private",
        "agents",
        "workspace/config",
        "workspace/skills",
        "project/demo/config",
        "project//memory",
        "project/../memory",
        "global/*",
        "skills/extra",
    ],
)
def test_private_or_invalid_collection_rejected_before_io(collection):
    recipient, auth = inputs(collection)
    reader = Mock()
    with pytest.raises((EncryptedRemoteError, ValueError)):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id=collection,
            recipient=recipient,
            authorization=auth,
            now=NOW,
        )
    reader.read.assert_not_called()


@pytest.mark.parametrize(
    "identity",
    [
        "memory:notes/old.md",
        "global-instructions:OTHER.md",
        "global-memory:../escape.md",
        "project-memory:demo/../escape.md",
        "skill:../escape",
        "future:resource",
    ],
)
def test_invalid_or_legacy_identity_not_silently_repartitioned(identity):
    with pytest.raises(ValueError):
        collection_for_resource(identity)


@pytest.mark.parametrize(
    "identity",
    [
        "mcp:demo",
        "agent:demo",
        "config:sync.enabled",
        "skill-selection:demo",
        "project:demo",
        "project-field:demo/name",
        "project-path:demo/default",
        "project-skill:demo/demo",
    ],
)
def test_private_kinds_have_no_delegable_collection(identity):
    assert collection_for_resource(identity) == "private"


def test_default_layout_uses_only_current_resource_identities():
    vault = EncryptedVault(VAULT, KEY)
    assert collection_for_id("skill:demo") == "skills"
    assert collection_for_id("global-memory:notes/decision.md") == "global/memory"
    with pytest.raises(EncryptedRemoteError):
        vault.opaque_id("memory:notes/decision.md")
    with pytest.raises(WorkspaceCoreError, match="Unsupported resource ID"):
        decode_resources({"memory:notes/decision.md": "0" * 64})


def test_empty_collection_rejected_but_empty_file_can_prove_key():
    with pytest.raises(scoped.CollectionEmpty, match="collection_empty"):
        export(Reader([]))
    reader = Reader([("global-memory:notes/empty.md", FilePayload(b""))])
    encoded, auth = export(reader, "global/memory")
    opened = scoped.open_scoped_read(encoded, PRIVATE, authorization=auth, now=NOW)
    opaque = reader.vault.opaque_id("global-memory:notes/empty.md")
    assert (
        opened.decrypt(
            opaque, reader.descriptors[opaque], reader.payloads[opaque], now=NOW
        )[1].data
        == b""
    )


def test_wrong_key_and_non_sample_corruption_fail_export():
    reader = Reader()
    tag = reader.vault.collection("global/memory").tag
    ids = sorted(k for k in reader.payloads if k.startswith(f"e1:{tag}:"))
    target = ids[-1]
    reader.payloads[target] = FilePayload(b"corrupt non-sample")
    with pytest.raises(EncryptedRemoteError):
        export(reader, "global/memory")
    recipient, auth = inputs()
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_read(
            Reader(),
            Mock(load=lambda _: bytes(32)),
            collection_id="skills",
            recipient=recipient,
            authorization=auth,
            now=NOW,
        )


def test_removed_or_replaced_private_dependency_never_exports_binding():
    reader = Reader()
    target = reader.vault.opaque_id("agent:codex")
    del reader.descriptors[target]
    with pytest.raises(EncryptedRemoteError):
        export(reader, "subagents")


def test_snapshot_change_before_sealing_rejected():
    reader = Reader()
    fetch = reader.fetch

    def changed(expected, ids):
        result = fetch(expected, ids)
        if any(identity != "format:encrypted-v1" for identity in ids):
            reader.revision += 1
        return result

    reader.fetch = changed
    with pytest.raises(EncryptedRemoteError):
        export(reader)


@pytest.mark.parametrize(
    "field",
    [
        "collection_id",
        "parent_project_id",
        "reference_bindings",
        "version",
        "permissions",
    ],
)
def test_context_tampering_or_version_confusion_never_opens(field):
    encoded, auth = export(Reader())
    packet = json.loads(encoded)
    packet["context"][field] = "tampered"
    with pytest.raises((EncryptedRemoteError, ValueError)):
        scoped.open_scoped_read(
            _canonical(packet), PRIVATE, authorization=auth, now=NOW
        )


def test_full_tree_preserves_binary_executable_and_reference_files():
    reader = Reader()
    encoded, auth = export(reader)
    opened = scoped.open_scoped_read(encoded, PRIVATE, authorization=auth, now=NOW)
    identity = reader.vault.opaque_id("skill:demo")
    _, payload = opened.decrypt(
        identity, reader.descriptors[identity], reader.payloads[identity], now=NOW
    )
    members = {entry.path: entry for entry in payload.entries}
    assert members["assets/blob.bin"].data == bytes(range(256))
    assert members["scripts/run.py"].executable
    assert members["references/guide.md"].data == b"Reference content\n"


def test_frozen_vectors_open_and_decrypt_original_resources():
    vector = json.loads(
        (Path(__file__).parent / "fixtures/scoped_export/context-v2.json").read_bytes()
    )
    assert vector["test_only"] is True
    for case in vector["cases"]:
        auth = _canonical(case["authorization"])
        packet = _canonical(case["packet"])
        if case["permission"] == "read":
            opened = scoped.open_scoped_read(
                packet,
                bytes.fromhex(vector["recipient_private_hex"]),
                authorization=auth,
                now=NOW,
            )
            for sample in case["samples"]:
                descriptor = _decode_descriptor(sample["descriptor"])
                outer_bytes = bytes.fromhex(sample["payload_hex"])
                outer = decode_payload(
                    outer_bytes, sample["descriptor"]["content_hash"]
                )
                resource, payload = opened.decrypt(
                    sample["resource_id"], descriptor, outer, now=NOW
                )
                assert resource.id == sample["logical_id"]
                assert (
                    scoped._b64(encode_payload(payload)) == sample["plaintext_payload"]
                )


def test_resource_budget_fails_before_business_payload_fetch(monkeypatch):
    reader = Reader()
    monkeypatch.setattr(scoped.remote_limits, "MAX_RESOURCE_PAYLOAD_BYTES", 1)
    with pytest.raises(EncryptedRemoteError):
        export(reader)
    assert reader.calls == [("format:encrypted-v1",)]


def test_expired_request_and_implicit_write_never_read_store():
    recipient, auth = inputs("skills", "write")
    reader = Mock()
    with pytest.raises(scoped.ScopedExportDenied):
        scoped.export_scoped_read(
            reader,
            KEYS,
            collection_id="skills",
            recipient=recipient,
            authorization=auth,
            now=NOW,
        )
    with pytest.raises(EncryptedRemoteError):
        scoped.export_scoped_write(
            reader,
            KEYS,
            collection_id="skills",
            recipient=recipient,
            authorization=auth,
            now=NOW + 3600,
        )
    reader.read.assert_not_called()
