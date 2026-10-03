"""Generate byte-exact protocol examples for independent implementations."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

from workspace_memory_remote import InMemoryRemote

from aikito.workspace.payload import encode_payload
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
from aikito.workspace.remote_protocol import (
    ERROR_CODES,
    Operation,
    RemoteProtocolHandler,
    encode_commit_request,
    encode_error,
    encode_fetch_request,
    encode_read_request,
    encode_read_response,
    encode_recover_request,
    encode_resolve_request,
)
from aikito.workspace.remote_wire import (
    build_commit_request,
    build_commit_result,
    committed_snapshot,
    encode_commit_request as encode_domain_request,
)
from aikito.workspace.toml_render import TomlValue

DEFAULT_DIRECTORY = Path(__file__).parent / "fixtures/remote_protocol_vectors"


def canonical(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def generate_vectors() -> dict[str, bytes]:
    artifacts = {}
    manifest = {
        "protocol": 1,
        "description": "Byte-exact examples; operation pairs run in manifest order against the stated initial store.",
        "initial_sync_id": "中心:<>&\u2028\u2029",
        "operations": [],
        "errors": [],
        "malformed": [],
        "digests": [],
    }
    backend = InMemoryRemote(manifest["initial_sync_id"])
    handler = RemoteProtocolHandler(backend)

    def pair(name, message, description, *, malformed=False):
        request_name, response_name = f"{name}.request.bin", f"{name}.response.bin"
        artifacts[request_name] = message
        artifacts[response_name] = handler.handle(message)
        manifest["malformed" if malformed else "operations"].append(
            {
                "name": name,
                "description": description,
                "request": request_name,
                "response": response_name,
            }
        )

    pair("read-empty", encode_read_request(), "Read the initial empty store")
    pair(
        "fetch-empty",
        encode_fetch_request(backend.read(), []),
        "Empty fetch with snapshot validation",
    )
    payloads = [
        ("内容:<>&\u2028\u2029", FilePayload("内容:<>&\u2028\u2029\n".encode())),
        (
            "typed-value",
            TomlPayload.from_value(
                TomlValue(
                    ("特殊:<>&\u2028\u2029",),
                    [True, 9007199254740993, 1.5, "内容:<>&\u2028\u2029"],
                )
            ),
        ),
        ("member", MemberPayload()),
        (
            "tree",
            TreePayload(
                (
                    TreeEntry("SKILL.md", b"# Portable\n"),
                    TreeEntry("run.sh", b"echo test\n", True),
                    TreeEntry("empty", None, False),
                )
            ),
        ),
    ]
    mutations = tuple(
        ResourceMutation(
            identity,
            None,
            ResourceDescriptor(
                f"opaque:{identity}",
                payload_hash(payload),
                len(encode_payload(payload)),
            ),
            payload,
        )
        for identity, payload in payloads
    )
    commit = build_commit_request(
        "副本:<>&\u2028\u2029", "请求:<>&\u2028\u2029", backend.read(), mutations
    )
    pair(
        "resolve-missing",
        encode_resolve_request(
            commit.expected.sync_id,
            commit.client_id,
            commit.request_id,
            commit.mutation_digest,
        ),
        "Resolve before acceptance returns null",
    )
    pair(
        "commit",
        encode_commit_request(commit),
        "Atomic batch with all payload kinds and non-ASCII identities",
    )
    pair("read-populated", encode_read_request(), "Read the published descriptors")
    pair(
        "fetch-populated",
        encode_fetch_request(backend.read(), [identity for identity, _ in payloads]),
        "Fetch all payload kinds",
    )
    pair(
        "resolve-accepted",
        encode_resolve_request(
            commit.expected.sync_id,
            commit.client_id,
            commit.request_id,
            commit.mutation_digest,
        ),
        "Resolve the original accepted request",
    )
    pair("recover", encode_recover_request(), "Recover a healthy backend returns false")
    result = build_commit_result(commit)
    domain = json.loads(encode_domain_request(commit))
    domain.pop("request_id")
    domain.pop("mutation_digest")
    result_body = {
        "version": 1,
        "kind": "aikito.commit-result",
        "sync_id": result.sync_id,
        "client_id": result.client_id,
        "request_id": result.request_id,
        "mutation_digest": result.mutation_digest,
        "accepted_revision": result.accepted_revision,
        "resources": json.loads(encode_read_response(committed_snapshot(commit)))[
            "body"
        ]["resources"],
    }
    artifacts["commit.domain.bin"] = encode_domain_request(commit)
    for name, body, digest in [
        ("mutation", domain, commit.mutation_digest),
        ("result", result_body, result.result_digest),
    ]:
        data = canonical(body)
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Digest preimage does not match the implementation")
        artifacts[f"{name}.preimage.bin"] = data
        manifest["digests"].append(
            {
                "name": name,
                "preimage": f"{name}.preimage.bin",
                "sha256": digest,
                "commit_request": "commit.request.bin",
                "commit_response": "commit.response.bin",
            }
        )
    for code in sorted(ERROR_CODES):
        filename = f"error-{code}.response.bin"
        artifacts[filename] = encode_error(Operation.COMMIT, code)
        manifest["errors"].append({"code": code, "response": filename})
    bad_base64 = json.loads(encode_commit_request(commit))
    bad_base64["body"]["mutations"][0]["payload"]["data"] += "="
    malformed = [
        (
            "duplicate-field",
            b'{"body":{},"operation":"read","protocol":1,"protocol":1}',
            "Duplicate object field",
        ),
        (
            "noncanonical-whitespace",
            b'{ "body":{},"operation":"read","protocol":1}',
            "Whitespace differs from canonical bytes",
        ),
        (
            "noncanonical-order",
            b'{"protocol":1,"operation":"read","body":{}}',
            "Object field order differs from canonical bytes",
        ),
        (
            "unknown-version",
            b'{"body":{},"operation":"read","protocol":99}',
            "Unsupported protocol version",
        ),
        (
            "unknown-operation",
            b'{"body":{},"operation":"unknown","protocol":1}',
            "Unsupported operation",
        ),
        (
            "deep-nesting",
            b'{"body":'
            + b"[" * 70
            + b"0"
            + b"]" * 70
            + b',"operation":"read","protocol":1}',
            "Nesting exceeds the strict decoder limit",
        ),
        (
            "noncanonical-base64",
            canonical(bad_base64),
            "Extra Base64 padding is rejected",
        ),
        ("invalid-utf8", b"\xff", "Invalid UTF-8"),
    ]
    for name, data, description in malformed:
        pair(name, data, description, malformed=True)
    artifacts["manifest.json"] = (
        json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
    )
    return artifacts


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    artifacts = generate_vectors()
    if args.check:
        existing = {
            path.name: path.read_bytes()
            for path in args.output.iterdir()
            if path.is_file()
        }
        if existing != artifacts:
            raise SystemExit(
                "Remote protocol vectors differ; regenerate and review the wire changes"
            )
        print("[SUCCESS] Remote protocol vectors match")
    else:
        args.output.mkdir(parents=True, exist_ok=True)
        for name, data in artifacts.items():
            (args.output / name).write_bytes(data)
        print(f"[SUCCESS] Generated {len(artifacts)} remote protocol vector files")


if __name__ == "__main__":
    main()
