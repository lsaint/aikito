"""Replica identity, shared Base and local completion evidence.

The optional cursor and completion marker are committed with Base through the
local journal. Loading never changes state or requires a remote connection.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

from .remote_store import InvalidContent, ReceiptCursor
from .remote_wire import decode_state_json
from .resource_state import (
    REPLICA_STATE,
    decode_resources,
    decode_revision,
    encode_resources,
    state_path,
    valid_identity,
    validate_skill_fingerprint_scheme,
)
from .resources import Resource, SKILL_FINGERPRINT_SCHEME
from .transactions import WorkspaceCoreError, entry_type


def encode_cursor(cursor: ReceiptCursor | None) -> dict | None:
    return (
        None
        if cursor is None
        else {
            "request_id": cursor.request_id,
            "mutation_digest": cursor.mutation_digest,
        }
    )


def decode_cursor(raw: object) -> ReceiptCursor | None:
    if raw is None:
        return None
    if type(raw) is not dict or set(raw) != {"request_id", "mutation_digest"}:
        raise WorkspaceCoreError("Invalid replica receipt cursor")
    try:
        return ReceiptCursor(raw["request_id"], raw["mutation_digest"])
    except InvalidContent as exc:
        raise WorkspaceCoreError("Invalid replica receipt cursor") from exc


@dataclass(frozen=True)
class CompletionMarker:
    sync_id: str
    request_id: str
    mutation_digest: str

    def __post_init__(self):
        if type(self.sync_id) is not str or not self.sync_id:
            raise WorkspaceCoreError("Invalid completion identity")
        try:
            ReceiptCursor(self.request_id, self.mutation_digest)
        except InvalidContent as exc:
            raise WorkspaceCoreError("Invalid completion request identity") from exc


@dataclass(frozen=True)
class ReplicaState:
    sync_id: str
    replica_id: str
    revision: int
    base: Mapping[str, Resource]
    receipt_cursor: ReceiptCursor | None = None
    completion_marker: CompletionMarker | None = None
    unpaired_ids: frozenset[str] = frozenset()

    def __post_init__(self):
        if (
            type(self.sync_id) is not str
            or not self.sync_id
            or not valid_identity(self.replica_id)
            or type(self.revision) is not int
            or self.revision < 0
            or (
                self.receipt_cursor is not None
                and not isinstance(self.receipt_cursor, ReceiptCursor)
            )
        ):
            raise WorkspaceCoreError("Invalid replica state")
        if not isinstance(self.unpaired_ids, frozenset) or any(
            type(key) is not str or not key for key in self.unpaired_ids
        ):
            raise WorkspaceCoreError("Invalid unpaired resource IDs")
        marker = self.completion_marker
        if marker is not None and (
            not isinstance(marker, CompletionMarker)
            or marker.sync_id != self.sync_id
            or self.receipt_cursor
            != ReceiptCursor(marker.request_id, marker.mutation_digest)
        ):
            raise WorkspaceCoreError("Replica completion marker and cursor disagree")
        object.__setattr__(self, "base", MappingProxyType(dict(self.base)))

    def encode(self) -> str:
        marker = self.completion_marker
        return json.dumps(
            {
                "version": 2,
                "skill_fingerprint": SKILL_FINGERPRINT_SCHEME,
                "sync_id": self.sync_id,
                "replica_id": self.replica_id,
                "revision": self.revision,
                "base": encode_resources(self.base),
                "unpaired_ids": sorted(self.unpaired_ids),
                **(
                    {"receipt_cursor": encode_cursor(self.receipt_cursor)}
                    if self.receipt_cursor is not None
                    else {}
                ),
                **(
                    {
                        "completion_marker": {
                            "sync_id": marker.sync_id,
                            "request_id": marker.request_id,
                            "mutation_digest": marker.mutation_digest,
                        }
                    }
                    if marker is not None
                    else {}
                ),
            },
            sort_keys=True,
        )

    @classmethod
    def decode(cls, text: str) -> ReplicaState:
        try:
            raw = decode_state_json(text)
            if (
                type(raw) is not dict
                or type(raw.get("version")) is not int
                or raw["version"] != 2
            ):
                raise WorkspaceCoreError("Unsupported replica state version")
            base = decode_resources(raw.get("base"))
            validate_skill_fingerprint_scheme(raw.get("skill_fingerprint"), base)
            marker = None
            if "completion_marker" in raw:
                value = raw["completion_marker"]
                if type(value) is not dict or set(value) != {
                    "sync_id",
                    "request_id",
                    "mutation_digest",
                }:
                    raise WorkspaceCoreError("Invalid replica completion marker")
                marker = CompletionMarker(
                    value["sync_id"], value["request_id"], value["mutation_digest"]
                )
            unpaired = raw.get("unpaired_ids", [])
            if (
                type(unpaired) is not list
                or any(type(key) is not str or not key for key in unpaired)
                or unpaired != sorted(set(unpaired))
            ):
                raise WorkspaceCoreError("Invalid unpaired resource IDs")
            return cls(
                raw["sync_id"],
                raw["replica_id"],
                decode_revision(raw),
                base,
                decode_cursor(raw.get("receipt_cursor")),
                marker,
                frozenset(unpaired),
            )
        except WorkspaceCoreError:
            raise
        except (InvalidContent, ValueError, TypeError, KeyError) as exc:
            raise WorkspaceCoreError("Invalid replica state") from exc


def load_replica_state(
    local: Path,
    *,
    sync_id: str | None = None,
    max_revision: int | None = None,
) -> tuple[ReplicaState | None, str | None]:
    legacy = state_path(local, ".local/state/aikito/workspace-reconcile/baseline.json")
    if entry_type(legacy) != "missing":
        raise WorkspaceCoreError(
            "Unsupported baseline format; remove the old internal baseline and pair again"
        )
    path = state_path(local, REPLICA_STATE)
    if entry_type(path) == "missing":
        return None, None
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeError as exc:
        raise WorkspaceCoreError("Invalid replica state text encoding") from exc
    state = ReplicaState.decode(text)
    if sync_id is not None and state.sync_id != sync_id:
        raise WorkspaceCoreError("Replica belongs to a different resource center")
    if max_revision is not None and state.revision > max_revision:
        raise WorkspaceCoreError("Invalid replica state")
    return state, text
