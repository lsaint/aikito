"""Validated collection policy for explicitly delegated workspace resources.

This policy defines the default collection layout. Existing Vault data must be
converted by local maintenance tooling before synchronization resumes.
"""

from __future__ import annotations

from dataclasses import dataclass

from .resource_state import local_resource_for_id

GLOBAL_COLLECTIONS = {
    "global/instructions": "global-instructions",
    "global/memory": "global-memory",
    "skills": "skill",
    "subagents": "subagent",
    "inbox": "inbox",
}
_PRIVATE_KINDS = frozenset(
    {
        "mcp",
        "agent",
        "config",
        "skill-selection",
        "project",
        "project-field",
        "project-path",
        "project-skill",
    }
)
_MEMBER_KINDS = frozenset(
    {"skill-selection", "project", "project-path", "project-skill"}
)


@dataclass(frozen=True)
class DelegableCollection:
    id: str
    kind: str
    project: str | None = None

    @property
    def parent_id(self) -> str | None:
        return None if self.project is None else f"project:{self.project}"


def parse_collection(collection_id: str) -> DelegableCollection:
    if type(collection_id) is not str:
        raise ValueError("Invalid collection")
    if collection_id in GLOBAL_COLLECTIONS:
        return DelegableCollection(collection_id, GLOBAL_COLLECTIONS[collection_id])
    parts = collection_id.split("/")
    if (
        len(parts) != 3
        or parts[0] != "project"
        or parts[2] not in {"instructions", "memory"}
    ):
        raise ValueError("Collection cannot be delegated")
    local_resource_for_id(f"project:{parts[1]}", "")
    kind = "project-instructions" if parts[2] == "instructions" else "project-memory"
    return DelegableCollection(collection_id, kind, parts[1])


def collection_for_resource(logical_id: str) -> str:
    if type(logical_id) is not str:
        raise ValueError("Invalid resource identity")
    kind = logical_id.partition(":")[0]
    resource = local_resource_for_id(
        logical_id, "" if kind in _MEMBER_KINDS else "0" * 64
    )
    if kind in _PRIVATE_KINDS:
        return "private"
    for collection_id, expected_kind in GLOBAL_COLLECTIONS.items():
        if kind == expected_kind:
            return collection_id
    if kind == "project-instructions":
        return f"project/{resource.name}/instructions"
    if kind == "project-memory":
        return f"project/{resource.name.partition('/')[0]}/memory"
    raise ValueError("Unsupported resource identity")
