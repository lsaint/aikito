"""Commit identity, canonical persistence and historical receipt validation."""

from __future__ import annotations

import base64
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    ResourceDescriptor,
    ResourceMutation,
    TomlPayload,
    TreeEntry,
    TreePayload,
    encode_payload,
    inspect_payload,
    payload_hash,
)
from aikito.workspace.resource_state import local_resource_for_id
from aikito.workspace.remote_store import (
    CommitResult,
    InvalidContent,
    ReceiptCursor,
    RemoteSnapshot,
)
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_wire import (
    build_commit_request,
    build_commit_result,
    committed_snapshot,
    decode_commit_request,
    encode_commit_request,
    validate_commit_request,
    validate_commit_result,
)


def mutation(identity="opaque-resource", payload=None, before=None):
    payload = FilePayload(b"\x00content\r\n") if payload is None else payload
    return ResourceMutation(
        identity,
        before,
        ResourceDescriptor("opaque fingerprint", payload_hash(payload)),
        payload,
    )


def request(*mutations, expected=None, previous_receipt=None):
    return build_commit_request(
        "opaque client",
        "request-one",
        RemoteSnapshot("opaque center", 10, {}) if expected is None else expected,
        mutations or (mutation(),),
        previous_receipt=previous_receipt,
    )


def encode_json(raw):
    return json.dumps(
        raw,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


@pytest.mark.parametrize(
    "payload",
    [
        FilePayload(b"\x00\xff\r\n"),
        TomlPayload(("literal.key",), "value = [2026-09-30, { flag = true }]\n"),
        TreePayload(
            (
                TreeEntry("SKILL.md", b"# Skill\r\n"),
                TreeEntry("run.sh", b"echo hello\n", True),
                TreeEntry("empty"),
            )
        ),
        MemberPayload(),
    ],
)
def test_request_preserves_existing_payload_encoding(payload):
    original = request(mutation(payload=payload))
    encoded = encode_commit_request(original)
    raw = json.loads(encoded)
    assert base64.b64decode(raw["mutations"][0]["payload"]) == encode_payload(payload)
    restored = decode_commit_request(encoded)
    assert restored == original
    assert restored.mutations[0].payload == payload
    assert encode_commit_request(restored) == encoded


def test_deterministic_order_and_defensive_ownership():
    left, right = mutation("a"), mutation("z")
    resources = {"existing-z": left.after, "existing-a": right.after}
    batch = [right, left]
    original = request(*batch, expected=RemoteSnapshot("center", 2, resources))
    reordered = request(
        left,
        right,
        expected=RemoteSnapshot("center", 2, dict(reversed(list(resources.items())))),
    )
    assert encode_commit_request(original) == encode_commit_request(reordered)
    batch.clear()
    resources.clear()
    assert len(original.expected.resources) == len(original.mutations) == 2
    with pytest.raises(TypeError):
        original.expected.resources["new"] = left.after
    with pytest.raises(FrozenInstanceError):
        original.request_id = "another"


def test_constructor_copies_mutation_list():
    original = request()
    batch = list(original.mutations)
    copied = replace(original, mutations=batch)
    batch.clear()
    assert copied.mutations == original.mutations


def test_commit_codec_treats_client_shaped_ids_as_opaque():
    original = request(
        mutation("skill:not-a-client-tree", FilePayload(b"opaque bytes"))
    )
    assert decode_commit_request(encode_commit_request(original)) == original


def test_plaintext_filesystem_keeps_skill_executable_defense(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    snapshot = remote.read()
    tree = TreePayload((TreeEntry("SKILL.md", b"# Skill"),))
    item = mutation("skill:missing-mode", tree)
    fingerprint, _ = inspect_payload(local_resource_for_id(item.id, "0" * 64), tree)
    item = replace(item, after=replace(item.after, fingerprint=fingerprint))
    with pytest.raises(InvalidContent, match="Mutation executable state mismatch"):
        remote.commit(snapshot, (item,))
    assert remote.read() == snapshot


def test_presence_update_delete_and_unchanged_descriptors():
    present = mutation("present", MemberPayload())
    present = replace(present, after=replace(present.after, fingerprint=""))
    old = mutation("old")
    update = mutation("old", FilePayload(b"new"), old.after)
    deletion = ResourceMutation("gone", old.after, None, None)
    untouched = replace(
        old.after, references=("opaque-ref",), mode_fingerprint="f" * 64
    )
    expected = RemoteSnapshot(
        "center",
        10,
        {
            "old": old.after,
            "gone": old.after,
            "untouched": untouched,
        },
    )
    original = request(present, update, deletion, expected=expected)
    restored = decode_commit_request(encode_commit_request(original))
    after = committed_snapshot(restored)
    assert after.revision == 11
    assert after.resources == {
        "present": present.after,
        "old": update.after,
        "untouched": untouched,
    }
    assert after.resources["present"].fingerprint == ""
    assert "gone" in expected.resources
    receipt = build_commit_result(restored)
    assert receipt.accepted_revision == 11
    validate_commit_result(restored, receipt)


def test_retry_identity_and_previous_receipt_are_distinct():
    original = request()
    assert decode_commit_request(encode_commit_request(original)) == original
    new_id = replace(original, request_id="new logical request")
    assert new_id.mutation_digest == original.mutation_digest
    assert (
        build_commit_result(new_id).result_digest
        != build_commit_result(original).result_digest
    )
    cursor = ReceiptCursor("previous", "f" * 64)
    next_request = request(previous_receipt=cursor)
    assert next_request.mutation_digest != original.mutation_digest
    assert (
        decode_commit_request(encode_commit_request(next_request)).previous_receipt
        == cursor
    )


@pytest.mark.parametrize(
    "field",
    [
        "client",
        "sync",
        "revision",
        "expected-resource",
        "before",
        "after",
        "references",
        "mode",
        "payload",
        "cursor",
    ],
)
def test_digest_covers_every_semantic_field(field):
    original = request()
    changed = original
    m = original.mutations[0]
    if field == "client":
        changed = replace(original, client_id="other")
    elif field == "sync":
        changed = replace(
            original, expected=replace(original.expected, sync_id="other")
        )
    elif field == "revision":
        changed = replace(original, expected=replace(original.expected, revision=11))
    elif field == "expected-resource":
        changed = replace(
            original,
            expected=replace(original.expected, resources={"unmutated": m.after}),
        )
    elif field == "cursor":
        changed = replace(
            original, previous_receipt=ReceiptCursor("previous", "a" * 64)
        )
    else:
        if field == "before":
            m = replace(m, before=m.after)
        elif field == "after":
            m = replace(
                m, after=replace(m.after, fingerprint="another opaque fingerprint")
            )
        elif field == "references":
            m = replace(m, after=replace(m.after, references=("opaque ref",)))
        elif field == "mode":
            m = replace(m, after=replace(m.after, mode_fingerprint="b" * 64))
        elif field == "payload":
            m = mutation(payload=FilePayload(b"changed"))
        changed = replace(original, mutations=(m,))
    with pytest.raises(InvalidContent, match="digest mismatch"):
        validate_commit_request(changed)
    with pytest.raises(InvalidContent):
        encode_commit_request(changed)


@pytest.mark.parametrize("change", ["empty", "duplicate", "wrong-before"])
def test_invalid_batch_cannot_be_encoded(change):
    m = mutation()
    batch = {
        "empty": (),
        "duplicate": (m, m),
        "wrong-before": (replace(m, before=m.after),),
    }[change]
    with pytest.raises(InvalidContent):
        build_commit_request(
            "client", "request", RemoteSnapshot("center", 0, {}), batch
        )


@pytest.mark.parametrize(
    "malformed",
    [
        "unknown-version",
        "boolean-version",
        "duplicate-field",
        "duplicate-resource",
        "duplicate-mutation",
        "unknown-field",
        "absent-descriptor",
        "references-string",
        "bad-payload",
        "delete-payload",
        "forged-digest",
        "noncanonical",
        "mutations-object",
        "root-list",
        "invalid-json",
        "non-bytes",
    ],
)
def test_strict_decoding(malformed):
    original = request()
    raw = json.loads(encode_commit_request(original))
    encoded = None
    if malformed == "unknown-version":
        raw["version"] = 2
    elif malformed == "boolean-version":
        raw["version"] = True
    elif malformed == "duplicate-field":
        encoded = b'{"version":1,' + encode_commit_request(original)[1:]
    elif malformed == "duplicate-resource":
        descriptor = encode_json(raw["mutations"][0]["after"])
        encoded = encode_commit_request(original).replace(
            b'"resources":{}',
            b'"resources":{"same":' + descriptor + b',"same":' + descriptor + b"}",
        )
    elif malformed == "duplicate-mutation":
        raw["mutations"].append(raw["mutations"][0])
    elif malformed == "unknown-field":
        raw["secret"] = "must not appear in errors"
    elif malformed == "absent-descriptor":
        raw["expected"]["resources"]["missing"] = None
    elif malformed == "references-string":
        raw["mutations"][0]["after"]["references"] = "ref"
    elif malformed == "bad-payload":
        raw["mutations"][0]["payload"] = "!not base64!"
    elif malformed == "delete-payload":
        raw["mutations"][0]["after"] = None
    elif malformed == "forged-digest":
        raw["mutation_digest"] = "a" * 64
    elif malformed == "noncanonical":
        encoded = json.dumps(raw, indent=2).encode()
    elif malformed == "mutations-object":
        raw["mutations"] = {}
    elif malformed == "root-list":
        raw = []
    elif malformed == "invalid-json":
        encoded = b"not json: must not appear in errors"
    elif malformed == "non-bytes":
        encoded = "not bytes"
    with pytest.raises(InvalidContent) as error:
        decode_commit_request(encode_json(raw) if encoded is None else encoded)
    assert "must not appear in errors" not in str(error.value)


def test_noncanonical_mutation_order_is_rejected():
    original = request(mutation("z"), mutation("a"))
    raw = json.loads(encode_commit_request(original))
    raw["mutations"].reverse()
    with pytest.raises(InvalidContent, match="Noncanonical"):
        decode_commit_request(encode_json(raw))


@pytest.mark.parametrize(
    "field,value",
    [
        ("sync_id", "other center"),
        ("client_id", "other client"),
        ("request_id", "other request"),
        ("mutation_digest", "a" * 64),
        ("accepted_revision", 12),
        ("result_digest", "a" * 64),
    ],
)
def test_receipt_must_bind_identity_revision_and_historical_result(field, value):
    original = request()
    receipt = build_commit_result(original)
    with pytest.raises(InvalidContent, match="result"):
        validate_commit_result(original, replace(receipt, **{field: value}))


def test_historical_receipt_remains_valid_after_later_remote_changes():
    original = request()
    receipt = build_commit_result(original)
    later = build_commit_request(
        original.client_id,
        "later request",
        committed_snapshot(original),
        (mutation("another"),),
        previous_receipt=ReceiptCursor(receipt.request_id, receipt.mutation_digest),
    )
    assert committed_snapshot(later).revision == 12
    validate_commit_result(original, receipt)
    with pytest.raises(InvalidContent):
        validate_commit_result(original, build_commit_result(later))


@pytest.mark.parametrize("revision", [True, -1, 0, 1.5, "1"])
def test_result_rejects_invalid_accepted_revision(revision):
    with pytest.raises(InvalidContent):
        CommitResult("center", "client", "request", "a" * 64, revision, "b" * 64)


@pytest.mark.parametrize("identity", [None, "", 1, True])
def test_cursor_rejects_invalid_identity(identity):
    with pytest.raises(InvalidContent):
        ReceiptCursor(identity, "a" * 64)
