"""Resolve one durable request before planning any new mutations.

Recovery confirms only uploaded Base entries and never replays local writes or
fetches historical content. A missing receipt permits one exact retry, never a
new identity. Callers hold the local writer lock across recovery and replanning.
"""

from dataclasses import replace
from pathlib import Path

from .pending_commit import PendingCommitStore
from .remote_store import RecoveryRequired, RemoteStore, SnapshotExpired
from .remote_wire import validate_commit_result
from .replica_state import load_replica_state
from .resource_state import local_resource_for_id


def recover_pending(local: Path, remote: RemoteStore, home: Path) -> bool:
    store = PendingCommitStore(local, home)
    pending = store.load()
    if pending is None:
        return False
    state, _ = load_replica_state(local)
    if pending.completed_by(state):
        store.clear(pending)
        return True
    request = pending.request
    args = (
        request.expected.sync_id,
        request.client_id,
        request.request_id,
        request.mutation_digest,
    )
    try:
        result = remote.resolve_commit(*args)
    except RecoveryRequired:
        remote.recover()
        result = remote.resolve_commit(*args)
    if result is None:
        try:
            result = remote.commit(request)
        except SnapshotExpired as exc:
            store.clear(pending, rejected=exc)
            return True
    validate_commit_result(request, result)
    base = dict(state.base)
    for mutation in request.mutations:
        after = mutation.after
        if after is None:
            base.pop(mutation.id, None)
        else:
            base[mutation.id] = replace(
                local_resource_for_id(mutation.id, after.fingerprint),
                references=after.references,
                mode_fingerprint=after.mode_fingerprint,
            )
    completed = replace(
        state, base=base, revision=max(state.revision, result.accepted_revision)
    )
    store.complete(pending, result, completed)
    store.clear(pending)
    return True
