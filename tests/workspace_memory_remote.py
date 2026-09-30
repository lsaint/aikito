"""Test-only content-blind store with atomic in-memory publication.

Encoded payload bytes and immutable descriptors belong to one revision.
There is no physical center, journal, or semantic resource/path decoding.
"""

from __future__ import annotations

import hashlib
import threading
import uuid
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType

from aikito.workspace.payload import (
    PayloadError,
    ResourceMutation,
    ResourcePayload,
    decode_payload,
    encode_payload,
)
from aikito.workspace.remote_store import (
    CommitRequest,
    CommitResult,
    InvalidContent,
    RemoteSnapshot,
    SnapshotExpired,
)
from aikito.workspace.remote_receipts import ReceiptHistory
from aikito.workspace.remote_wire import build_commit_result, validate_commit_request


class InMemoryRemote:
    def __init__(self, sync_id: str | None = None):
        self._snapshot = RemoteSnapshot(
            f"memory-center:{uuid.uuid4()}" if sync_id is None else sync_id, 0, {}
        )
        self._payloads: dict[str, bytes] = {}
        self._receipts = ReceiptHistory(self._snapshot.sync_id)
        self._mutex = threading.RLock()

    def validate_replica(self, local: object) -> None:
        """Memory storage imposes no local attachment constraint."""

    def _verify(self) -> None:
        if set(self._payloads) != set(self._snapshot.resources) or any(
            hashlib.sha256(self._payloads[key]).hexdigest() != descriptor.content_hash
            for key, descriptor in self._snapshot.resources.items()
        ):
            raise InvalidContent("Stored payload integrity mismatch")

    def _copy_snapshot(self) -> RemoteSnapshot:
        return RemoteSnapshot(
            self._snapshot.sync_id, self._snapshot.revision, self._snapshot.resources
        )

    def _expect(self, expected: RemoteSnapshot) -> None:
        if expected != self._snapshot:
            raise SnapshotExpired(
                "Resource center identity or revision changed; replan"
            )

    def read(self) -> RemoteSnapshot:
        with self._mutex:
            self._verify()
            return self._copy_snapshot()

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]:
        identities = tuple(ids)
        with self._mutex:
            self._expect(expected)
            self._verify()
            if any(identity not in self._payloads for identity in identities):
                raise InvalidContent("Missing requested resource content")
            try:
                return MappingProxyType(
                    {
                        identity: decode_payload(
                            self._payloads[identity],
                            self._snapshot.resources[identity].content_hash,
                        )
                        for identity in identities
                    }
                )
            except PayloadError as exc:
                raise InvalidContent("Invalid stored payload encoding") from exc

    def commit(self, request: CommitRequest) -> CommitResult:
        validate_commit_request(request)
        with self._mutex:
            self._verify()
            receipt = self._receipts.resolve(
                request.expected.sync_id,
                request.client_id,
                request.request_id,
                request.mutation_digest,
            )
            if receipt is not None:
                return receipt
            self._expect(request.expected)
            self._receipts.check_previous(request.client_id, request.previous_receipt)
            return self._commit_batch(
                request.expected,
                request.mutations,
                receipt=build_commit_result(request),
            )

    def resolve_commit(
        self,
        sync_id: str,
        client_id: str,
        request_id: str,
        mutation_digest: str,
    ) -> CommitResult | None:
        with self._mutex:
            self._verify()
            return self._receipts.resolve(
                sync_id, client_id, request_id, mutation_digest
            )

    def _commit_batch(
        self,
        expected: RemoteSnapshot,
        mutations: Sequence[ResourceMutation],
        *,
        receipt: CommitResult,
    ) -> CommitResult:
        batch = tuple(mutations)
        with self._mutex:
            self._expect(expected)
            self._verify()
            if len(batch) != len({mutation.id for mutation in batch}):
                raise InvalidContent("Duplicate resource mutation ID")
            descriptors, payloads = dict(self._snapshot.resources), dict(self._payloads)
            for mutation in batch:
                if self._snapshot.resources.get(mutation.id) != mutation.before:
                    raise SnapshotExpired("Target changed before writing")
                if mutation.after is None:
                    if mutation.before is None or mutation.payload is not None:
                        raise InvalidContent("Invalid resource deletion")
                    del descriptors[mutation.id]
                    del payloads[mutation.id]
                else:
                    try:
                        encoded = encode_payload(mutation.payload)
                    except PayloadError as exc:
                        raise InvalidContent("Invalid mutation payload") from exc
                    if (
                        hashlib.sha256(encoded).hexdigest()
                        != mutation.after.content_hash
                    ):
                        raise InvalidContent("Mutation payload hash mismatch")
                    descriptors[mutation.id] = mutation.after
                    payloads[mutation.id] = encoded
            snapshot = RemoteSnapshot(
                self._snapshot.sync_id, self._snapshot.revision + 1, descriptors
            )
            receipts = self._receipts.accepted(receipt)
            self._snapshot, self._payloads, self._receipts = (
                snapshot,
                payloads,
                receipts,
            )
            return receipt

    def recover(self) -> bool:
        """No durable transaction exists to recover."""
        return False
