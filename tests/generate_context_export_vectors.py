"""Generate public test-only native HPKE exports for independent receivers."""

import json
import sys
from pathlib import Path

from workspace_context_setup import (
    COLLECTIONS,
    KEY,
    KEYS,
    NOW,
    PRIVATE,
    SYNC,
    VAULT,
    Reader,
    inputs,
)

from aikito.workspace.encrypted_remote import _b64
from aikito.workspace.payload import encode_payload
from aikito.workspace.remote_wire import _canonical, _descriptor
from aikito.workspace.resource_collections import collection_for_resource
from aikito.workspace.scoped_export import export_scoped_read, export_scoped_write


def generate(target):
    reader = Reader()
    cases = []
    for collection in COLLECTIONS:
        samples = [
            {
                "logical_id": resource.id,
                "resource_id": opaque,
                "descriptor": _descriptor(reader.descriptors[opaque]),
                "payload_hex": encode_payload(reader.payloads[opaque]).hex(),
                "plaintext_payload": _b64(encode_payload(payload)),
                "logical_fingerprint": resource.fingerprint,
                "logical_mode_fingerprint": resource.mode_fingerprint,
            }
            for opaque, (resource, payload) in reader.original.items()
            if collection_for_resource(resource.id) == collection
        ]
        for permission in ("read", "write"):
            recipient, auth = inputs(collection, permission)
            action = export_scoped_read if permission == "read" else export_scoped_write
            packet = action(
                reader,
                KEYS,
                collection_id=collection,
                recipient=recipient,
                authorization=auth,
                now=NOW,
            )
            cases.append(
                {
                    "permission": permission,
                    "authorization": json.loads(auth),
                    "packet": json.loads(packet),
                    "samples": samples,
                }
            )
    vector = {
        "test_only": True,
        "version": 2,
        "vault_id": VAULT,
        "sync_id": SYNC,
        "vrk_hex": KEY.hex(),
        "recipient_private_hex": PRIVATE.hex(),
        "cases": cases,
    }
    Path(target).write_bytes(_canonical(vector))


if __name__ == "__main__":
    generate(sys.argv[1])
