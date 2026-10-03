"""Select a deterministic, dependency-safe round before fetching any payloads.

Only cycles need atomic grouping. Existing reference targets allow independent
updates; deletions wait for operations that remove incoming references. Wire
budgets include shared-config validation, envelopes and the full manifest.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from . import remote_limits
from .remote_protocol import (
    commit_envelope_size,
    descriptor_entry_size,
    fetch_entry_size,
    json_string_size,
    mutation_size,
    encode_fetch_request,
    encode_fetch_response,
    encode_read_response,
)
from .remote_store import InvalidContent

if TYPE_CHECKING:
    from .reconcile import ReconcileItem
    from .remote_store import RemoteSnapshot, ReceiptCursor
    from .payload import ResourceDescriptor


CHANGES = frozenset({"CREATE", "UPDATE", "DELETE"})


def operation_dependencies(items, local, remote) -> dict[str, set[str]]:
    operations = {item.id: item for item in items if item.action in CHANGES}
    dependencies = {key: set() for key in operations}
    for key, item in operations.items():
        source, target = (remote, local) if item.target == "local" else (local, remote)
        if item.after is not None:
            for reference in source[key].references:
                prerequisite = operations.get(reference)
                if reference not in target and prerequisite is not None:
                    if prerequisite.target == item.target:
                        dependencies[key].add(reference)
        else:
            for identity, resource in target.items():
                if key not in resource.references:
                    continue
                prerequisite = operations.get(identity)
                if prerequisite is not None and prerequisite.target == item.target:
                    if (
                        prerequisite.after is None
                        or key not in source[identity].references
                    ):
                        dependencies[key].add(identity)
    return dependencies


def atomic_groups(dependencies: dict[str, set[str]]) -> tuple[tuple[str, ...], ...]:
    """Iterative SCC discovery followed by deterministic dependency ordering."""
    seen, finished = set(), []
    for start in sorted(dependencies):
        stack = [(start, False)]
        while stack:
            node, exiting = stack.pop()
            if exiting:
                finished.append(node)
            elif node not in seen:
                seen.add(node)
                stack.append((node, True))
                stack.extend(
                    (key, False)
                    for key in sorted(dependencies[node], reverse=True)
                    if key not in seen
                )
    reverse = {key: set() for key in dependencies}
    for node, edges in dependencies.items():
        for edge in edges:
            reverse[edge].add(node)
    seen, groups = set(), []
    for start in reversed(finished):
        if start in seen:
            continue
        group, stack = [], [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            group.append(node)
            for edge in sorted(reverse[node], reverse=True):
                if edge not in seen:
                    seen.add(edge)
                    stack.append(edge)
        groups.append(tuple(sorted(group)))
    owner = {key: group for group in groups for key in group}
    required = {
        group: {owner[edge] for key in group for edge in dependencies[key]} - {group}
        for group in groups
    }
    followers = {group: set() for group in groups}
    for group, edges in required.items():
        for edge in edges:
            followers[edge].add(group)
    ready = [group for group in groups if not required[group]]
    heapq.heapify(ready)
    result = []
    while ready:
        group = heapq.heappop(ready)
        result.append(group)
        for follower in sorted(followers[group]):
            required[follower].remove(group)
            if not required[follower]:
                heapq.heappush(ready, follower)
    return tuple(result)


def _needs_config_fields(item) -> bool:
    return (
        (
            item.action in {"CREATE", "UPDATE"}
            or (item.action == "DELETE" and item.reason.startswith("Conflict resolved"))
        )
        and item.target is not None
        and item.id.startswith("config:")
    )


def needed_downloads(items, resources) -> set[str]:
    needed = {
        item.id
        for item in items
        if item.action in CHANGES and item.target == "local" and item.after is not None
    }
    if any(_needs_config_fields(item) for item in items):
        needed.update(key for key in resources if key.startswith("config:"))
    return needed


def fetch_response_size(snapshot, identities) -> int:
    return (
        len(encode_fetch_response({}))
        + sum(fetch_entry_size(key, snapshot.resources[key]) for key in identities)
        + max(0, len(identities) - 1)
    )


@dataclass(frozen=True)
class _Usage:
    upload_count: int = 0
    upload_bytes: int = 0
    download_count: int = 0
    response_entries: int = 0
    fetch_ids: int = 0
    manifest_delta: int = 0
    resource_delta: int = 0

    def plus(self, other):
        return _Usage(
            *(
                getattr(self, key) + getattr(other, key)
                for key in self.__dataclass_fields__
            )
        )


class _RoundBudget:
    """Accumulate exact wire costs; payload bytes never enter this calculation."""

    def __init__(self, snapshot, descriptors, cursor):
        self.commit_base = commit_envelope_size(snapshot, cursor)
        self.fetch_base = len(encode_fetch_request(snapshot, ()))
        self.read_base = len(encode_read_response(snapshot))
        self.response_base = len(encode_fetch_response({}))
        self.resource_count = len(snapshot.resources)
        self.revision_delta = len(str(snapshot.revision + 1)) - len(
            str(snapshot.revision)
        )
        self.download_costs = {
            key: (fetch_entry_size(key, descriptor), json_string_size(key))
            for key, descriptor in snapshot.resources.items()
        }
        self.config_ids = {
            key for key in snapshot.resources if key.startswith("config:")
        }
        self.config_cost = _Usage(
            download_count=len(self.config_ids),
            response_entries=sum(
                self.download_costs[key][0] for key in self.config_ids
            ),
            fetch_ids=sum(self.download_costs[key][1] for key in self.config_ids),
        )
        self.upload_costs = {}
        for key, after in descriptors.items():
            before = snapshot.resources.get(key)
            self.upload_costs[key] = _Usage(
                upload_count=1,
                upload_bytes=mutation_size(key, before, after),
                manifest_delta=(descriptor_entry_size(key, after) if after else 0)
                - (descriptor_entry_size(key, before) if before else 0),
                resource_delta=int(after is not None) - int(before is not None),
            )
        self.usage = _Usage()
        self.config_loaded = False

    def group_cost(self, candidates, *, standalone=False):
        cost = _Usage()
        for item in candidates:
            if item.id in self.upload_costs:
                cost = cost.plus(self.upload_costs[item.id])
            if (
                item.target == "local"
                and item.after is not None
                and not item.id.startswith("config:")
            ):
                response, identity = self.download_costs[item.id]
                cost = cost.plus(
                    _Usage(
                        download_count=1, response_entries=response, fetch_ids=identity
                    )
                )
        config = any(_needs_config_fields(item) for item in candidates)
        if config and (standalone or not self.config_loaded):
            cost = cost.plus(self.config_cost)
        return cost, config

    def fits(self, usage):
        if (
            self.fetch_base + usage.fetch_ids + max(0, usage.download_count - 1)
            > remote_limits.MAX_REMOTE_REQUEST_BYTES
        ):
            return False
        if (
            self.response_base
            + usage.response_entries
            + max(0, usage.download_count - 1)
            > remote_limits.MAX_REMOTE_RESPONSE_BYTES
        ):
            return False
        if usage.upload_count:
            if (
                self.commit_base + usage.upload_bytes + usage.upload_count - 1
                > remote_limits.MAX_REMOTE_REQUEST_BYTES
            ):
                return False
            manifest_delta = (
                usage.manifest_delta
                + self.revision_delta
                + max(0, self.resource_count + usage.resource_delta - 1)
                - max(0, self.resource_count - 1)
            )
            if (
                self.read_base + manifest_delta
                > remote_limits.MAX_REMOTE_RESPONSE_BYTES
                or self.fetch_base + manifest_delta
                > remote_limits.MAX_REMOTE_REQUEST_BYTES
            ):
                return False
        return True


def check_manifest(snapshot) -> None:
    if (
        len(encode_read_response(snapshot)) > remote_limits.MAX_REMOTE_RESPONSE_BYTES
        or len(encode_fetch_request(snapshot, ()))
        > remote_limits.MAX_REMOTE_REQUEST_BYTES
    ):
        raise InvalidContent(
            "Remote descriptor manifest exceeds the wire size limit; resource count requires a separate protocol design"
        )


def select_round(
    items: list[ReconcileItem],
    dependencies: dict[str, set[str]],
    snapshot: RemoteSnapshot,
    descriptors: dict[str, ResourceDescriptor | None],
    cursor: ReceiptCursor | None,
) -> list[ReconcileItem]:
    check_manifest(snapshot)
    prospective = dict(snapshot.resources)
    for key, after in descriptors.items():
        if after is None:
            prospective.pop(key, None)
        else:
            prospective[key] = after
    check_manifest(
        replace(
            snapshot,
            resources=prospective,
            revision=snapshot.revision + bool(descriptors),
        )
    )
    by_id = {item.id: item for item in items}
    selected = set()
    budget = _RoundBudget(snapshot, descriptors, cursor)
    for group in atomic_groups(dependencies):
        active = {key for key in group if by_id[key].action in CHANGES}
        if not active:
            continue
        required = set().union(*(dependencies[key] for key in group)) - set(group)
        if len(active) != len(group) or not required <= selected:
            for key in active:
                by_id[key] = replace(
                    by_id[key],
                    action="DEFERRED",
                    reason="Waiting for prerequisite operations",
                )
            continue
        candidates = [by_id[key] for key in group]
        cost, config = budget.group_cost(candidates)
        combined = budget.usage.plus(cost)
        if budget.fits(combined):
            selected.update(active)
            budget.usage = combined
            budget.config_loaded |= config
        else:
            standalone, _ = budget.group_cost(candidates, standalone=True)
            action = "DEFERRED" if budget.fits(standalone) else "BLOCKED"
            for key in active:
                by_id[key] = replace(
                    by_id[key],
                    action=action,
                    target=by_id[key].target if action == "DEFERRED" else None,
                    reason="Round wire budget exhausted"
                    if action == "DEFERRED"
                    else "Atomic operation group exceeds the remote wire budget",
                )
    return [by_id[item.id] for item in items]


def defer_dependents(items, dependencies):
    by_id = {item.id: item for item in items}
    for group in atomic_groups(dependencies):
        unavailable_group = {key for key in group if by_id[key].action not in CHANGES}
        unavailable = bool(unavailable_group)
        required = set().union(*(dependencies[key] for key in group)) - set(group)
        prerequisites_unavailable = any(
            by_id[key].action not in CHANGES for key in required
        )
        unavailable |= prerequisites_unavailable
        if unavailable:
            for key in group:
                if by_id[key].action in CHANGES or (
                    by_id[key].action == "CONFLICT"
                    and "references missing" in by_id[key].reason
                    and (prerequisites_unavailable or bool(unavailable_group - {key}))
                ):
                    by_id[key] = replace(
                        by_id[key],
                        action="DEFERRED",
                        reason="Waiting for prerequisite operations",
                    )
    return [by_id[item.id] for item in items]
