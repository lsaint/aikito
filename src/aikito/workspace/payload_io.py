"""Capture selected local content and materialize validated portable payloads.

Only this adapter knows filesystem locations. Shared fields never read or copy
their neighbors into a payload. Download validation completes for the entire
batch before staging starts; existing writers still own target transactions.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from ..compat import is_windows
from .payload import (
    FilePayload,
    MemberPayload,
    PayloadError,
    ResourceDescriptor,
    ResourceMutation,
    ResourcePayload,
    TomlPayload,
    TreeEntry,
    TreePayload,
    credential_payload,
    encode_payload,
    payload_hash,
    tree_mode_fingerprint,
    validate_payload,
    validate_tree_path,
)
from .resource_write import ResourceContent, ResourceWrite, prepare_resource_writes
from .resources import (
    Resource,
    WorkspaceSnapshot,
    SKILL_EXECUTABLE_METADATA,
    is_ignored_name,
)
from .skill_metadata import read_executable_metadata, write_executable_metadata
from .transactions import Change, PathPolicy, entry_type


def _safe_file(path: Path) -> bytes:
    if entry_type(path) != "file":
        raise PayloadError("Payload source is not a regular file")
    before = path.lstat()
    try:
        with os.fdopen(
            os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)), "rb"
        ) as stream:
            data = stream.read()
    except OSError as exc:
        raise PayloadError("Cannot safely read payload source") from exc
    after = path.lstat()
    if entry_type(path) != "file" or (
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    ) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise PayloadError("Payload source changed during capture")
    return data


def _check_ancestors(path: Path) -> None:
    for parent in path.parents:
        if entry_type(parent) != "directory":
            raise PayloadError("Payload source crosses an unsafe directory")


def _execution_metadata(root: Path) -> frozenset[str]:
    path = root / SKILL_EXECUTABLE_METADATA
    if entry_type(path) == "missing":
        return frozenset()
    try:
        names = read_executable_metadata(path)
        for name in names:
            validate_tree_path(name)
        return names
    except (ValueError, TypeError, KeyError) as exc:
        raise PayloadError("Invalid skill executable metadata") from exc


def _capture_tree(root: Path) -> TreePayload:
    if entry_type(root) != "directory":
        raise PayloadError("Skill source is not a regular directory")
    logical = _execution_metadata(root)
    entries = []

    def visit(directory: Path):
        children = [
            p for p in sorted(directory.iterdir()) if not is_ignored_name(p.name)
        ]
        if not children and directory != root:
            entries.append(TreeEntry(directory.relative_to(root).as_posix()))
        for child in children:
            kind = entry_type(child)
            if kind == "directory":
                visit(child)
            elif kind == "file":
                relative = child.relative_to(root).as_posix()
                executable = (
                    relative in logical
                    if is_windows()
                    else bool(
                        child.lstat().st_mode
                        & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    )
                )
                entries.append(TreeEntry(relative, _safe_file(child), executable))
            else:
                raise PayloadError("Skill contains an unsafe node")

    visit(root)
    return TreePayload(tuple(entries))


def capture_resources(
    content: ResourceContent,
    identities: Sequence[str],
    *,
    check_credentials: bool = True,
) -> Mapping[str, ResourcePayload]:
    """Capture only explicitly authorized resources and verify planned semantics."""
    if len(identities) != len(set(identities)):
        raise PayloadError("Duplicate captured resource ID")
    result = {}
    for identity in sorted(identities):
        resource = content.resources.get(identity)
        if resource is None or resource.id != identity:
            raise PayloadError("Missing selected resource")
        if resource.kind in {"config", "project-field"}:
            field = content.values.get(identity)
            if field is None:
                raise PayloadError("Missing selected typed value")
            payload = TomlPayload.from_value(field)
        elif resource.kind in {
            "project",
            "project-path",
            "project-skill",
            "skill-selection",
        }:
            payload = MemberPayload()
        else:
            path = content.paths.get(identity)
            if path is None:
                raise PayloadError("Missing selected source content")
            _check_ancestors(path)
            payload = (
                _capture_tree(path)
                if resource.kind == "skill"
                else FilePayload(_safe_file(path))
            )
        validate_payload(resource, payload)
        if check_credentials and credential_payload(payload):
            raise PayloadError("Possible plaintext credential; payload capture blocked")
        result[identity] = payload
    return MappingProxyType(result)


def capture_mutations(
    content: ResourceContent,
    writes: Sequence[ResourceWrite],
    before: Mapping[str, ResourceDescriptor],
) -> tuple[ResourceMutation, ...]:
    if len(writes) != len({write.id for write in writes}):
        raise PayloadError("Duplicate mutation ID")
    payloads = capture_resources(
        content, [w.id for w in writes if w.fingerprint is not None]
    )
    mutations = []
    for write in writes:
        previous = before.get(write.id)
        if (previous.fingerprint if previous is not None else None) != write.before:
            raise PayloadError("Mutation before descriptor changed")
        if write.fingerprint is None:
            mutations.append(ResourceMutation(write.id, previous, None, None))
        else:
            resource = content.resources[write.id]
            if resource.fingerprint != write.fingerprint:
                raise PayloadError("Mutation source changed after planning")
            payload = payloads[write.id]
            descriptor = ResourceDescriptor(
                resource.fingerprint,
                payload_hash(payload),
                len(encode_payload(payload)),
                resource.references,
                tree_mode_fingerprint(payload)
                if isinstance(payload, TreePayload)
                else None,
            )
            mutations.append(ResourceMutation(write.id, previous, descriptor, payload))
    return tuple(mutations)


def materialize_resources(
    resources: Mapping[str, Resource],
    payloads: Mapping[str, ResourcePayload],
    staging: Path,
) -> ResourceContent:
    """Validate the complete batch, then write only to an owned empty directory."""
    if set(resources) != set(payloads):
        raise PayloadError("Incomplete downloaded payload batch")
    for identity, resource in resources.items():
        if resource.id != identity:
            raise PayloadError("Downloaded resource identity mismatch")
        validate_payload(resource, payloads[identity])
        if credential_payload(payloads[identity]):
            raise PayloadError("Possible plaintext credential; download blocked")
    if entry_type(staging) != "directory" or any(staging.iterdir()):
        raise PayloadError("Payload staging requires an empty regular directory")
    staging = staging.resolve()
    paths, values = {}, {}
    for index, identity in enumerate(sorted(resources)):
        payload = payloads[identity]
        if isinstance(payload, TomlPayload):
            values[identity] = payload.field()
        elif isinstance(payload, MemberPayload):
            continue
        else:
            path = staging / str(index)
            if isinstance(payload, FilePayload):
                path.write_bytes(payload.data)
            elif isinstance(payload, TreePayload):
                path.mkdir()
                for entry in payload.entries:
                    target = path.joinpath(*entry.path.split("/"))
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if entry.data is None:
                        target.mkdir()
                    else:
                        target.write_bytes(entry.data)
                        if not is_windows():
                            target.chmod(0o755 if entry.executable else 0o644)
                if is_windows():
                    write_executable_metadata(
                        path / SKILL_EXECUTABLE_METADATA,
                        [e.path for e in payload.entries if e.executable],
                    )
            paths[identity] = path
    return ResourceContent(dict(resources), paths, values=values)


def prepare_payload_writes(
    resources: Mapping[str, Resource],
    payloads: Mapping[str, ResourcePayload],
    target_snapshot: WorkspaceSnapshot,
    writes: Sequence[ResourceWrite],
    staging: Path,
    *,
    policy: PathPolicy,
    external: frozenset[str] = frozenset(),
    home: Path | None = None,
) -> tuple[tuple[Change, ...], dict[str, Resource]]:
    """Stage portable content, then compose each shared target file once."""
    content = materialize_resources(resources, payloads, staging)
    rendered = staging / "rendered"
    rendered.mkdir()
    return prepare_resource_writes(
        content,
        target_snapshot,
        tuple(writes),
        rendered,
        policy=policy,
        external=external,
        sync=True,
        home=home,
    )
