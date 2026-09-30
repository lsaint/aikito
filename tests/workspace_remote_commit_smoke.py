"""Persist and reload canonical commit identities in separate CLI invocations."""

from __future__ import annotations

import argparse
import hashlib
import json
import base64
from dataclasses import replace
from pathlib import Path

from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    ResourceDescriptor,
    ResourceMutation,
    TomlPayload,
    TreeEntry,
    TreePayload,
    payload_hash,
)
from aikito.workspace.remote_store import InvalidContent, ReceiptCursor, RemoteSnapshot
from aikito.workspace.remote_wire import (
    build_commit_request,
    build_commit_result,
    committed_snapshot,
    decode_commit_request,
    decode_receipt,
    decode_state_json,
    encode_receipt,
    encode_commit_request,
    validate_commit_result,
)
from aikito.workspace.pending_commit import PendingCommit, PendingCommitError
from aikito.workspace.replica_state import ReplicaState


def canonical(raw):
    return json.dumps(
        raw, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def hardening_checks(base: Path, original, receipt, *, verify_only: bool):
    request = build_commit_request(
        "a" * 32,
        original.request_id,
        original.expected,
        original.mutations,
        previous_receipt=original.previous_receipt,
    )
    state = ReplicaState(
        request.expected.sync_id, request.client_id, 9, {}, request.previous_receipt
    )
    pending = PendingCommit(
        request,
        state.encode(),
        tuple(m.id for m in request.mutations),
        ("opaque-excluded",),
    )
    if not verify_only:
        (base / "receipt.json").write_bytes(canonical(encode_receipt(receipt)))
        (base / "pending.json").write_bytes(pending.encode().encode("utf-8"))
    receipt_bytes = (base / "receipt.json").read_bytes()
    persisted_receipt = decode_receipt(decode_state_json(receipt_bytes))
    validate_commit_result(original, persisted_receipt)
    pending_bytes = (base / "pending.json").read_bytes()
    assert PendingCommit.decode(pending_bytes) == pending
    assert encode_commit_request(
        PendingCommit.decode(pending_bytes).request
    ) == encode_commit_request(request)
    rejected = []

    def rejects(label, operation, error=InvalidContent):
        try:
            operation()
        except error:
            rejected.append(label)
        else:
            raise AssertionError(f"Malformed encoding accepted: {label}")

    encoded = encode_commit_request(original)
    for label, raw in (
        ("invalid-utf8", b'"\xff"'),
        ("utf16", encoded.decode().encode("utf-16")),
        ("nonfinite", b'{"revision":NaN}'),
        ("duplicate-nested-field", b'{"a":{"same":1,"same":2}}'),
        ("excess-depth", b"[" * 65 + b"0" + b"]" * 65),
        ("unpaired-surrogate", b'{"id":"\\ud800"}'),
    ):
        rejects(label, lambda: decode_state_json(raw))
    raw = json.loads(encoded)
    raw["version"] = 2
    rejects("request-version", lambda: decode_commit_request(canonical(raw)))
    raw = json.loads(encoded)
    raw["mutations"].reverse()
    rejects("request-order", lambda: decode_commit_request(canonical(raw)))
    raw = json.loads(encoded)
    item = next(m for m in raw["mutations"] if m["payload"] is not None)
    item["after"]["content_hash"] = "f" * 64
    rejects("payload-hash", lambda: decode_commit_request(canonical(raw)))
    raw_receipt = json.loads(receipt_bytes)
    raw_receipt["accepted_revision"] = True
    rejects("receipt-boolean-revision", lambda: decode_receipt(raw_receipt))
    rejects(
        "result-identity",
        lambda: validate_commit_result(original, replace(receipt, client_id="other")),
    )
    raw_pending = json.loads(pending_bytes)
    raw_pending["safe_resource_ids"].reverse()
    body = {k: v for k, v in raw_pending.items() if k != "envelope_digest"}
    raw_pending["envelope_digest"] = hashlib.sha256(canonical(body)).hexdigest()
    rejects(
        "pending-scope-order",
        lambda: PendingCommit.decode(canonical(raw_pending)),
        PendingCommitError,
    )
    raw_pending = json.loads(pending_bytes)
    raw_pending["request"] = (
        base64.b64encode(encode_commit_request(request)).decode() + "\n"
    )
    body = {k: v for k, v in raw_pending.items() if k != "envelope_digest"}
    raw_pending["envelope_digest"] = hashlib.sha256(canonical(body)).hexdigest()
    rejects(
        "pending-base64",
        lambda: PendingCommit.decode(canonical(raw_pending)),
        PendingCommitError,
    )
    assert (base / "pending.json").read_bytes() == pending_bytes
    assert (base / "receipt.json").read_bytes() == receipt_bytes
    (base / "rejected.json").write_bytes(
        canonical(
            {
                "cases": rejected,
                "count": len(rejected),
                "verified_after_restart": verify_only,
            }
        )
    )


def exercise(base: Path, *, verify_only: bool) -> None:
    payloads = {
        "opaque-file": FilePayload(b"\x00\xffUnicode: \xe4\xbd\xa0\r\n"),
        "opaque-toml": TomlPayload(
            ("literal.key",), "value = [2026-09-30, 12:34:56, { enabled = true }]\n"
        ),
        "opaque-tree": TreePayload(
            (
                TreeEntry("SKILL.md", "# Portable 你好\r\n".encode()),
                TreeEntry("run.sh", b"echo portable\n", True),
                TreeEntry("empty"),
            )
        ),
        "opaque-member": MemberPayload(),
    }
    descriptors = {
        key: ResourceDescriptor(
            "" if key == "opaque-member" else "opaque fingerprint",
            payload_hash(payload),
            ("opaque reference",),
        )
        for key, payload in payloads.items()
    }
    expected = RemoteSnapshot(
        "opaque center 你好",
        10,
        {
            "deleted": descriptors["opaque-file"],
            "untouched": descriptors["opaque-file"],
        },
    )
    mutations = tuple(
        ResourceMutation(key, None, descriptors[key], payloads[key])
        for key in reversed(tuple(payloads))
    ) + (ResourceMutation("deleted", descriptors["opaque-file"], None, None),)
    original = build_commit_request(
        "opaque client",
        "stable-request-id",
        expected,
        mutations,
        previous_receipt=ReceiptCursor("previous-request", "a" * 64),
    )
    encoded = encode_commit_request(original)
    receipt = build_commit_result(original)
    if not verify_only:
        base.mkdir(parents=True)
        (base / "request.json").write_bytes(encoded)
        (base / "result-digest.txt").write_text(receipt.result_digest, encoding="utf-8")

    persisted = (base / "request.json").read_bytes()
    restored = decode_commit_request(persisted)
    assert restored == original
    assert encode_commit_request(restored) == persisted == encoded
    assert build_commit_result(restored).result_digest == (
        base / "result-digest.txt"
    ).read_text(encoding="utf-8")
    validate_commit_result(restored, receipt)
    after = committed_snapshot(restored)
    assert after.revision == 11 and after.resources == {
        **descriptors,
        "untouched": descriptors["opaque-file"],
    }
    hardening_checks(base, original, receipt, verify_only=verify_only)

    tampered = json.loads(persisted)
    tampered["client_id"] = "other client"
    try:
        decode_commit_request(json.dumps(tampered).encode())
    except InvalidContent:
        pass
    else:
        raise AssertionError("Tampered persisted request was accepted")

    (base / "checked.json").write_text(
        json.dumps(
            {
                "request_id": restored.request_id,
                "mutation_digest": restored.mutation_digest,
                "result_digest": receipt.result_digest,
                "encoding_sha256": hashlib.sha256(persisted).hexdigest(),
                "accepted_revision": after.revision,
                "verified_after_restart": verify_only,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base", type=Path)
    parser.add_argument("--verify-only", action="store_true")
    args = parser.parse_args()
    exercise(args.base.resolve(), verify_only=args.verify_only)
    print("[SUCCESS] Canonical commit persistence checks passed")
