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
from shutil import copy2, copytree
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .compat import is_windows, secure_directory_permissions, secure_file_permissions
from .workspace_core import (
    PathPolicy,
    StateUpdate,
    WorkspaceCoreError,
    apply,
    entry_type,
    has_pending,
    recover,
    validate_resource_path,
    version_at,
)
from .workspace_resource_write import (
    ResourceContent,
    ResourceWrite,
    prepare_resource_writes,
    verify_resource_snapshot,
)
from .workspace_resources import (
    Resource,
    ResourcePart,
    WorkspaceSnapshot,
    is_ignored_name,
    physical_kind,
    scan_credentials,
)

if is_windows():
    import msvcrt
else:
    import fcntl


SYNC_KINDS = frozenset({"memory", "project-memory", "skill"})
REMOTE_STATE = ".local/state/aikito/workspace-reconcile/remote.json"
REPLICA_STATE = ".local/state/aikito/workspace-reconcile/replica.json"
RECONCILE_POLICY = PathPolicy(states=(REMOTE_STATE, REPLICA_STATE), create_parents=True)


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
    """Decode supported IDs using their canonical, validated storage paths."""
    kind, separator, name = identity.partition(":")
    if not separator or not name or kind not in SYNC_KINDS:
        raise WorkspaceCoreError(f"Unsupported resource ID: {identity}")
    references = ()
    if kind == "project-memory":
        project, separator, note = name.partition("/")
        if not separator:
            raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
        path = f"projects/{project}/memory/{note}"
        references = (f"project:{project}",)
    else:
        path = f"{'skills' if kind == 'skill' else 'memory'}/{name}"
    validate_resource_path(path, physical_kind(kind))
    if any(is_ignored_name(part) for part in Path(path).parts):
        raise WorkspaceCoreError(f"Excluded resource ID: {identity}")
    if (kind == "skill" and len(Path(path).parts) != 2) or (
        kind != "skill" and not path.endswith(".md")
    ):
        raise WorkspaceCoreError(f"Invalid resource ID: {identity}")
    if (
        not isinstance(fingerprint, str)
        or len(fingerprint) != 64
        or any(char not in "0123456789abcdef" for char in fingerprint)
    ):
        raise WorkspaceCoreError(f"Invalid resource fingerprint: {identity}")
    return Resource(kind, name, fingerprint, (ResourcePart(path),), references)


def decode_resources(raw: object) -> dict[str, Resource]:
    if not isinstance(raw, dict) or any(not isinstance(key, str) for key in raw):
        raise WorkspaceCoreError("Invalid resource state")
    return {key: resource_for_id(key, value) for key, value in raw.items()}


def encode_resources(resources: dict[str, Resource]) -> dict[str, str]:
    return {key: resource.fingerprint for key, resource in sorted(resources.items())}


def valid_identity(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 32
        and all(char in "0123456789abcdef" for char in value)
    )


def external_projects(resources: dict[str, Resource]) -> frozenset[str]:
    # Project configuration remains local until that resource kind is supported.
    return frozenset(
        reference
        for resource in resources.values()
        for reference in resource.references
        if reference.startswith("project:")
    )


@dataclass(frozen=True)
class RemoteSnapshot:
    sync_id: str
    generation: int
    resources: dict[str, Resource]


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
                "version": 1,
                "sync_id": snapshot.sync_id,
                "generation": snapshot.generation,
                "resources": encode_resources(snapshot.resources),
            },
            sort_keys=True,
        )

    def verify_contents(self, resources: dict[str, Resource]) -> WorkspaceSnapshot:
        paths = {resource.parts[0].path for resource in resources.values()}

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

        check_files(self.root / "memory")
        check_files(self.root / "projects")
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
            if (
                resource.kind == "skill"
                and entry_type(self.root / resource.parts[0].path / "SKILL.md")
                != "file"
            ):
                raise WorkspaceCoreError(f"Invalid center skill: {resource.id}")
            version = version_at(
                self.root,
                resource.parts[0].path,
                physical_kind(resource.kind),
                RECONCILE_POLICY,
            )
            if version is None or version.fingerprint != resource.fingerprint:
                raise WorkspaceCoreError(
                    f"Resource center content changed: {resource.id}"
                )
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
            or raw.get("version") != 1
            or not valid_identity(raw.get("sync_id"))
            or type(raw.get("generation")) is not int
            or raw["generation"] < 0
        ):
            raise WorkspaceCoreError("Invalid resource center state")
        resources = decode_resources(raw.get("resources"))
        self.verify_contents(resources)
        if path.read_text(encoding="utf-8") != before:
            raise WorkspaceCoreError("Resource center changed during read; run again")
        return RemoteSnapshot(raw["sync_id"], raw["generation"], resources)

    def content(self, snapshot: RemoteSnapshot) -> ResourceContent:
        return ResourceContent(
            snapshot.resources,
            {
                key: self.root / resource.parts[0].path
                for key, resource in snapshot.resources.items()
            },
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
            for write in writes:
                canonical = resource_for_id(write.id, write.fingerprint or write.before)
                if write.relative_path != Path(canonical.parts[0].path) or (
                    write.fingerprint is not None
                    and content.resources.get(write.id) != canonical
                ):
                    raise WorkspaceCoreError(
                        f"Invalid resource content metadata: {write.id}"
                    )
            target = WorkspaceSnapshot(self.root, current.resources, (), ())
            with tempfile.TemporaryDirectory(prefix="aikito-center-write-") as staging:
                changes, resources = prepare_resource_writes(
                    content,
                    target,
                    writes,
                    Path(staging),
                    policy=RECONCILE_POLICY,
                    external=external_projects(
                        {**current.resources, **content.resources}
                    ),
                )
                # Scan the actual staged content, even when supplied outside a workspace.
                uploaded = {}
                for change in changes:
                    if change.source is not None:
                        destination = Path(staging) / "scan" / change.path
                        destination.parent.mkdir(parents=True, exist_ok=True)
                        if change.kind == "skill":
                            copytree(change.source, destination)
                        else:
                            copy2(change.source, destination)
                        uploaded[change.path] = Resource(
                            change.kind,
                            change.path,
                            change.after,
                            (ResourcePart(change.path),),
                        )
                scan = WorkspaceSnapshot(Path(staging) / "scan", uploaded, (), ())
                if scan_credentials(scan):
                    raise WorkspaceCoreError(
                        "Possible plaintext credential; center commit blocked"
                    )
                new = RemoteSnapshot(current.sync_id, current.generation + 1, resources)
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
                        self.verify_contents(resources),
                        resources,
                        external=external_projects(resources),
                    ),
                    policy=RECONCILE_POLICY,
                )
            return new
