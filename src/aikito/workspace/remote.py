"""Filesystem resource center with conditional, recoverable batch commits.

The center is a resource store, not a workspace. Its manifest names logical
resources and a monotonically increasing revision; consumers obtain content
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
from collections.abc import Collection, Mapping, Sequence
from types import MappingProxyType
from typing import Iterator

from ..compat import is_windows, secure_file_permissions
from .transactions import (
    StateUpdate,
    WorkspaceCoreError,
    apply,
    entry_type,
    has_pending,
    recover,
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
    WorkspaceResourceError,
    WorkspaceSnapshot,
    is_ignored_name,
    is_shared_resource,
    inspect_resource_content,
    skill_mode_fingerprint,
    value_fingerprint,
    SKILL_FINGERPRINT_SCHEME,
)

from .toml_render import TomlValue
from .payload import (
    ResourceDescriptor,
    ResourceMutation,
    ResourcePayload,
    PayloadError,
    payload_hash,
    tree_mode_fingerprint,
    TreePayload,
)
from .payload_io import capture_resources, materialize_resources
from .remote_store import (
    RemoteSnapshot,
    SnapshotExpired,
    InvalidContent,
    RecoveryRequired,
    StoreUnavailable,
)

from .resource_state import (
    SYNC_KINDS,
    LOCAL_CONFIG,
    REMOTE_STATE,
    REPLICA_STATE,
    RECONCILE_POLICY,
    state_path,
    local_resource_for_id,
    decode_resources,
    decode_revision,
    encode_resources,
    validate_skill_fingerprint_scheme,
    valid_identity,
)

if is_windows():
    import msvcrt
else:
    import fcntl


__all__ = [
    "FilesystemRemote",
    "SYNC_KINDS",
    "LOCAL_CONFIG",
    "REMOTE_STATE",
    "REPLICA_STATE",
    "RECONCILE_POLICY",
    "state_path",
    "resource_for_id",
    "decode_resources",
    "encode_resources",
    "validate_skill_fingerprint_scheme",
    "valid_identity",
]


def resource_for_id(identity: str, fingerprint: str) -> Resource:
    """Map a logical resource to the plaintext center layout."""
    return local_resource_for_id(identity, fingerprint)


@dataclass(frozen=True)
class _CenterState:
    sync_id: str
    revision: int
    resources: dict[str, Resource]
    values: dict[str, TomlValue] = field(default_factory=dict)
    payload_hashes: dict[str, str] | None = field(default_factory=dict)


class FilesystemRemote:
    """Read by ID and commit a complete batch against one expected revision."""

    _mutex = threading.RLock()

    def __init__(self, root: Path, *, workspace: Path | None = None):
        self.root = root.expanduser().resolve()
        self._depth = 0
        if workspace is not None:
            self.validate_replica(workspace)

    def validate_replica(self, local: Path) -> None:
        local = local.expanduser().resolve()
        if (
            local == self.root
            or local in self.root.parents
            or self.root in local.parents
        ):
            raise InvalidContent("Replica and resource center must be separate")

    @classmethod
    def create(cls, root: Path) -> FilesystemRemote:
        remote = cls(root)
        if entry_type(remote.root) == "missing":
            remote.root.mkdir(parents=True, mode=0o700)
        if entry_type(remote.root) != "directory" or any(remote.root.iterdir()):
            raise WorkspaceCoreError("Resource center requires an empty directory")
        state_path(remote.root, REMOTE_STATE, create=True)
        with remote.lock():
            initial = _CenterState(uuid.uuid4().hex, 0, {})
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
    def encode(snapshot: _CenterState) -> str:
        return json.dumps(
            {
                "version": 2,
                "skill_fingerprint": SKILL_FINGERPRINT_SCHEME,
                "sync_id": snapshot.sync_id,
                "revision": snapshot.revision,
                "resources": {
                    key: {
                        "fingerprint": resource.fingerprint,
                        "references": list(resource.references),
                        **(
                            {"mode_fingerprint": resource.mode_fingerprint}
                            if resource.kind == "skill"
                            and resource.mode_fingerprint is not None
                            else {}
                        ),
                    }
                    for key, resource in sorted(snapshot.resources.items())
                },
                "payload_hashes": dict(sorted(snapshot.payload_hashes.items())),
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
            if resource.kind == "skill" and resource.mode_fingerprint is not None:
                if (
                    skill_mode_fingerprint(self.root / resource.parts[0].path)
                    != resource.mode_fingerprint
                ):
                    raise WorkspaceCoreError(
                        f"Resource center executable state changed: {resource.id}"
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

    def _read_state(self) -> _CenterState:
        if has_pending((self.root,), policy=RECONCILE_POLICY):
            raise RecoveryRequired("Pending center transaction needs recovery")
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
        ):
            raise WorkspaceCoreError("Invalid resource center state")
        revision = decode_revision(raw)
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
        return _CenterState(
            raw["sync_id"],
            revision,
            resources,
            values,
            raw.get("payload_hashes"),
        )

    def _content(self, snapshot: _CenterState) -> ResourceContent:
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
        try:
            with self.lock():
                return recover((self.root,), policy=RECONCILE_POLICY)
        except WorkspaceCoreError as exc:
            raise InvalidContent(str(exc)) from exc
        except OSError as exc:
            raise StoreUnavailable("Resource center recovery unavailable") from exc

    def _load(
        self,
    ) -> tuple[_CenterState, RemoteSnapshot, Mapping[str, ResourcePayload]]:
        """Optimistically capture one coherent version without creating a lock."""
        try:
            path = state_path(self.root, REMOTE_STATE)
            before = path.read_bytes()
            state = self._read_state()
            payloads = capture_resources(
                self._content(state), list(state.resources), check_credentials=False
            )
            if path.read_bytes() != before:
                raise SnapshotExpired(
                    "Resource center revision changed during read; replan"
                )
            if has_pending((self.root,), policy=RECONCILE_POLICY):
                raise RecoveryRequired("Pending center transaction needs recovery")
            hashes = {key: payload_hash(payload) for key, payload in payloads.items()}
            if state.payload_hashes is not None and state.payload_hashes != hashes:
                raise InvalidContent("Resource center payload integrity mismatch")
            snapshot = RemoteSnapshot(
                state.sync_id,
                state.revision,
                {
                    key: ResourceDescriptor(
                        resource.fingerprint,
                        hashes[key],
                        resource.references,
                        tree_mode_fingerprint(payloads[key])
                        if isinstance(payloads[key], TreePayload)
                        else None,
                    )
                    for key, resource in state.resources.items()
                },
            )
            return state, snapshot, payloads
        except (WorkspaceCoreError, WorkspaceResourceError, PayloadError) as exc:
            raise InvalidContent(str(exc)) from exc
        except OSError as exc:
            raise StoreUnavailable("Resource center is unavailable") from exc

    def read(self) -> RemoteSnapshot:
        return self._load()[1]

    def fetch(
        self, expected: RemoteSnapshot, ids: Collection[str]
    ) -> Mapping[str, ResourcePayload]:
        _, current, payloads = self._load()
        if current != expected:
            raise SnapshotExpired("Resource center revision changed; replan")
        if any(identity not in current.resources for identity in ids):
            raise InvalidContent("Missing requested resource content")
        return MappingProxyType({identity: payloads[identity] for identity in ids})

    def commit(
        self, expected: RemoteSnapshot, mutations: Sequence[ResourceMutation]
    ) -> RemoteSnapshot:
        try:
            with self.lock():
                state, current, _ = self._load()
                if current != expected:
                    raise SnapshotExpired("Resource center revision changed; replan")
                if len(mutations) != len({m.id for m in mutations}):
                    raise InvalidContent("Duplicate resource mutation ID")
                if not mutations:
                    return current
                resources, payloads, writes = {}, {}, []
                descriptors = dict(current.resources)
                for mutation in mutations:
                    if current.resources.get(mutation.id) != mutation.before:
                        raise SnapshotExpired("Target changed before writing")
                    descriptor = mutation.after or mutation.before
                    resource = resource_for_id(mutation.id, descriptor.fingerprint)
                    resource = replace(
                        resource,
                        references=descriptor.references,
                        mode_fingerprint=descriptor.mode_fingerprint,
                    )
                    if mutation.after is None:
                        descriptors.pop(mutation.id)
                    else:
                        if resource.kind == "skill" and (
                            not isinstance(mutation.payload, TreePayload)
                            or mutation.after.mode_fingerprint
                            != tree_mode_fingerprint(mutation.payload)
                        ):
                            raise InvalidContent("Mutation executable state mismatch")
                        if (
                            payload_hash(mutation.payload)
                            != mutation.after.content_hash
                        ):
                            raise InvalidContent("Mutation payload hash mismatch")
                        resources[mutation.id] = resource
                        payloads[mutation.id] = mutation.payload
                        descriptors[mutation.id] = mutation.after
                    writes.append(
                        ResourceWrite(
                            Path(resource.parts[0].path),
                            resource.kind,
                            mutation.after.fingerprint if mutation.after else None,
                            resource.name,
                            before=mutation.before.fingerprint
                            if mutation.before
                            else None,
                        )
                    )
                with tempfile.TemporaryDirectory(
                    prefix="aikito-center-payload-"
                ) as staging:
                    content = materialize_resources(resources, payloads, Path(staging))
                    self._commit_resources(
                        state,
                        content,
                        tuple(writes),
                        payload_hashes={
                            key: value.content_hash
                            for key, value in descriptors.items()
                        },
                    )
                return self.read()
        except (WorkspaceCoreError, WorkspaceResourceError, PayloadError) as exc:
            raise InvalidContent(str(exc)) from exc
        except OSError as exc:
            raise StoreUnavailable("Resource center commit unavailable") from exc

    def _commit_resources(
        self,
        expected: _CenterState,
        content: ResourceContent,
        writes: tuple[ResourceWrite, ...],
        *,
        payload_hashes: Mapping[str, str],
    ) -> _CenterState:
        """Commit all accepted writes and revision together, or none of them."""
        with self.lock():
            current = self._read_state()
            if current != expected:
                raise WorkspaceCoreError("Resource center revision changed; replan")
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
                    canonical = replace(
                        canonical,
                        references=source.references,
                        mode_fingerprint=source.mode_fingerprint,
                    )
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
                new = _CenterState(
                    current.sync_id,
                    current.revision + 1,
                    resources,
                    values,
                    dict(payload_hashes),
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
