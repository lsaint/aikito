"""Persist and reload canonical commit identities in separate CLI invocations."""

from __future__ import annotations

import argparse
import hashlib
import json
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
    encode_commit_request,
    validate_commit_result,
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
        "opaque center 你好", 10, {"deleted": descriptors["opaque-file"]}
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
    assert after.revision == 11 and after.resources == descriptors

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
