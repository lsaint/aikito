"""Discovery authenticates data before names, graphs or resumable maps are used."""

import hashlib
import hmac
import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from workspace_encrypted_setup import unchecked_header_resource, unsupported_header

from aikito.workspace import encrypted_discovery as discovery
from aikito.workspace.encrypted_remote import (
    EncryptedRemoteError,
    EncryptedVault,
    create_recovery_metadata,
    metadata_resource,
)
from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    encode_payload,
    inspect_payload,
    payload_hash,
)
from aikito.workspace.remote_factory import BoundRemoteReader
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_store import (
    RemoteSnapshot,
    SnapshotExpired,
    StoreIdentityMismatch,
)
from aikito.workspace.remote_wire import _canonical
from aikito.workspace.resource_state import local_resource_for_id

pytest.importorskip("cryptography")
VAULT = "00112233445566778899aabbccddeeff"
KEY = bytes(range(32))
SYNC = "discovery-test-sync"
KEYS = SimpleNamespace(load=lambda vault: KEY)


class Reader:
    def __init__(self, contents=(), *, format="encrypted-v1"):
        self.calls = []
        self.payloads = {}
        self.descriptors = {}
        vault = EncryptedVault(VAULT, KEY)
        for logical_id, payload in contents:
            resource = local_resource_for_id(
                logical_id, "" if isinstance(payload, MemberPayload) else "0" * 64
            )
            fingerprint, references = inspect_payload(resource, payload)
            resource = replace(resource, fingerprint=fingerprint, references=references)
            identity, descriptor, outer = vault.encrypt(resource, payload, SYNC)
            self.descriptors[identity] = descriptor
            self.payloads[identity] = outer
        raw = (
            create_recovery_metadata(KEY, VAULT, SYNC, bytes([42]) * 32)
            if format == "encrypted-v1"
            else unsupported_header(KEY, VAULT, SYNC, bytes([42]) * 32)
        )
        descriptor, payload = unchecked_header_resource(raw)
        self.descriptors[FORMAT_RESOURCE_ID] = descriptor
        self.payloads[FORMAT_RESOURCE_ID] = payload
        self.revision = 1

    def read(self):
        return RemoteSnapshot(SYNC, self.revision, self.descriptors)

    def fetch(self, expected, ids):
        assert expected == self.read()
        self.calls.append(tuple(ids))
        return {k: self.payloads[k] for k in ids}


@pytest.fixture
def reader():
    return Reader(
        [
            ("project:demo", MemberPayload()),
            ("project:other", MemberPayload()),
            ("global-memory:notes/private.md", FilePayload(b"private content\n")),
            ("project-memory:demo/notes/shared.md", FilePayload("你好\n".encode())),
            ("project-memory:other/notes/shared.md", FilePayload(b"other\n")),
        ]
    )


def test_bootstrap_is_bounded_resumable_and_private_first(reader):
    checkpoint = None
    rounds = []
    for _ in range(5):
        result = discovery.discover_encrypted(
            reader, KEYS, checkpoint=checkpoint, maximum=1
        )
        rounds.append(result)
        checkpoint = result.checkpoint
    assert [r.remaining for r in rounds] == [4, 3, 2, 1, 0]
    assert all(r.fetched == 1 for r in rounds)
    assert all(not r.complete for r in rounds[:-1]) and result.complete
    assert set(result.resources) == {
        "project:demo",
        "project:other",
        "global-memory:notes/private.md",
        "project-memory:demo/notes/shared.md",
        "project-memory:other/notes/shared.md",
    }
    assert all(
        not any(r.kind == "project-memory" for r in view.resources.values())
        for view in rounds[:2]
    )
    assert b"private content" not in checkpoint and KEY.hex().encode() not in checkpoint
    with pytest.raises(TypeError):
        result.resources["invalid"] = None
    before = len(reader.calls)
    same = discovery.discover_encrypted(reader, KEYS, checkpoint=checkpoint)
    assert same.fetched == 0 and same.complete
    assert reader.calls[before:] == [(FORMAT_RESOURCE_ID,)]


@pytest.mark.parametrize(
    "change", ["mac", "resource", "sync", "vault", "duplicate", "noncanonical"]
)
def test_tampered_checkpoint_never_authorizes_cached_names(reader, change):
    result = discovery.discover_encrypted(reader, KEYS)
    raw = json.loads(result.checkpoint)
    if change == "mac":
        raw["mac"] = "0" * 64
    elif change == "resource":
        raw["entries"][next(iter(raw["entries"]))]["resource"] = {
            "project:attacker": ""
        }
    elif change in {"sync", "vault"}:
        raw["sync_id" if change == "sync" else "vault_id"] = "0" * 32
    encoded = _canonical(raw)
    if change == "duplicate":
        encoded = encoded[:-1] + b',"version":1}'
    if change == "noncanonical":
        encoded += b" "
    before = len(reader.calls)
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS, checkpoint=encoded)
    assert reader.calls[before:] == [(FORMAT_RESOURCE_ID,)]


def test_unchanged_descriptors_survive_revision_changes_but_updates_are_revalidated():
    reader = Reader([("global-memory:notes/note.md", FilePayload(b"old\n"))])
    result = discovery.discover_encrypted(reader, KEYS)
    changed = Reader([("global-memory:notes/note.md", FilePayload(b"new\n"))])
    changed.revision = 2
    new = discovery.discover_encrypted(changed, KEYS, checkpoint=result.checkpoint)
    assert new.fetched == 1 and new.complete
    assert (
        new.resources["global-memory:notes/note.md"].fingerprint
        != result.resources["global-memory:notes/note.md"].fingerprint
    )
    del changed.descriptors[
        next(k for k in changed.descriptors if k != FORMAT_RESOURCE_ID)
    ]
    empty = discovery.discover_encrypted(changed, KEYS, checkpoint=new.checkpoint)
    assert empty.complete and not empty.resources


def test_unknown_collection_waits_for_private_then_fails_closed():
    reader = Reader(
        [
            ("project:demo", MemberPayload()),
            ("global-memory:notes/private.md", FilePayload(b"note\n")),
        ]
    )
    identity = EncryptedVault(VAULT, KEY).opaque_id("global-memory:notes/private.md")
    descriptor = reader.descriptors.pop(identity)
    unknown = "e1:" + "0" * 32 + ":" + "1" * 64
    reader.descriptors[unknown] = descriptor
    first = discovery.discover_encrypted(reader, KEYS)
    assert not first.complete and first.remaining == 1
    before = len(reader.calls)
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS, checkpoint=first.checkpoint)
    assert reader.calls[before:] == [(FORMAT_RESOURCE_ID,)]


def test_orphan_descriptor_references_are_rejected_before_business_fetch(reader):
    parent = EncryptedVault(VAULT, KEY).opaque_id("project:demo")
    del reader.descriptors[parent]
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_missing_key_does_not_fetch_business(reader):
    with pytest.raises(discovery.EncryptedStoreLocked):
        discovery.discover_encrypted(reader, SimpleNamespace(load=lambda vault: None))
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_migrating_mixed_store_pauses_before_business_fetch():
    reader = Reader(format="migrating")
    reader.descriptors["global-memory:notes/plain.md"] = reader.descriptors[
        FORMAT_RESOURCE_ID
    ]
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_wrong_device_key_and_header_mac_are_rejected(reader):
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(
            reader, SimpleNamespace(load=lambda vault: bytes(32))
        )
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_format_header_cap_is_checked_before_fetch(reader):
    reader.descriptors[FORMAT_RESOURCE_ID] = replace(
        reader.descriptors[FORMAT_RESOURCE_ID], size=1000000
    )
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == []


def test_fetch_budget_can_block_without_unbounded_downloads(reader, monkeypatch):
    # A large business descriptor fits the manifest but cannot fit a round.
    key = EncryptedVault(VAULT, KEY).opaque_id("global-memory:notes/private.md")
    reader = Reader([("global-memory:notes/private.md", FilePayload(b"note\n"))])
    reader.descriptors[key] = replace(reader.descriptors[key], size=500000)
    monkeypatch.setattr(discovery.remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 100000)
    with pytest.raises(discovery.DiscoveryBudgetExceeded):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


def test_oversized_request_manifest_cannot_trigger_fetch(reader, monkeypatch):
    monkeypatch.setattr(discovery.remote_limits, "MAX_REMOTE_REQUEST_BYTES", 100)
    with pytest.raises(ValueError):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == []


def test_incomplete_fetch_and_tampered_ciphertext_are_refused(reader, monkeypatch):
    real_fetch = reader.fetch

    def incomplete(expected, ids):
        payloads = real_fetch(expected, ids)
        return payloads if FORMAT_RESOURCE_ID in ids else {}

    monkeypatch.setattr(reader, "fetch", incomplete)
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)


def test_identity_pin_precedes_header_fetch(reader):
    pin = BoundRemoteReader(reader, "other-store")
    with pytest.raises(StoreIdentityMismatch):
        discovery.discover_encrypted(pin, KEYS)
    assert reader.calls == []
    assert not hasattr(pin, "commit") and not hasattr(pin, "resolve_commit")


def test_snapshot_expiry_is_propagated_without_retrying_or_writing(reader, monkeypatch):
    monkeypatch.setattr(
        reader, "fetch", lambda *args: (_ for _ in ()).throw(SnapshotExpired("changed"))
    )
    with pytest.raises(SnapshotExpired):
        discovery.discover_encrypted(reader, KEYS)


def test_modified_ciphertext_is_refused_even_with_valid_transport_hash():
    reader = Reader([("global-memory:notes/private.md", FilePayload(b"note\n"))])
    identity = EncryptedVault(VAULT, KEY).opaque_id("global-memory:notes/private.md")
    envelope = json.loads(reader.payloads[identity].data)
    envelope["ciphertext"] = "AAAA"
    outer = FilePayload(_canonical(envelope))
    reader.payloads[identity] = outer
    reader.descriptors[identity] = replace(
        reader.descriptors[identity],
        content_hash=payload_hash(outer),
        size=len(encode_payload(outer)),
    )
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)


def test_invalid_metadata_authentication_is_refused_before_business_fetch(reader):
    raw = json.loads(reader.payloads[FORMAT_RESOURCE_ID].data)
    raw["metadata_mac"] = "0" * 64
    descriptor, payload = metadata_resource(_canonical(raw))
    reader.descriptors[FORMAT_RESOURCE_ID] = descriptor
    reader.payloads[FORMAT_RESOURCE_ID] = payload
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS)
    assert reader.calls == [(FORMAT_RESOURCE_ID,)]


@pytest.mark.parametrize("version", [1, 2])
def test_authenticated_old_checkpoint_is_rejected(version):
    reader = Reader([("global-memory:notes/note.md", FilePayload(b"content\n"))])
    result = discovery.discover_encrypted(reader, KEYS)
    raw = json.loads(result.checkpoint)
    raw["version"] = version
    for entry in raw["entries"].values():
        entry.pop("plaintext")
    body = {key: value for key, value in raw.items() if key != "mac"}
    raw["mac"] = hmac.new(
        discovery._checkpoint_key(EncryptedVault(VAULT, KEY)),
        _canonical(body),
        hashlib.sha256,
    ).hexdigest()
    before = len(reader.calls)
    with pytest.raises(EncryptedRemoteError):
        discovery.discover_encrypted(reader, KEYS, checkpoint=_canonical(raw))
    assert reader.calls[before:] == [(FORMAT_RESOURCE_ID,)]


@pytest.mark.parametrize(
    "identity, content",
    [
        ("mcp:docs", b'agents = ["custom"]\ncommand = "docs"\n'),
        (
            "subagent:review",
            b'---\ndescription: "Review"\nagents: ["custom"]\n---\nReview\n',
        ),
    ],
)
def test_dynamic_agent_references_survive_discovery_checkpoint_rounds(
    identity, content
):
    reader = Reader(
        [
            ("agent:custom", FilePayload(b'[agents.custom]\nvalue = "portable"\n')),
            (identity, FilePayload(content)),
        ]
    )
    checkpoint = None
    for _ in range(2):
        result = discovery.discover_encrypted(
            reader, KEYS, checkpoint=checkpoint, maximum=1
        )
        checkpoint = result.checkpoint
    assert result.complete
    assert result.resources[identity].references == ("agent:custom",)
    resumed = discovery.discover_encrypted(
        reader, KEYS, checkpoint=checkpoint, maximum=1
    )
    assert resumed.complete and resumed.fetched == 0
    assert resumed.resources[identity].references == ("agent:custom",)
