"""Strict durable encodings, digest domains and untrusted nested model values."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import replace

import pytest

from aikito.workspace.payload import ResourceMutation, TreeEntry, TreePayload
from aikito.workspace.pending_commit import (
    PendingCommit,
    PendingCommitError,
    PendingCommitStore,
)
from aikito.workspace.remote_store import InvalidContent, ReceiptCursor, RemoteSnapshot
from aikito.workspace.remote import FilesystemRemote
from aikito.workspace.remote_wire import (
    build_commit_request,
    build_commit_result,
    commit_request_digest,
    decode_base64,
    decode_commit_request,
    decode_receipt,
    decode_state_json,
    MAX_COMMIT_JSON_DEPTH,
    encode_commit_request,
    encode_receipt,
    validate_commit_request,
    validate_commit_result,
)
from aikito.workspace.replica_state import ReplicaState
from aikito.workspace.resource_state import (
    PENDING_COMMIT_STATE,
    REPLICA_STATE,
    REMOTE_STATE,
    local_resource_for_id,
)
from aikito.workspace.transactions import WorkspaceCoreError
from test_workspace_remote_commit import mutation, request
from test_workspace_remote_receipts import mutation as plaintext_mutation
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace


def canonical(raw):
    return json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def signed_envelope(raw):
    body = {key: value for key, value in raw.items() if key != "envelope_digest"}
    return canonical(
        {**body, "envelope_digest": hashlib.sha256(canonical(body)).hexdigest()}
    )


def pending_record():
    req = build_commit_request(
        "a" * 32,
        "request 你好",
        RemoteSnapshot("center 你好", 7, {}),
        (mutation("memory:notes/b.md"), mutation("memory:notes/a.md")),
        previous_receipt=ReceiptCursor("prior", "f" * 64),
    )
    state = ReplicaState(
        req.expected.sync_id, req.client_id, 6, {}, req.previous_receipt
    )
    return PendingCommit(
        req,
        state.encode(),
        ("memory:notes/b.md", "memory:notes/a.md"),
        ("memory:notes/excluded.md",),
    )


@pytest.mark.parametrize(
    "encoded", ["AB==", "AAB=", "AA===", "AAAA=", "AA==\n", "_A==", "é", 0, b"AA=="]
)
def test_noncanonical_base64_is_rejected(encoded):
    with pytest.raises(InvalidContent):
        decode_base64(encoded)


@pytest.mark.parametrize("data", [b"", b"\0", b"\0\0", b"\0\xff\n", bytes(range(256))])
def test_base64_retains_exact_binary_bytes(data):
    assert decode_base64(base64.b64encode(data).decode("ascii")) == data


@pytest.mark.parametrize(
    "encoded",
    [
        b'"\xff"',
        '{"value":1}'.encode("utf-16"),
        b"\xef\xbb\xbf{}",
        '{"value":NaN}',
        '{"value":Infinity}',
        '{"value":-Infinity}',
        '{"nested":{"same":1,"same":2}}',
        "[" * 2000 + "0" + "]" * 2000,
        {},
        bytearray(b"{}"),
    ],
)
def test_state_json_rejects_invalid_text_numbers_duplicate_keys_and_depth(encoded):
    with pytest.raises(InvalidContent):
        decode_state_json(encoded)


@pytest.mark.parametrize(
    "field",
    [
        "snapshot-revision",
        "snapshot-id",
        "snapshot-descriptor",
        "before-descriptor",
        "after-descriptor",
        "cursor",
        "file-bytes",
        "tree-node",
    ],
)
def test_validation_rechecks_nested_constructor_invariants(field):
    item = mutation()
    old = replace(item.after)
    req = request(
        mutation(before=old),
        expected=RemoteSnapshot("center", 0, {item.id: old}),
        previous_receipt=ReceiptCursor("prior", "e" * 64),
    )
    if field == "snapshot-revision":
        object.__setattr__(req.expected, "revision", True)
    elif field == "snapshot-id":
        object.__setattr__(req.expected, "sync_id", 1)
    elif field == "snapshot-descriptor":
        object.__setattr__(old, "references", (False,))
    elif field == "before-descriptor":
        object.__setattr__(req.mutations[0].before, "content_hash", "bad")
    elif field == "after-descriptor":
        object.__setattr__(req.mutations[0].after, "fingerprint", False)
    elif field == "cursor":
        object.__setattr__(req.previous_receipt, "request_id", 0)
    elif field == "file-bytes":
        object.__setattr__(req.mutations[0].payload, "data", bytearray(b"changed"))
    elif field == "tree-node":
        tree = TreePayload((TreeEntry("SKILL.md", b"x"),))
        req = request(mutation(payload=tree))
        object.__setattr__(tree.entries[0], "executable", 1)
    # Re-sign so rejection proves structural validation, not a stale digest.
    try:
        object.__setattr__(req, "mutation_digest", commit_request_digest(req))
    except (InvalidContent, TypeError):
        pass
    with pytest.raises(InvalidContent):
        validate_commit_request(req)


def test_result_digest_binds_whole_predicted_snapshot_and_its_domain():
    changed = mutation("changed")
    deleted = mutation("deleted")
    untouched = replace(
        changed.after, references=("opaque ref",), mode_fingerprint="e" * 64
    )
    expected = RemoteSnapshot(
        "center", 5, {"deleted": deleted.after, "untouched": untouched}
    )
    req = request(
        changed,
        ResourceMutation("deleted", deleted.after, None, None),
        expected=expected,
    )
    result = build_commit_result(req)

    def descriptor(value):
        return {
            "fingerprint": value.fingerprint,
            "content_hash": value.content_hash,
            "size": value.size,
            "references": list(value.references),
            "mode_fingerprint": value.mode_fingerprint,
        }

    body = {
        "version": 1,
        "kind": "aikito.commit-result",
        "sync_id": "center",
        "client_id": req.client_id,
        "request_id": req.request_id,
        "mutation_digest": req.mutation_digest,
        "accepted_revision": 6,
        "resources": {
            "changed": descriptor(changed.after),
            "untouched": descriptor(untouched),
        },
    }
    assert result.result_digest == hashlib.sha256(canonical(body)).hexdigest()
    for changed_body in (
        {**body, "kind": "aikito.commit-request"},
        {**body, "version": 2},
        {**body, "resources": {"changed": descriptor(changed.after)}},
    ):
        assert (
            result.result_digest != hashlib.sha256(canonical(changed_body)).hexdigest()
        )
    assert result.result_digest != req.mutation_digest


def test_request_id_is_excluded_from_mutation_digest_but_bound_by_result():
    req = request()
    other = replace(req, request_id="other")
    validate_commit_request(other)
    assert other.mutation_digest == req.mutation_digest
    assert (
        build_commit_result(other).result_digest
        != build_commit_result(req).result_digest
    )


def test_receipt_validation_rejects_bypassed_boolean_revision():
    req = request(expected=RemoteSnapshot("center", 0, {}))
    result = build_commit_result(req)
    object.__setattr__(result, "accepted_revision", True)
    with pytest.raises(InvalidContent):
        validate_commit_result(req, result)
    with pytest.raises(InvalidContent):
        encode_receipt(result)


@pytest.mark.parametrize(
    "field,value",
    [
        ("version", True),
        ("version", 2),
        ("accepted_revision", True),
        ("accepted_revision", 1.0),
        ("accepted_revision", 0),
        ("sync_id", None),
        ("client_id", ""),
        ("request_id", 1),
        ("mutation_digest", "F" * 64),
        ("result_digest", "short"),
        ("extra", "sensitive input"),
    ],
)
def test_persisted_receipt_decode_is_strict(field, value):
    raw = encode_receipt(build_commit_result(request()))
    raw[field] = value
    with pytest.raises(InvalidContent) as error:
        decode_receipt(raw)
    assert "sensitive input" not in str(error.value)


def test_pending_roundtrip_keeps_original_state_text_and_canonical_request():
    pending = pending_record()
    pretty_state = json.dumps(
        json.loads(pending.replica_state_text), indent=2, ensure_ascii=False
    )
    pending = replace(pending, replica_state_text=pretty_state)
    encoded = pending.encode()
    restored = PendingCommit.decode(encoded.encode("utf-8"))
    assert restored == pending
    assert restored.replica_state_text == pretty_state
    assert encode_commit_request(restored.request) == encode_commit_request(
        pending.request
    )
    assert restored.encode() == encoded
    # Envelope JSON whitespace is immaterial; embedded state text remains exact.
    assert PendingCommit.decode(json.dumps(json.loads(encoded), indent=2)) == pending


@pytest.mark.parametrize(
    "corruption",
    [
        "unsorted-scope",
        "duplicate-scope",
        "overlap",
        "foreign-client",
        "foreign-sync",
        "cursor",
        "revision",
        "embedded-version",
        "embedded-fields",
        "invalid-base64",
        "alias-base64",
        "request-version",
        "request-order",
    ],
)
def test_resigned_pending_cannot_smuggle_invalid_nested_identity_or_encoding(
    corruption,
):
    pending = pending_record()
    raw = json.loads(pending.encode())
    state = json.loads(raw["replica_state"])
    request_bytes = base64.b64decode(raw["request"])
    request_raw = json.loads(request_bytes)
    if corruption == "unsorted-scope":
        raw["safe_resource_ids"].reverse()
    elif corruption == "duplicate-scope":
        raw["excluded_resource_ids"] *= 2
    elif corruption == "overlap":
        raw["excluded_resource_ids"] = raw["safe_resource_ids"][:1]
    elif corruption == "foreign-client":
        state["replica_id"] = "b" * 32
    elif corruption == "foreign-sync":
        state["sync_id"] = "other"
    elif corruption == "cursor":
        state["receipt_cursor"]["mutation_digest"] = "b" * 64
    elif corruption == "revision":
        state["revision"] = 8
    elif corruption == "embedded-version":
        state["version"] = True
    elif corruption == "embedded-fields":
        raw["replica_state"] = pending.replica_state_text.replace(
            '"revision": 6', '"revision": 6, "revision": 6'
        )
    elif corruption == "invalid-base64":
        raw["request"] += "\n"
    elif corruption == "alias-base64":
        # Nonzero unused pad bits encode the same data in liberal decoders.
        req = pending.request
        while not len(encode_commit_request(req)) % 3:
            req = replace(req, request_id=req.request_id + "x")
        request_bytes = encode_commit_request(req)
        encoded = base64.b64encode(request_bytes).decode()
        alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
        offset = len(encoded) - (3 if encoded.endswith("==") else 2)
        raw["request"] = (
            encoded[:offset]
            + alphabet[alphabet.index(encoded[offset]) | 1]
            + encoded[offset + 1 :]
        )
    elif corruption == "request-version":
        request_raw["version"] = 2
    elif corruption == "request-order":
        request_raw["mutations"].reverse()
    if corruption in {
        "foreign-client",
        "foreign-sync",
        "cursor",
        "revision",
        "embedded-version",
    }:
        raw["replica_state"] = canonical(state).decode()
    if corruption in {"request-version", "request-order"}:
        raw["request"] = base64.b64encode(canonical(request_raw)).decode()
    with pytest.raises(PendingCommitError):
        PendingCommit.decode(signed_envelope(raw))


@pytest.mark.parametrize(
    "bad_bytes",
    [
        b"\xff",
        '{"version":1}'.encode("utf-16"),
        b"\xef\xbb\xbf{}",
        b'{"version":NaN}',
        b"[" * 2000 + b"0" + b"]" * 2000,
    ],
)
def test_unreadable_pending_file_is_preserved_and_blocks(tmp_path, bad_bytes):
    local = _workspace(tmp_path / "local")
    pending = pending_record()
    path = local / PENDING_COMMIT_STATE
    path.parent.mkdir(parents=True)
    path.write_bytes(bad_bytes)
    state_path = local / REPLICA_STATE
    state_path.write_text(pending.replica_state_text)
    before = path.read_bytes(), state_path.read_bytes()
    with pytest.raises(PendingCommitError):
        PendingCommitStore(local, tmp_path / "home").load()
    assert before == (path.read_bytes(), state_path.read_bytes())


def test_json_depth_limit_is_explicit_and_independent_of_python_parser():
    value = decode_state_json(
        "[" * MAX_COMMIT_JSON_DEPTH + "0" + "]" * MAX_COMMIT_JSON_DEPTH
    )
    for _ in range(MAX_COMMIT_JSON_DEPTH):
        value = value[0]
    assert value == 0
    with pytest.raises(InvalidContent, match="nesting"):
        decode_state_json(
            "[" * (MAX_COMMIT_JSON_DEPTH + 1) + "0" + "]" * (MAX_COMMIT_JSON_DEPTH + 1)
        )


@pytest.mark.parametrize("encoded", [b'{"x":"\\ud800"}', b'{"\\udfff":0}'])
def test_unpaired_unicode_surrogates_are_not_valid_durable_strings(encoded):
    with pytest.raises(InvalidContent):
        decode_state_json(encoded)


def test_unicode_codepoints_are_preserved_without_normalizing_opaque_ids():
    base = request(mutation("é😀"), mutation("e\u0301"))
    restored = decode_commit_request(encode_commit_request(base))
    assert {m.id for m in restored.mutations} == {"é😀", "e\u0301"}
    assert restored == base
    assert decode_state_json(b'"\\ud83d\\ude00"') == "😀"


def test_invalid_utf8_replica_state_blocks_pending_without_rewriting(tmp_path):
    local = _workspace(tmp_path / "local")
    pending = pending_record()
    path = local / PENDING_COMMIT_STATE
    path.parent.mkdir(parents=True)
    path.write_text(pending.encode())
    state_path = local / REPLICA_STATE
    state_path.write_bytes(b"\xff")
    with pytest.raises(WorkspaceCoreError, match="text encoding"):
        PendingCommitStore(local, tmp_path / "home").load()
    assert path.read_text() == pending.encode()
    assert state_path.read_bytes() == b"\xff"


@pytest.mark.parametrize("backend", ["filesystem", "memory"])
def test_backend_cannot_accept_boolean_snapshot_cas_even_with_recomputed_digest(
    tmp_path, backend
):
    remote = (
        FilesystemRemote.create(tmp_path / "center")
        if backend == "filesystem"
        else InMemoryRemote()
    )
    before = remote.read()
    req = build_commit_request("client", "request", before, (plaintext_mutation(),))
    # False compares equal to revision 0 in Python, but is not a valid revision.
    object.__setattr__(req.expected, "revision", False)
    object.__setattr__(req, "mutation_digest", commit_request_digest(req))
    with pytest.raises(InvalidContent):
        remote.commit(req)
    assert remote.read().revision == 0
    assert remote.read().resources == {}
    assert (
        remote.resolve_commit(
            req.expected.sync_id, req.client_id, req.request_id, req.mutation_digest
        )
        is None
    )


def test_corrupt_utf8_center_state_is_refused_without_recovery_or_erasure(tmp_path):
    remote = FilesystemRemote.create(tmp_path / "center")
    snapshot = remote.read()
    path = remote.root / REMOTE_STATE
    path.write_bytes(b"\xff")
    for operation in (
        remote.read,
        lambda: remote.fetch(snapshot, ()),
        lambda: remote.resolve_commit(snapshot.sync_id, "client", "request", "a" * 64),
    ):
        with pytest.raises(InvalidContent):
            operation()
        assert path.read_bytes() == b"\xff"


def test_legacy_replica_crlf_text_can_complete_through_existing_local_journal(tmp_path):
    local = _workspace(tmp_path / "local")
    req = build_commit_request(
        "a" * 32,
        "request",
        RemoteSnapshot("center", 7, {}),
        (plaintext_mutation(),),
        previous_receipt=ReceiptCursor("prior", "f" * 64),
    )
    old = ReplicaState("center", req.client_id, 6, {}, req.previous_receipt)
    raw = (
        json.dumps(json.loads(old.encode()), indent=2)
        .replace("\n", "\r\n")
        .encode("utf-8")
    )
    path = local / REPLICA_STATE
    path.parent.mkdir(parents=True)
    path.write_bytes(raw)
    store = PendingCommitStore(local, tmp_path / "home")
    pending = store.persist(req, safe_resource_ids=tuple(m.id for m in req.mutations))
    assert path.read_bytes() == raw
    result = build_commit_result(req)
    state = ReplicaState(
        "center",
        req.client_id,
        result.accepted_revision,
        {m.id: local_resource_for_id(m.id, m.after.fingerprint) for m in req.mutations},
    )
    complete = store.complete(pending, result, state)
    assert pending.completed_by(complete)
    store.clear(pending)
    assert store.load() is None
