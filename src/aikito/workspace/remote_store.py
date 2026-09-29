"""Content-blind storage contract for portable reconciliation.

The protocol has no center path, locks, journals, or physical resource parts.
Attachment validation is local backend lifecycle, never serialized transport.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol

from .payload import ResourceDescriptor, ResourceMutation, ResourcePayload


class StoreError(ValueError):
    """A remote store operation failed without exposing content."""


class SnapshotExpired(StoreError):
    """The expected center identity or generation is no longer current."""


class InvalidContent(StoreError):
    """Stored or submitted content violates integrity or backend defenses."""


class RecoveryRequired(StoreError):
    """An explicit backend lifecycle recovery must precede another plan."""


class StoreUnavailable(StoreError):
    """The store cannot currently be reached or opened."""


@dataclass(frozen=True)
class RemoteSnapshot:
    sync_id: str
    generation: int
    resources: Mapping[str, ResourceDescriptor]

    def __post_init__(self):
        if (
            type(self.sync_id) is not str
            or not self.sync_id
            or type(self.generation) is not int
            or self.generation < 0
        ):
            raise InvalidContent("Invalid remote snapshot identity or generation")
        if any(
            type(key) is not str or not key or not isinstance(value, ResourceDescriptor)
            for key, value in self.resources.items()
        ):
            raise InvalidContent("Invalid remote resource descriptors")
        object.__setattr__(self, "resources", MappingProxyType(dict(self.resources)))


class RemoteStore(Protocol):
    def validate_replica(self, local: Path) -> None:
        """Validate local attachment constraints; do not mutate or transmit paths."""
        ...

    def read(self) -> RemoteSnapshot: ...

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]: ...

    def commit(
        self, expected: RemoteSnapshot, mutations: Sequence[ResourceMutation]
    ) -> RemoteSnapshot: ...

    def recover(self) -> bool: ...
