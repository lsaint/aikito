"""Filesystem resource center with conditional, recoverable batch commits.

The center is a resource store, not a workspace. Its manifest names logical
resources and a monotonically increasing generation; consumers obtain content
by resource ID instead of depending on a workspace directory layout.
"""

from __future__ import annotations

import json
import tempfile
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Iterator

from ..templating import BUNDLED_SKILL_NAMES
from ..compat import is_windows, secure_directory_permissions, secure_file_permissions
from .transactions import (
    PathPolicy,
    StateUpdate,
    WorkspaceCoreError,
    apply,
    entry_type,
    has_pending,
    recover,
    validate_resource_path,
)
from .resource_write import (
    ResourceContent,
    ResourceWrite,
    prepare_resource_writes,
    credential_resources,
    missing_references,
    toml_conflicts,
    verify_resource_snapshot,
)
from .resources import (
    Resource,
    ResourcePart,
    WorkspaceResourceError,
    WorkspaceSnapshot,
    is_ignored_name,
    physical_kind,
    is_shared_resource,
    inspect_resource_content,
    value_fingerprint,
    SKILL_FINGERPRINT_SCHEME,
)

from .toml_render import TomlValue

if is_windows():
    import msvcrt
else:
    import fcntl


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
RECONCILE_POLICY = PathPolicy(
    states=(REMOTE_STATE, REPLICA_STATE), create_parents=True, inbox_prefix="inbox"
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


def resource_for_id(identity: str, fingerprint: str) -> Resource:
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
        resource = resource_for_id(
            key, value.get("fingerprint") if isinstance(value, dict) else value
        )
        if isinstance(value, dict):
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


def encode_resources(resources: dict[str, Resource]) -> dict[str, str]:
    return {key: resource.fingerprint for key, resource in sorted(resources.items())}


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


@dataclass(frozen=True)
class RemoteSnapshot:
    sync_id: str
    generation: int
    resources: dict[str, Resource]
    values: dict[str, TomlValue] = field(default_factory=dict)


class FilesystemRemote:
    """Read by ID and commit a complete batch against one expected generation."""

    _mutex = threading.RLock()

    def __init__(self, root: Path):
        self.root = root.expanduser().resolve()
        self._depth = 0

    @classmethod
    def create(cls, root: Path) -> FilesystemRemote:
        remote = cls(root)
        if entry_type(remote.root) == "missing":
            remote.root.mkdir(parents=True, mode=0o700)
        if entry_type(remote.root) != "directory" or any(remote.root.iterdir()):
            raise WorkspaceCoreError("Resource center requires an empty directory")
        state_path(remote.root, REMOTE_STATE, create=True)
        with remote.lock():
            initial = RemoteSnapshot(uuid.uuid4().hex, 0, {})
            apply(
                (remote.root,),
                (),
                states=(StateUpdate(0, REMOTE_STATE, None, remote.encode(initial)),),
                policy=RECONCILE_POLICY,
            )
        return remote

    @contextmanager
    def lock(self) -> Iterator[None]:
        """Serialize center writers across processes and threads."""
        with self._mutex:
            if self._depth:
                self._depth += 1
                try:
                    yield
                finally:
                    self._depth -= 1
                return
            lock_path = state_path(self.root, REMOTE_STATE).parent / "remote.lock"
            if entry_type(lock_path) not in ("file", "missing"):
                raise WorkspaceCoreError("Unsafe resource center lock")
            with lock_path.open("a+", encoding="utf-8") as stream:
                if not secure_file_permissions(lock_path):
                    raise WorkspaceCoreError("Cannot secure resource center lock")
                if is_windows():
                    if stream.tell() == 0:
                        stream.write("0")
                        stream.flush()
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_LOCK, 1)
                else:
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
                self._depth = 1
                try:
                    yield
                finally:
                    self._depth = 0
                    if is_windows():
                        stream.seek(0)
                        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        fcntl.flock(stream.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def encode(snapshot: RemoteSnapshot) -> str:
        return json.dumps(
            {
                "version": 2,
                "skill_fingerprint": SKILL_FINGERPRINT_SCHEME,
                "sync_id": snapshot.sync_id,
                "generation": snapshot.generation,
                "resources": {
                    key: {
                        "fingerprint": resource.fingerprint,
                        "references": list(resource.references),
                    }
                    for key, resource in sorted(snapshot.resources.items())
                },
                "values": {
                    key: value.encode()
                    for key, value in sorted(snapshot.values.items())
                },
            },
            sort_keys=True,
        )

    def verify_contents(
        self,
        resources: dict[str, Resource],
        values: dict[str, TomlValue] | None = None,
        *,
        legacy: bool = False,
    ) -> WorkspaceSnapshot:
        values = values or {}
        scalar_ids = {
            key
            for key, resource in resources.items()
            if resource.kind in {"config", "project-field"}
        }
        if set(values) != scalar_ids:
            raise WorkspaceCoreError("Invalid center field values")
        for key, value in values.items():
            resource = resources[key]
            valid_path = (
                ".".join(value.path) == resource.name
                if resource.kind == "config"
                else value.path == (resource.name.partition("/")[2],)
            )
            if (
                not valid_path
                or (resource.kind == "config" and isinstance(value.value, dict))
                or value_fingerprint(value.value) != resource.fingerprint
            ):
                raise WorkspaceCoreError(f"Invalid center field value: {key}")
        payload = ResourceContent(resources, {}, values=values)
        if toml_conflicts(payload, ResourceContent({}, {}), set(values), set()):
            raise WorkspaceCoreError("Overlapping center configuration fields")
        allowed = {
            "memory",
            "projects",
            "inbox",
            "global",
            "agents",
            "subagents",
            "mcps",
            "skills",
            ".local",
            ".git",
        }
        for path in self.root.iterdir():
            if not is_ignored_name(path.name) and path.name not in allowed:
                raise WorkspaceCoreError(f"Unmanaged center content: {path}")
        paths = {
            resource.parts[0].path
            for resource in resources.values()
            if not is_shared_resource(resource.kind)
        }

        def check_files(directory: Path) -> None:
            kind = entry_type(directory)
            if kind == "missing":
                return
            if kind != "directory":
                raise WorkspaceCoreError(f"Unsafe center directory: {directory}")
            for child in directory.iterdir():
                if is_ignored_name(child.name):
                    continue
                kind = entry_type(child)
                if kind == "directory":
                    check_files(child)
                elif (
                    kind != "file"
                    or child.relative_to(self.root).as_posix() not in paths
                ):
                    raise WorkspaceCoreError(
                        f"Unmanaged or unsafe center content: {child}"
                    )

        for area in (
            "memory",
            "projects",
            "inbox",
            "global",
            "agents",
            "subagents",
            "mcps",
        ):
            check_files(self.root / area)
        skills = self.root / "skills"
        if entry_type(skills) not in ("missing", "directory"):
            raise WorkspaceCoreError("Unsafe center skills directory")
        if entry_type(skills) == "directory":
            for child in skills.iterdir():
                if not is_ignored_name(child.name) and (
                    entry_type(child) != "directory"
                    or child.relative_to(self.root).as_posix() not in paths
                ):
                    raise WorkspaceCoreError(
                        f"Unmanaged or unsafe center content: {child}"
                    )
        for resource in resources.values():
            if is_shared_resource(resource.kind):
                continue
            try:
                logical, references = inspect_resource_content(
                    resource, self.root / resource.parts[0].path
                )
            except WorkspaceResourceError as exc:
                raise WorkspaceCoreError(str(exc)) from exc
            if logical != resource.fingerprint or references != resource.references:
                raise WorkspaceCoreError(
                    f"Resource center content changed: {resource.id}"
                )
        external = (
            frozenset(
                ref
                for resource in resources.values()
                for ref in resource.references
                if ref.startswith("project:")
            )
            if legacy
            else frozenset()
        )
        if missing_references(resources, external=external):
            raise WorkspaceCoreError("Invalid center resource references")
        return WorkspaceSnapshot(self.root, resources, (), ())

    def read(self) -> RemoteSnapshot:
        if has_pending((self.root,), policy=RECONCILE_POLICY):
            raise WorkspaceCoreError("Pending center transaction needs recovery")
        path = state_path(self.root, REMOTE_STATE)
        if entry_type(path) != "file":
            raise WorkspaceCoreError("Resource center is not initialized")
        before = path.read_text(encoding="utf-8")
        try:
            raw = json.loads(before)
        except ValueError as exc:
            raise WorkspaceCoreError("Invalid resource center state") from exc
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw.get("version") not in (1, 2)
            or not valid_identity(raw.get("sync_id"))
            or type(raw.get("generation")) is not int
            or raw["generation"] < 0
        ):
            raise WorkspaceCoreError("Invalid resource center state")
        resources = decode_resources(raw.get("resources"))
        validate_skill_fingerprint_scheme(raw.get("skill_fingerprint"), resources)
        try:
            values = {
                key: TomlValue.decode(value)
                for key, value in raw.get("values", {}).items()
            }
        except (ValueError, AttributeError, TypeError) as exc:
            raise WorkspaceCoreError("Invalid center field values") from exc
        self.verify_contents(resources, values, legacy=raw["version"] == 1)
        if path.read_text(encoding="utf-8") != before:
            raise WorkspaceCoreError("Resource center changed during read; run again")
        return RemoteSnapshot(raw["sync_id"], raw["generation"], resources, values)

    def content(self, snapshot: RemoteSnapshot) -> ResourceContent:
        return ResourceContent(
            snapshot.resources,
            {
                key: self.root / resource.parts[0].path
                for key, resource in snapshot.resources.items()
                if not is_shared_resource(resource.kind)
            },
            values=snapshot.values,
        )

    def recover(self) -> bool:
        with self.lock():
            return recover((self.root,), policy=RECONCILE_POLICY)

    def commit(
        self,
        expected: RemoteSnapshot,
        content: ResourceContent,
        writes: tuple[ResourceWrite, ...],
    ) -> RemoteSnapshot:
        """Commit all accepted writes and generation together, or none of them."""
        with self.lock():
            current = self.read()
            if current != expected:
                raise WorkspaceCoreError("Resource center generation changed; replan")
            if not writes:
                return current
            resources = dict(current.resources)
            values = dict(current.values)
            canonical_content = {}
            standalone = []
            for write in writes:
                old = current.resources.get(write.id)
                if (old.fingerprint if old else None) != write.before:
                    raise WorkspaceCoreError(
                        f"Target changed before writing: {write.id}"
                    )
                canonical = resource_for_id(
                    write.id,
                    write.fingerprint
                    if write.fingerprint is not None
                    else write.before,
                )
                if write.relative_path != Path(canonical.parts[0].path):
                    raise WorkspaceCoreError(
                        f"Invalid resource destination: {write.id}"
                    )
                if write.fingerprint is None:
                    if old is None:
                        raise WorkspaceCoreError(
                            f"Missing deleted resource: {write.id}"
                        )
                    resources.pop(write.id)
                    values.pop(write.id, None)
                else:
                    source = content.resources.get(write.id)
                    if (
                        source is None
                        or source.id != write.id
                        or source.fingerprint != write.fingerprint
                        or (
                            source.kind not in {"mcp", "subagent"}
                            and source.references != canonical.references
                        )
                    ):
                        raise WorkspaceCoreError(
                            f"Invalid resource content metadata: {write.id}"
                        )
                    canonical = replace(canonical, references=source.references)
                    canonical_content[write.id] = canonical
                    resources[write.id] = canonical
                    if write.kind in {"config", "project-field"}:
                        value = content.values.get(write.id)
                        if value is None:
                            raise WorkspaceCoreError(
                                f"Missing resource value: {write.id}"
                            )
                        values[write.id] = value
                if not is_shared_resource(write.kind):
                    standalone.append(
                        replace(write, source_path=Path(canonical.parts[0].path))
                    )
            if credential_resources(
                ResourceContent(canonical_content, content.paths, values=values),
                {write.id for write in writes if write.fingerprint is not None},
            ):
                raise WorkspaceCoreError(
                    "Possible plaintext credential; center commit blocked"
                )
            overlay = {
                key: resource
                for key, resource in resources.items()
                if is_shared_resource(resource.kind)
            }
            target_resources = {
                key: resource
                for key, resource in current.resources.items()
                if not is_shared_resource(resource.kind)
            }
            target_resources.update(overlay)
            target = WorkspaceSnapshot(self.root, target_resources, (), ())
            source_content = ResourceContent(
                canonical_content, content.paths, values=values
            )
            with tempfile.TemporaryDirectory(prefix="aikito-center-write-") as staging:
                changes, intended = prepare_resource_writes(
                    source_content,
                    target,
                    tuple(standalone),
                    Path(staging),
                    policy=RECONCILE_POLICY,
                )
                if intended != resources:
                    raise WorkspaceCoreError("Invalid center resource batch")
                new = RemoteSnapshot(
                    current.sync_id, current.generation + 1, resources, values
                )
                path = state_path(self.root, REMOTE_STATE)
                apply(
                    (self.root,),
                    changes,
                    states=(
                        StateUpdate(
                            0,
                            REMOTE_STATE,
                            path.read_text(encoding="utf-8"),
                            self.encode(new),
                        ),
                    ),
                    verify=lambda: verify_resource_snapshot(
                        self.verify_contents(resources, values),
                        resources,
                    ),
                    policy=RECONCILE_POLICY,
                )
            return new
