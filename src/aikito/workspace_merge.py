"""Three-way comparison of one logical resource.

Callers supply fingerprints only, so the same rules serve imports, which
use bundled templates as the common ancestor, and reconciliation, which uses
the last agreed snapshot. The comparison never touches the filesystem.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Outcome:
    action: str  # CREATE, UPDATE, DELETE, NOOP, or CONFLICT
    target: str | None  # "local" or "remote" for changes, otherwise None
    reason: str


def compare(base: frozenset[str], local: str | None, remote: str | None) -> Outcome:
    """Decide one resource; ``base`` holds every fingerprint counted as unmodified.

    An empty ``base`` means the resource had no common ancestor, so only a
    missing side counts as unmodified. ``None`` marks a missing side.
    """
    if local == remote:
        return Outcome("NOOP", None, "Both sides agree")
    local_same = local in base if local is not None else not base
    remote_same = remote in base if remote is not None else not base
    if local_same and remote_same:
        return Outcome("NOOP", None, "Both sides are unmodified")
    if local_same:
        return Outcome(_action(local, remote), "local", "Remote changed")
    if remote_same:
        return Outcome(_action(remote, local), "remote", "Local changed")
    return Outcome("CONFLICT", None, "Both sides changed")


def _action(current: str | None, new: str | None) -> str:
    if new is None:
        return "DELETE"
    return "CREATE" if current is None else "UPDATE"
