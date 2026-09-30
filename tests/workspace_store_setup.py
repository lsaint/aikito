"""Publish fixture batches through the real receipt-aware storage contract."""

from __future__ import annotations

import uuid
from collections.abc import Sequence

from aikito.workspace.payload import ResourceMutation
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
