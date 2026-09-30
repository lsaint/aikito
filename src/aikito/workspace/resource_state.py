"""Client resource identity, replica state, and local filesystem policy.

These helpers decode logical resources for local writing. Stores may reuse
this mapping for their own plaintext layout, but the RemoteStore contract
never requires ID decoding or physical parts.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from ..compat import secure_directory_permissions
from ..templating import BUNDLED_SKILL_NAMES
from .transactions import (
    PathPolicy,
    WorkspaceCoreError,
    entry_type,
    validate_resource_path,
)
from .resources import (
    Resource,
    ResourcePart,
    SKILL_FINGERPRINT_SCHEME,
    is_ignored_name,
    physical_kind,
)

SYNC_KINDS = frozenset(
    {
        "memory",
        "project-memory",
        "skill",
        "inbox",
        "global-instructions",
        "project-instructions",
        "agent",
        "mcp",
        "subagent",
        "skill-selection",
        "project",
        "project-field",
        "project-path",
        "project-skill",
        "config",
    }
)
LOCAL_CONFIG = frozenset({"inbox.path"})
REMOTE_STATE = ".local/state/aikito/workspace-reconcile/remote.json"
REPLICA_STATE = ".local/state/aikito/workspace-reconcile/replica.json"
PENDING_COMMIT_STATE = ".local/state/aikito/workspace-reconcile/pending.json"
RECONCILE_POLICY = PathPolicy(
    states=(REMOTE_STATE, REPLICA_STATE, PENDING_COMMIT_STATE),
    create_parents=True,
    inbox_prefix="inbox",
)


def state_path(root: Path, relative: str, *, create: bool = False) -> Path:
    """Resolve private state without following unsafe directory entries."""
    current = root
    for part in Path(relative).parts[:-1]:
        current /= part
        kind = entry_type(current)
        if kind == "missing" and create:
            current.mkdir(mode=0o700)
            if not secure_directory_permissions(current):
                raise WorkspaceCoreError(f"Cannot secure state directory: {current}")
        elif kind == "missing":
            return root / relative
        elif kind != "directory":
            raise WorkspaceCoreError(f"Unsafe state directory: {current}")
    path = root / relative
    if entry_type(path) not in ("missing", "file"):
        raise WorkspaceCoreError(f"Unsafe state file: {path}")
    return path


def local_resource_for_id(identity: str, fingerprint: str) -> Resource:
    """Decode logical IDs without treating set members as physical paths."""
    kind, separator, name = identity.partition(":")
    if not separator or not name or kind not in SYNC_KINDS:
        raise WorkspaceCoreError(f"Unsupported resource ID: {identity}")
    references = ()
    table = ""
    project, _, member = name.partition("/")
    if kind == "project" and "/" in name:
        raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
    if kind == "project-field" and member in {"path", "paths", "skills"}:
        raise WorkspaceCoreError(f"Invalid project field ID: {identity}")
    if kind == "config":
        if name in LOCAL_CONFIG:
            raise WorkspaceCoreError(f"Host-local resource ID: {identity}")
        path, table = "config.toml", name.rpartition(".")[0]
    elif kind == "skill-selection":
        path = "skills.toml"
        references = (f"skill:{name}",)
    elif kind in {"project", "project-field", "project-path", "project-skill"}:
        if kind != "project" and not member:
            raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
        path = f"projects/{project}/agent.toml"
        if kind != "project":
            references = (f"project:{project}",)
        if kind == "project-skill":
            references += (f"skill:{member}",)
    elif kind == "project-memory":
        if not member:
            raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
        path = f"projects/{project}/memory/{member}"
        references = (f"project:{project}",)
    elif kind == "project-instructions":
        path = f"projects/{name}/AGENTS.md"
        references = (f"project:{name}",)
    elif kind == "global-instructions":
        if name != "AGENTS.md":
            raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
        path = "global/AGENTS.md"
    elif kind in {"agent", "mcp", "subagent"}:
        area = {"agent": "agents", "mcp": "mcps", "subagent": "subagents"}[kind]
        suffix = "md" if kind == "subagent" else "toml"
        path = f"{area}/{name}.{suffix}"
    else:
        area = {"skill": "skills", "memory": "memory", "inbox": "inbox"}[kind]
        path = f"{area}/{name}"
    validate_resource_path(path, physical_kind(kind), RECONCILE_POLICY)
    if any(is_ignored_name(part) for part in Path(path).parts):
        raise WorkspaceCoreError(f"Excluded resource ID: {identity}")
    empty = kind in {"project", "project-path", "project-skill", "skill-selection"}
    if not isinstance(fingerprint, str) or (
        fingerprint != ""
        if empty
        else len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise WorkspaceCoreError(f"Invalid resource fingerprint: {identity}")
    # Bundled skills are virtual providers and never center content.
    references = tuple(
        reference
        for reference in references
        if not (reference.startswith("skill:") and reference[6:] in BUNDLED_SKILL_NAMES)
    )
    return Resource(kind, name, fingerprint, (ResourcePart(path, table),), references)


def decode_resources(raw: object) -> dict[str, Resource]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise WorkspaceCoreError("Invalid resource state")
    resources = {}
    for key, value in raw.items():
        resource = local_resource_for_id(
            key, value.get("fingerprint") if isinstance(value, dict) else value
        )
        if isinstance(value, dict):
            mode = value.get("mode_fingerprint")
            if mode is not None:
                if (
                    resource.kind != "skill"
                    or type(mode) is not str
                    or len(mode) != 64
                    or any(char not in "0123456789abcdef" for char in mode)
                ):
                    raise WorkspaceCoreError("Invalid skill mode fingerprint")
                resource = replace(resource, mode_fingerprint=mode)
            references = value.get("references")
            if not isinstance(references, list) or any(
                not isinstance(ref, str) for ref in references
            ):
                raise WorkspaceCoreError("Invalid resource references")
            if (
                resource.kind not in {"mcp", "subagent"}
                and tuple(references) != resource.references
            ):
                raise WorkspaceCoreError("Invalid resource references")
            resource = replace(resource, references=tuple(references))
        resources[key] = resource
    return resources


def encode_resources(resources: dict[str, Resource]) -> dict[str, object]:
    return {
        key: (
            {
                "fingerprint": resource.fingerprint,
                "references": list(resource.references),
                "mode_fingerprint": resource.mode_fingerprint,
            }
            if resource.kind == "skill" and resource.mode_fingerprint is not None
            else resource.fingerprint
        )
        for key, resource in sorted(resources.items())
    }


def decode_revision(state: dict[str, object]) -> int:
    """Read the required nonnegative revision without rewriting state."""
    revision = state.get("revision")
    if type(revision) is not int or revision < 0:
        raise WorkspaceCoreError("Invalid resource state revision")
    return revision


def validate_skill_fingerprint_scheme(
    scheme: object, resources: dict[str, Resource]
) -> None:
    """Refuse ambiguous old skill hashes without rewriting historical state."""
    if scheme == SKILL_FINGERPRINT_SCHEME:
        return
    if scheme is None and not any(r.kind == "skill" for r in resources.values()):
        return
    raise WorkspaceCoreError(
        "Unsupported skill fingerprint scheme; preserve the existing center and "
        "replica Base, then explicitly create and pair a new resource center"
    )


def valid_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


REPLICA_POLICY = PathPolicy(
    states=(REPLICA_STATE, PENDING_COMMIT_STATE),
    create_parents=True,
    inbox_prefix="inbox",
)
