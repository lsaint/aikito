"""Import policy over the shared three-way comparison.

Imports treat bundled templates as the common ancestor, the target as the
local side, and the source as the remote side. Only the target is written,
and a resource absent from the target is always created: without shared
history its absence cannot be a deliberate deletion.
"""

from __future__ import annotations

from .merge import compare
from .resources import Resource
from .templates import template_fingerprints


def decide_resource(
    resource: Resource, target_resources: dict[str, Resource]
) -> tuple[str, str]:
    """Return the import action and reason for one source resource."""
    current = target_resources.get(resource.id)
    if current is None:
        return "CREATE", "Resource is absent from target"
    if current.fingerprint == resource.fingerprint:
        return "NOOP", "Contents match"
    base = template_fingerprints(resource.id)
    outcome = compare(base, current.fingerprint, resource.fingerprint)
    if outcome.action == "NOOP":
        return "NOOP", "Both sides are unmodified templates"
    if outcome.target == "remote":
        return "NOOP", "Target is customized; source matches the template"
    if outcome.target == "local":
        return "UPDATE", "Replace unmodified template with source"
    if base:
        return "CONFLICT", "Both sides differ from the template"
    return "CONFLICT", "Contents differ"
