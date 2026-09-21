"""Structured configuration models, target resolution, file aggregation, and snapshot validation.

Provides common foundation for Subagent and MCP configuration management, implementing:
- INV-CFG-01: Separation of Logical Config Node and Physical File
- INV-CFG-02: Single Pre-Image Multi-Operation File Aggregation and Write-Once Guarantee
- INV-CFG-03: Duplicate and Colliding Logical Key Detection Prior to Write
- INV-CFG-04: Pre-Image Mutation Stale Plan Invalidation
- INV-CFG-05: Preservation of Unmanaged Configuration Content and Format Semantics
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .compat import get_physical_path, is_windows


class ConfigCollisionError(Exception):
    """Raised when logical operations collide or conflict on the same physical configuration target."""


class StaleConfigPlanError(Exception):
    """Raised when an on-disk configuration target does not match the plan's pre-image snapshot."""


def resolve_physical_identity(path: Path) -> str:
    """Return a canonical physical identity string for a path, even if it does not yet exist.

    Uses get_physical_path to resolve symlinks and actual path casing. If the path does not exist,
    resolves its deepest existing ancestor and appends remaining relative segments.
    On case-insensitive operating systems (Windows and macOS), the identity is normalized to lowercase.
    """
    try:
        resolved = get_physical_path(path)
    except Exception:
        resolved = path.resolve()

    norm = resolved.as_posix()
    if is_windows() or sys.platform == "darwin":
        norm = norm.lower()
    return norm


@dataclass(frozen=True)
class ConfigTarget:
    """A logical configuration target node within a physical file."""

    path: Path
    logical_identity: str
    key_path: tuple[str, ...] = ()
    format: str = ""
    agent: str = ""
    sensitive: bool = False

    @property
    def physical_identity(self) -> str:
        return resolve_physical_identity(self.path)


@dataclass(frozen=True)
class FileSnapshot:
    """Precondition snapshot of a physical configuration file."""

    path: Path
    physical_identity: str
    exists: bool
    content_hash: str | None
    size_bytes: int = 0
    format: str = ""
    sensitive: bool = False

    def validate_precondition(self, current_path: Path | None = None) -> tuple[bool, str]:
        """Validate whether current disk state matches this frozen pre-image."""
        target = current_path or self.path
        if not target.exists():
            if self.exists:
                return False, f"File '{target}' existed at plan time but is now missing"
            return True, ""
        if not self.exists:
            return False, f"File '{target}' was missing at plan time but now exists"
        try:
            current_bytes = target.read_bytes()
            current_hash = hashlib.sha256(current_bytes).hexdigest()
            if current_hash != self.content_hash:
                return (
                    False,
                    f"File '{target}' content has been modified externally since plan generation",
                )
            return True, ""
        except OSError as e:
            return False, f"Cannot read file '{target}' for precondition validation: {e}"


def capture_file_snapshot(
    path: Path, format: str = "", sensitive: bool = False
) -> FileSnapshot:
    """Capture current file state as a frozen FileSnapshot."""
    phys_id = resolve_physical_identity(path)
    if not path.exists():
        return FileSnapshot(
            path=path,
            physical_identity=phys_id,
            exists=False,
            content_hash=None,
            size_bytes=0,
            format=format,
            sensitive=sensitive,
        )
    raw_bytes = path.read_bytes()
    content_hash = hashlib.sha256(raw_bytes).hexdigest()
    return FileSnapshot(
        path=path,
        physical_identity=phys_id,
        exists=True,
        content_hash=content_hash,
        size_bytes=len(raw_bytes),
        format=format,
        sensitive=sensitive,
    )


@dataclass(frozen=True)
class ConfigOperation:
    """A logical mutation planned for a ConfigTarget."""

    target: ConfigTarget
    action: str  # "CREATE", "UPDATE", "REMOVE", "NOOP", "CONFLICT", "SKIP"
    reason: str = ""
    requires_force: bool = False
    force_identity: str | None = None
    requires_prune: bool = False
    is_authorized: bool = True
    rendered_payload: Any = None


@dataclass(frozen=True)
class FileMutationPlan:
    """Aggregates all operations targeting a single physical configuration file."""

    path: Path
    physical_identity: str
    format: str
    sensitive: bool
    pre_image: FileSnapshot
    operations: tuple[ConfigOperation, ...] = ()
    final_content: str | None = None

    @property
    def has_mutations(self) -> bool:
        return any(
            op.action in ("CREATE", "UPDATE", "REMOVE") and op.is_authorized
            for op in self.operations
        )

    @property
    def has_conflicts(self) -> bool:
        return any(
            op.action == "CONFLICT" or not op.is_authorized
            for op in self.operations
        )

    def validate_precondition(self) -> None:
        """Validate that current on-disk target matches pre_image snapshot, raising if stale."""
        valid, msg = self.pre_image.validate_precondition(self.path)
        if not valid:
            raise StaleConfigPlanError(msg)


def aggregate_file_plans(
    operations: Sequence[ConfigOperation],
    existing_snapshots: Mapping[str, FileSnapshot] | None = None,
) -> list[FileMutationPlan]:
    """Group logical operations by physical target file and detect collisions.

    Enforces INV-CFG-01, INV-CFG-02, INV-CFG-03:
    1. Group operations by canonical physical identity rather than raw path strings.
    2. Enforce format compatibility: a single physical file cannot declare conflicting formats.
    3. Detect duplicate and colliding logical key mutations.
    4. If any operation or target targeting the file is sensitive, mark the entire file plan sensitive.
    """
    grouped_ops: dict[str, list[ConfigOperation]] = defaultdict(list)
    canonical_paths: dict[str, Path] = {}

    for op in operations:
        phys_id = op.target.physical_identity
        grouped_ops[phys_id].append(op)
        if phys_id not in canonical_paths:
            canonical_paths[phys_id] = op.target.path

    file_plans: list[FileMutationPlan] = []
    snapshots = existing_snapshots or {}

    for phys_id, ops in sorted(grouped_ops.items(), key=lambda x: str(canonical_paths[x[0]])):
        canonical_path = canonical_paths[phys_id]

        # Check format consistency
        formats = {op.target.format for op in ops if op.target.format}
        if len(formats) > 1:
            raise ConfigCollisionError(
                f"Conflicting formats declared for physical file '{canonical_path}': {sorted(formats)}"
            )
        resolved_format = formats.pop() if formats else ""

        # Check sensitive flag aggregation
        is_sensitive = any(op.target.sensitive for op in ops)

        # Check duplicate logical key collisions
        seen_keys: dict[tuple[str, ...], ConfigOperation] = {}
        for op in ops:
            # Whole-file operations have empty key_path
            if op.target.key_path:
                key = op.target.key_path
                if key in seen_keys:
                    prev_op = seen_keys[key]
                    # If either operation modifies the key, it is a collision
                    mutating = {"CREATE", "UPDATE", "REMOVE"}
                    if op.action in mutating or prev_op.action in mutating:
                        raise ConfigCollisionError(
                            f"Duplicate logical key {key} in physical file '{canonical_path}': "
                            f"conflicting operations '{prev_op.target.logical_identity}' ({prev_op.action}) "
                            f"and '{op.target.logical_identity}' ({op.action})"
                        )
                else:
                    seen_keys[key] = op

        # Resolve or capture snapshot
        if phys_id in snapshots:
            snapshot = snapshots[phys_id]
        else:
            snapshot = capture_file_snapshot(
                canonical_path, format=resolved_format, sensitive=is_sensitive
            )

        file_plans.append(
            FileMutationPlan(
                path=canonical_path,
                physical_identity=phys_id,
                format=resolved_format,
                sensitive=is_sensitive,
                pre_image=snapshot,
                operations=tuple(ops),
            )
        )

    return file_plans
