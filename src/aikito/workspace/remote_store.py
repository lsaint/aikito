"""Content-blind storage contract for portable reconciliation.

The protocol has no center path, locks, journals, or physical resource parts.
Attachment validation is local backend lifecycle, never serialized transport.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
import re
from types import MappingProxyType
from typing import Protocol

from .payload import ResourceDescriptor, ResourceMutation, ResourcePayload


class StoreError(ValueError):
    """A remote store operation failed without exposing content."""


class SnapshotExpired(StoreError):
    """Snapshot preconditions failed; legacy stores also use this for identity.

    Receipt-aware commit must use StoreIdentityMismatch for a changed center.
    """


class InvalidContent(StoreError):
    """Stored or submitted content violates integrity or backend defenses."""


class RecoveryRequired(StoreError):
    """An explicit backend lifecycle recovery must precede another plan."""


class StoreUnavailable(StoreError):
    """The store cannot currently be reached or opened."""


class CommitOutcomeUnknown(StoreError):
    """Delivery may have committed; retain the request and resolve its outcome."""


class RequestIdentityMismatch(StoreError):
    """The latest receipt has this request identity but a different digest."""


class StoreIdentityMismatch(StoreError):
    """The requested center identity differs; retain pending and stop pairing."""


class ReplicaHistoryMismatch(StoreError):
    """The previous receipt no longer matches this client's accepted history."""


def _identity(value: str) -> None:
    if type(value) is not str or not value:
        raise InvalidContent("Invalid commit identity")


def _digest(value: str) -> None:
    if type(value) is not str or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise InvalidContent("Invalid commit digest")


@dataclass(frozen=True)
class RemoteSnapshot:
    sync_id: str
    revision: int
    resources: Mapping[str, ResourceDescriptor]

    def __post_init__(self):
        if (
            type(self.sync_id) is not str
            or not self.sync_id
            or type(self.revision) is not int
            or self.revision < 0
        ):
            raise InvalidContent("Invalid remote snapshot identity or revision")
        if any(
            type(key) is not str or not key or not isinstance(value, ResourceDescriptor)
            for key, value in self.resources.items()
        ):
            raise InvalidContent("Invalid remote resource descriptors")
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))


@dataclass(frozen=True)
class ReceiptCursor:
    request_id: str
    mutation_digest: str

    def __post_init__(self):
        _identity(self.request_id)
        _digest(self.mutation_digest)


@dataclass(frozen=True)
class CommitRequest:
    """A complete retryable batch; remote_wire verifies its claimed digest."""

    client_id: str
    request_id: str
    previous_receipt: ReceiptCursor | None
    expected: RemoteSnapshot
    mutations: tuple[ResourceMutation, ...]
    mutation_digest: str

    def __post_init__(self):
        _identity(self.client_id)
        _identity(self.request_id)
        _digest(self.mutation_digest)
        if (
            not isinstance(self.expected, RemoteSnapshot)
            or (
                self.previous_receipt is not None
                and not isinstance(self.previous_receipt, ReceiptCursor)
            )
            or not isinstance(self.mutations, (tuple, list))
            or not self.mutations
            or any(not isinstance(m, ResourceMutation) for m in self.mutations)
        ):
            raise InvalidContent("Invalid or empty commit request")
        batch = tuple(sorted(self.mutations, key=lambda m: m.id))
        if len({m.id for m in batch}) != len(batch):
            raise InvalidContent("Duplicate resource mutation ID")
        object.__setattr__(self, "mutations", batch)


@dataclass(frozen=True)
class CommitResult:
    """An immutable historical receipt, never a current remote snapshot."""

    sync_id: str
    client_id: str
    request_id: str
    mutation_digest: str
    accepted_revision: int
    result_digest: str

    def __post_init__(self):
        for value in (self.sync_id, self.client_id, self.request_id):
            _identity(value)
        _digest(self.mutation_digest)
        _digest(self.result_digest)
        if type(self.accepted_revision) is not int or self.accepted_revision < 1:
            raise InvalidContent("Invalid accepted revision")


class _RemoteAccess(Protocol):
    def validate_replica(self, local: Path) -> None:
        """Validate local attachment constraints; do not mutate or transmit paths."""
        ...

    def read(self) -> RemoteSnapshot: ...

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]: ...

    def recover(self) -> bool: ...


class RemoteStore(_RemoteAccess, Protocol):
    """Publish resources, revision and the client's latest receipt atomically.

    Validate the center, then check a matching latest receipt before CAS;
    a match returns its original result, with mismatched digests rejected.
    Otherwise check CAS and the previous receipt before publishing. Replaced
    historical requests are rejected by CAS, not resolved as current results.
    Each client identity must have one owner; cursors cannot detect full clones.
    """

    def commit(self, request: CommitRequest) -> CommitResult: ...

    def resolve_commit(
        self, sync_id: str, client_id: str, request_id: str, mutation_digest: str
    ) -> CommitResult | None:
        """Return a matching latest receipt; None never authorizes a new request.

        Matching ID with a different digest raises RequestIdentityMismatch.
        Identity mismatch, unavailable storage and unrecovered journals raise
        store errors. Lookup must serialize with atomic commit publication.
        """
        ...


class LegacyRemoteStore(_RemoteAccess, Protocol):
    """Temporary interface until backends and reconciliation adopt receipts."""

    def commit(
        self, expected: RemoteSnapshot, mutations: Sequence[ResourceMutation]
    ) -> RemoteSnapshot: ...
