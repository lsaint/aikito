"""Portable immutable content and client-side semantic validation.

The versioned encoding is independent of physical resource parts. Transport
hashes cover all metadata, including flags excluded from logical fingerprints.
TOML values are stored as canonical text so callers cannot mutate nested data.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from datetime import date, time
from typing import TypeAlias

from .resources import (
    Resource,
    RESOURCE_STORAGE,
    SKILL_EXECUTABLE_METADATA,
    has_credential_bytes,
    inspect_resource_bytes,
    is_ignored_name,
    value_fingerprint,
)
from .skill_metadata import executable_fingerprint
from .toml_render import TomlValue, _toml_value


class PayloadError(ValueError):
    """Invalid portable content; messages never include credential values."""


def _canonical_value(value: object) -> object:
    if isinstance(value, dict):
        if any(type(key) is not str for key in value):
            raise PayloadError("Invalid TOML table keys")
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, (str, bool, int, float, date, time)):
        return value
    raise PayloadError("Unsupported TOML value type")


@dataclass(frozen=True)
class FilePayload:
    data: bytes

    def __post_init__(self):
        if type(self.data) is not bytes:
            raise PayloadError("File payload requires immutable bytes")


@dataclass(frozen=True)
class TomlPayload:
    path: tuple[str, ...]
    text: str

    def __post_init__(self):
        if isinstance(self.path, str):
            raise PayloadError("TOML keys require components, not a dotted string")
        object.__setattr__(self, "path", tuple(self.path))
        if not self.path or any(type(key) is not str for key in self.path):
            raise PayloadError("Invalid TOML key components")
        try:
            document = tomllib.loads(self.text)
            if set(document) != {"value"}:
                raise ValueError
            canonical = f"value = {_toml_value(_canonical_value(document['value']))}\n"
            object.__setattr__(self, "text", canonical)
        except (TypeError, ValueError) as exc:
            raise PayloadError("Invalid typed TOML payload") from exc

    @classmethod
    def from_value(cls, field: TomlValue) -> TomlPayload:
        return cls(
            field.path, f"value = {_toml_value(_canonical_value(field.value))}\n"
        )

    def field(self) -> TomlValue:
        return TomlValue(self.path, tomllib.loads(self.text)["value"])


def validate_tree_path(path: str) -> None:
    """Reject paths that cannot safely represent the same tree on all targets."""
    if type(path) is not str or not path or path.startswith("/"):
        raise PayloadError("Invalid portable tree path")
    for part in path.split("/"):
        if (
            part in {"", ".", ".."}
            or part.endswith((".", " "))
            or any(ord(char) < 32 or char in '\\<>:"|?*' for char in part)
            or re.fullmatch(r"(?i)(?:CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?", part)
            or part == SKILL_EXECUTABLE_METADATA
            or is_ignored_name(part)
        ):
            raise PayloadError("Unsafe or excluded portable tree path")


@dataclass(frozen=True)
class TreeEntry:
    path: str
    data: bytes | None = None
    executable: bool = False

    def __post_init__(self):
        validate_tree_path(self.path)
        if (
            type(self.executable) is not bool
            or (self.data is not None and type(self.data) is not bytes)
            or (self.data is None and self.executable)
        ):
            raise PayloadError("Invalid tree node type or executable flag")


@dataclass(frozen=True)
class TreePayload:
    entries: tuple[TreeEntry, ...]

    def __post_init__(self):
        entries = tuple(self.entries)
        if any(not isinstance(entry, TreeEntry) for entry in entries):
            raise PayloadError("Unsupported tree node")
        object.__setattr__(
            self, "entries", tuple(sorted(entries, key=lambda e: e.path))
        )
        paths = set()
        aliases = {}
        for entry in self.entries:
            if entry.path in paths:
                raise PayloadError("Duplicate tree path")
            paths.add(entry.path)
            parts = entry.path.split("/")
            for length in range(1, len(parts) + 1):
                prefix = "/".join(parts[:length])
                key = unicodedata.normalize("NFC", prefix).casefold()
                if key in aliases and aliases[key] != prefix:
                    raise PayloadError("Target-platform tree path collision")
                aliases[key] = prefix
        for path in paths:
            parts = path.split("/")
            if any("/".join(parts[:i]) in paths for i in range(1, len(parts))):
                raise PayloadError("File or empty directory has descendants")
        if not any(e.path == "SKILL.md" and e.data is not None for e in self.entries):
            raise PayloadError("Skill payload lacks a regular SKILL.md")


def tree_mode_fingerprint(payload: TreePayload) -> str:
    return executable_fingerprint(
        entry.path for entry in payload.entries if entry.executable
    )


@dataclass(frozen=True)
class MemberPayload:
    """Presence of a logical set member or project provider, never a shared file."""


ResourcePayload: TypeAlias = FilePayload | TomlPayload | TreePayload | MemberPayload


def encode_payload(payload: ResourcePayload) -> bytes:
    if isinstance(payload, FilePayload):
        body = {"type": "file", "data": base64.b64encode(payload.data).decode("ascii")}
    elif isinstance(payload, TomlPayload):
        body = {"type": "toml", "path": list(payload.path), "text": payload.text}
    elif isinstance(payload, TreePayload):
        body = {
            "type": "tree",
            "entries": [
                {
                    "path": e.path,
                    "data": None
                    if e.data is None
                    else base64.b64encode(e.data).decode("ascii"),
                    "executable": e.executable,
                }
                for e in payload.entries
            ],
        }
    elif isinstance(payload, MemberPayload):
        body = {"type": "member"}
    else:
        raise PayloadError("Unsupported payload type")
    return json.dumps(
        {"version": 1, **body},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def payload_hash(payload: ResourcePayload) -> str:
    return hashlib.sha256(encode_payload(payload)).hexdigest()


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise PayloadError("Duplicate payload field")
        result[key] = value
    return result


def decode_payload(encoded: bytes, expected_hash: str) -> ResourcePayload:
    if (
        type(encoded) is not bytes
        or hashlib.sha256(encoded).hexdigest() != expected_hash
    ):
        raise PayloadError("Payload transport hash mismatch")
    try:
        raw = json.loads(encoded, object_pairs_hook=_unique_object)
        if type(raw.get("version")) is not int or raw["version"] != 1:
            raise PayloadError("Unsupported payload version")
        kind = raw["type"]
        if kind == "file" and set(raw) == {"version", "type", "data"}:
            payload = FilePayload(base64.b64decode(raw["data"], validate=True))
        elif kind == "toml" and set(raw) == {"version", "type", "path", "text"}:
            if type(raw["path"]) is not list:
                raise PayloadError("Invalid TOML path")
            payload = TomlPayload(tuple(raw["path"]), raw["text"])
        elif kind == "tree" and set(raw) == {"version", "type", "entries"}:
            if type(raw["entries"]) is not list:
                raise PayloadError("Invalid tree entries")
            nodes = []
            for node in raw["entries"]:
                if set(node) != {"path", "data", "executable"}:
                    raise PayloadError("Invalid tree fields")
                data = (
                    None
                    if node["data"] is None
                    else base64.b64decode(node["data"], validate=True)
                )
                nodes.append(TreeEntry(node["path"], data, node["executable"]))
            payload = TreePayload(tuple(nodes))
        elif kind == "member" and set(raw) == {"version", "type"}:
            payload = MemberPayload()
        else:
            raise PayloadError("Invalid payload fields or type")
        if encode_payload(payload) != encoded:
            raise PayloadError("Noncanonical payload encoding")
        return payload
    except (ValueError, TypeError, KeyError, AttributeError) as exc:
        raise PayloadError("Invalid portable payload") from exc


def inspect_payload(
    resource: Resource, payload: ResourcePayload
) -> tuple[str, tuple[str, ...]]:
    if resource.kind not in RESOURCE_STORAGE:
        raise PayloadError("Unsupported portable resource kind")
    if resource.kind in {"config", "project-field"} and isinstance(
        payload, TomlPayload
    ):
        field = payload.field()
        if resource.kind == "config" and isinstance(field.value, dict):
            raise PayloadError("Configuration payload must select one leaf field")
        expected = (
            resource.name
            if resource.kind == "config"
            else resource.name.partition("/")[2]
        )
        if (
            ".".join(field.path) if resource.kind == "config" else field.path[0]
        ) != expected or (resource.kind == "project-field" and len(field.path) != 1):
            raise PayloadError("Unexpected TOML key components")
        return value_fingerprint(field.value), resource.references
    if resource.kind in {
        "project",
        "project-path",
        "project-skill",
        "skill-selection",
    } and isinstance(payload, MemberPayload):
        return "", resource.references
    if resource.kind == "skill" and isinstance(payload, TreePayload):
        records = [
            f"d {e.path}"
            if e.data is None
            else f"f {e.path} {hashlib.sha256(e.data).hexdigest()}"
            for e in payload.entries
        ]
        return hashlib.sha256(
            "\n".join(sorted(records)).encode("utf-8")
        ).hexdigest(), resource.references
    if resource.kind not in {
        "config",
        "project-field",
        "project",
        "project-path",
        "project-skill",
        "skill-selection",
        "skill",
    } and isinstance(payload, FilePayload):
        try:
            return inspect_resource_bytes(resource, payload.data)
        except ValueError as exc:
            raise PayloadError("Invalid standalone payload semantics") from exc
    raise PayloadError("Payload does not match logical resource kind")


def validate_payload(resource: Resource, payload: ResourcePayload) -> None:
    if inspect_payload(resource, payload) != (
        resource.fingerprint,
        resource.references,
    ):
        raise PayloadError("Payload semantic fingerprint or references changed")
    if (
        resource.kind == "skill"
        and resource.mode_fingerprint is not None
        and isinstance(payload, TreePayload)
        and tree_mode_fingerprint(payload) != resource.mode_fingerprint
    ):
        raise PayloadError("Payload executable state changed")


def credential_payload(payload: ResourcePayload) -> bool:
    if isinstance(payload, FilePayload):
        return has_credential_bytes(payload.data)
    if isinstance(payload, TreePayload):
        return any(
            has_credential_bytes(e.data) for e in payload.entries if e.data is not None
        )
    if isinstance(payload, TomlPayload):
        field = payload.field()
        key = field.path[-1].rsplit(".", 1)[-1]
        return has_credential_bytes(
            f"{key} = {_toml_value(field.value)}\n".encode("utf-8")
        )
    return False


@dataclass(frozen=True)
class ResourceDescriptor:
    fingerprint: str
    content_hash: str
    references: tuple[str, ...] = ()
    mode_fingerprint: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "references", tuple(self.references))
        if (
            type(self.fingerprint) is not str
            or type(self.content_hash) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.content_hash)
            or any(type(r) is not str for r in self.references)
            or (
                self.mode_fingerprint is not None
                and (
                    type(self.mode_fingerprint) is not str
                    or not re.fullmatch(r"[0-9a-f]{64}", self.mode_fingerprint)
                )
            )
        ):
            raise PayloadError("Invalid resource descriptor")


@dataclass(frozen=True)
class ResourceMutation:
    id: str
    before: ResourceDescriptor | None
    after: ResourceDescriptor | None
    payload: ResourcePayload | None

    def __post_init__(self):
        if (
            type(self.id) is not str
            or not self.id
            or (
                self.before is not None
                and not isinstance(self.before, ResourceDescriptor)
            )
            or (
                self.after is not None
                and not isinstance(self.after, ResourceDescriptor)
            )
        ):
            raise PayloadError("Invalid mutation identity or descriptors")
        if self.after is None:
            if self.before is None or self.payload is not None:
                raise PayloadError("Deletion requires presence and no payload")
        elif (
            self.payload is None
            or payload_hash(self.payload) != self.after.content_hash
        ):
            raise PayloadError("Mutation payload hash mismatch")
