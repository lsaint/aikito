"""Durable pending identity and atomic local completion, independent of transport.

Every operation holds the existing writer lock and recovers the local journal
first. A pending record contains the exact request and the original replica
state; it cannot be rebuilt from current resources. Network resolution belongs
to reconciliation, not this store.
"""

from __future__ import annotations

import base64
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Iterator

from ..config import get_inbox_path
from ..skill_state import WorkspaceWriterLock
from .remote_store import (
    CommitRequest,
    CommitResult,
    InvalidContent,
    ReceiptCursor,
    SnapshotExpired,
)
from .remote_wire import (
    decode_base64,
    decode_commit_request,
    decode_state_json,
    encode_commit_request,
    validate_commit_result,
)
from .replica_state import CompletionMarker, ReplicaState, load_replica_state
from .resource_state import (
    PENDING_COMMIT_STATE,
    REPLICA_POLICY,
    REPLICA_STATE,
    state_path,
)
from .transactions import (
    Change,
    StateUpdate,
    WorkspaceCoreError,
    apply,
    atomic_unlink,
    entry_type,
    recover,
)


class PendingCommitError(WorkspaceCoreError):
    """Pending identity or its local completion evidence cannot be trusted."""


def _canonical(value: dict) -> str:
    try:
        text = json.dumps(
            value,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        text.encode("utf-8")
        return text
    except (ValueError, TypeError, RecursionError) as exc:
        raise PendingCommitError("Invalid pending envelope encoding") from exc


@dataclass(frozen=True)
class PendingCommit:
    request: CommitRequest
    replica_state_text: str
    safe_resource_ids: tuple[str, ...]
    excluded_resource_ids: tuple[str, ...] = ()

    def __post_init__(self):
        encode_commit_request(self.request)
        state = ReplicaState.decode(self.replica_state_text)
        if (
            state.sync_id != self.request.expected.sync_id
            or state.replica_id != self.request.client_id
            or state.receipt_cursor != self.request.previous_receipt
            or state.revision > self.request.expected.revision
        ):
            raise PendingCommitError(
                "Pending request and replica identity/history disagree"
            )
        for name in ("safe_resource_ids", "excluded_resource_ids"):
            values = getattr(self, name)
            if not isinstance(values, (tuple, list)) or any(
                type(key) is not str or not key for key in values
            ):
                raise PendingCommitError("Invalid pending resource scope")
            if len(set(values)) != len(values):
                raise PendingCommitError("Duplicate pending resource scope")
            object.__setattr__(self, name, tuple(sorted(values)))
        safe, excluded = set(self.safe_resource_ids), set(self.excluded_resource_ids)
        if safe & excluded or not {m.id for m in self.request.mutations} <= safe:
            raise PendingCommitError(
                "Pending uploads must belong to the safe resource scope"
            )

    def encode(self) -> str:
        body = {
            "version": 1,
            "request": base64.b64encode(encode_commit_request(self.request)).decode(
                "ascii"
            ),
            "replica_state": self.replica_state_text,
            "safe_resource_ids": list(self.safe_resource_ids),
            "excluded_resource_ids": list(self.excluded_resource_ids),
        }
        return _canonical(
            {
                **body,
                "envelope_digest": hashlib.sha256(
                    _canonical(body).encode("utf-8")
                ).hexdigest(),
            }
        )

    @classmethod
    def decode(cls, text: str | bytes) -> PendingCommit:
        try:
            raw = decode_state_json(text)
            if type(raw) is not dict or set(raw) != {
                "version",
                "request",
                "replica_state",
                "safe_resource_ids",
                "excluded_resource_ids",
                "envelope_digest",
            }:
                raise PendingCommitError("Invalid pending envelope fields")
            if type(raw["version"]) is not int or raw["version"] != 1:
                raise PendingCommitError("Unsupported pending envelope version")
            digest = raw.pop("envelope_digest")
            if digest != hashlib.sha256(_canonical(raw).encode("utf-8")).hexdigest():
                raise PendingCommitError("Pending envelope checksum mismatch")
            if type(raw["request"]) is not str or type(raw["replica_state"]) is not str:
                raise PendingCommitError("Invalid pending request or replica encoding")
            if (
                type(raw["safe_resource_ids"]) is not list
                or type(raw["excluded_resource_ids"]) is not list
            ):
                raise PendingCommitError("Invalid pending resource scope")
            pending = cls(
                decode_commit_request(decode_base64(raw["request"])),
                raw["replica_state"],
                tuple(raw["safe_resource_ids"]),
                tuple(raw["excluded_resource_ids"]),
            )
            if (
                list(pending.safe_resource_ids) != raw["safe_resource_ids"]
                or list(pending.excluded_resource_ids) != raw["excluded_resource_ids"]
            ):
                raise PendingCommitError("Noncanonical pending resource scope")
            return pending
        except PendingCommitError:
            raise
        except (
            InvalidContent,
            WorkspaceCoreError,
            ValueError,
            TypeError,
            KeyError,
        ) as exc:
            raise PendingCommitError("Invalid pending commit state") from exc

    def completed_by(self, state: ReplicaState) -> bool:
        request = self.request
        return (
            state.sync_id == request.expected.sync_id
            and state.replica_id == request.client_id
            and state.revision >= request.expected.revision + 1
            and state.completion_marker
            == CompletionMarker(
                request.expected.sync_id, request.request_id, request.mutation_digest
            )
            and state.receipt_cursor
            == ReceiptCursor(request.request_id, request.mutation_digest)
        )


class PendingCommitStore:
    def __init__(self, local: Path, home: Path):
        self.local, self.home = (
            local.expanduser().resolve(),
            home.expanduser().resolve(),
        )

    def _policy(self):
        try:
            prefix = get_inbox_path(self.local).relative_to(self.local).as_posix()
        except ValueError:
            prefix = ""
        return replace(REPLICA_POLICY, inbox_prefix=prefix)

    @contextmanager
    def _locked(self) -> Iterator[None]:
        with WorkspaceWriterLock(self.home):
            recover((self.local,), policy=self._policy())
            yield

    def _load(self) -> PendingCommit | None:
        path = state_path(self.local, PENDING_COMMIT_STATE)
        if entry_type(path) == "missing":
            return None
        pending = PendingCommit.decode(path.read_bytes())
        state, text = load_replica_state(self.local)
        if state is None or (
            text != pending.replica_state_text and not pending.completed_by(state)
        ):
            raise PendingCommitError("Replica state changed outside pending completion")
        return pending

    def load(self) -> PendingCommit | None:
        with self._locked():
            return self._load()

    def persist(
        self,
        request: CommitRequest,
        *,
        safe_resource_ids: tuple[str, ...],
        excluded_resource_ids: tuple[str, ...] = (),
    ) -> PendingCommit:
        with self._locked():
            if self._load() is not None:
                raise PendingCommitError("Unresolved pending commit already exists")
            state, before = load_replica_state(self.local)
            if state is None:
                state = ReplicaState(request.expected.sync_id, request.client_id, 0, {})
            text = before if before is not None else state.encode()
            pending = PendingCommit(
                request, text, safe_resource_ids, excluded_resource_ids
            )
            state_path(self.local, PENDING_COMMIT_STATE, create=True)
            states = []
            if before is None:
                states.append(StateUpdate(0, REPLICA_STATE, None, text))
            states.append(StateUpdate(0, PENDING_COMMIT_STATE, None, pending.encode()))
            apply((self.local,), (), states=tuple(states), policy=self._policy())
            return pending

    def complete(
        self,
        pending: PendingCommit,
        result: CommitResult,
        state: ReplicaState,
        *,
        changes: tuple[Change, ...] = (),
        verify: Callable[[], None] | None = None,
    ) -> ReplicaState:
        """Commit verified local writes, Base, cursor and marker in one journal."""
        with self._locked():
            if self._load() != pending:
                raise PendingCommitError("Pending commit changed before completion")
            current, before = load_replica_state(self.local)
            validate_commit_result(pending.request, result)
            if pending.completed_by(current):
                return current
            if (
                state.sync_id != result.sync_id
                or state.replica_id != result.client_id
                or state.revision < max(current.revision, result.accepted_revision)
            ):
                raise PendingCommitError(
                    "Invalid completed replica identity or revision"
                )
            changed = {
                key
                for key in set(state.base) | set(current.base)
                if state.base.get(key) != current.base.get(key)
            }
            if not changed <= set(pending.safe_resource_ids):
                raise PendingCommitError(
                    "Completion changed Base outside the safe resource scope"
                )
            for mutation in pending.request.mutations:
                resource = state.base.get(mutation.id)
                after = mutation.after
                if (after is None and resource is not None) or (
                    after is not None
                    and (
                        resource is None
                        or (
                            resource.fingerprint,
                            resource.references,
                            resource.mode_fingerprint,
                        )
                        != (after.fingerprint, after.references, after.mode_fingerprint)
                    )
                ):
                    raise PendingCommitError(
                        "Upload Base differs from the confirmed descriptor"
                    )
            completed = replace(
                state,
                receipt_cursor=ReceiptCursor(result.request_id, result.mutation_digest),
                completion_marker=CompletionMarker(
                    result.sync_id, result.request_id, result.mutation_digest
                ),
            )
            apply(
                (self.local,),
                changes,
                states=(StateUpdate(0, REPLICA_STATE, before, completed.encode()),),
                verify=verify,
                policy=self._policy(),
            )
            return completed

    def clear(
        self, pending: PendingCommit, *, rejected: SnapshotExpired | None = None
    ) -> None:
        """Clear only after local completion or an explicit definite CAS rejection."""
        with self._locked():
            existing = self._load()
            if existing is not None and existing != pending:
                raise PendingCommitError("Pending commit changed before cleanup")
            state, _ = load_replica_state(self.local)
            if state is None or (
                not pending.completed_by(state)
                and not isinstance(rejected, SnapshotExpired)
            ):
                raise PendingCommitError(
                    "Pending commit is neither completed nor definitely rejected"
                )
            path = state_path(self.local, PENDING_COMMIT_STATE)
            if entry_type(path) != "missing":
                atomic_unlink(path)
