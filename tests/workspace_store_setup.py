"""Publish fixture batches through the real receipt-aware storage contract."""

from __future__ import annotations

import uuid
import hashlib
from collections.abc import Sequence

from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
)
from aikito.workspace.remote_store import RemoteSnapshot, RemoteStore
from aikito.workspace.remote_wire import (
    build_commit_request,
    committed_snapshot,
    validate_commit_result,
)


def publish_batch(
    store: RemoteStore, expected: RemoteSnapshot, mutations: Sequence[ResourceMutation]
) -> RemoteSnapshot:
    # Fixture writers are independent clients; replica history is tested separately.
    request = build_commit_request(
        uuid.uuid4().hex, uuid.uuid4().hex, expected, mutations
    )
    result = store.commit(request)
    validate_commit_result(request, result)
    return committed_snapshot(request)


def mutation(name="one", data=b"one", before=None):
    payload = FilePayload(data)
    return ResourceMutation(
        f"memory:notes/{name}.md",
        before,
        ResourceDescriptor(
            hashlib.sha256(data).hexdigest(),
            payload_hash(payload),
            len(encode_payload(payload)),
        ),
        payload,
    )


def request(
    store, *batch, client="client-a", identity="request-a", cursor=None, expected=None
):
    return build_commit_request(
        client,
        identity,
        expected or store.read(),
        batch or (mutation(),),
        previous_receipt=cursor,
    )


def resolve(store, req):
    return store.resolve_commit(
        req.expected.sync_id, req.client_id, req.request_id, req.mutation_digest
    )
