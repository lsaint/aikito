"""Shared symlink classification utilities for aikito.

Both status and doctor depend on this module so that they
produce consistent verdicts about the same filesystem state. Neither
command module imports the other.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from .compat import _resolve_symlink_target


@dataclass(frozen=True)
class ObservedLink:
    """Observed runtime filesystem facts for a symbolic link or container target."""

    target_path: Path
    entry_type: str  # "missing", "symlink", "dir", "file", "unsupported"
    expected_canonical: Path | None = None
    canonical_valid: bool = True
    canonical_error: str | None = None
    raw_link_target: Path | None = None
    resolved_link_target: Path | None = None
    link_points_to_canonical: bool = False
    is_same_object: bool = False
    target_lstat: Any = None
    target_kind: str = "managed_entry"  # "managed_entry", "consumer_link", "managed_container"


@dataclass(frozen=True)
class LinkOperation:
    """A single atomic operation planned for a symbolic link or container target."""

    action: str  # "CREATE", "UNLINK", "NOOP", "CONFLICT", "SHARED_PATH", "SKIP", "MIGRATE_CONTAINER"
    rule_id: str
    target_path: Path
    canonical_path: Path | None = None
    reason: str = ""
    finding: str | None = None
    is_authorized: bool = True
    expected_representation: str = "missing"
    desired_representation: str = "link"
    requires_parent_creation: bool = False
    is_same_object: bool = False


def inspect_link_target(
    target_path: Path,
    expected_canonical: Path | None = None,
    *,
    canonical_valid: bool = True,
    canonical_error: str | None = None,
    target_kind: str = "managed_entry",
    is_same_object: bool = False,
) -> ObservedLink:
    """Inspect the live filesystem entry at target_path without side effects."""
    entry_type = "missing"
    raw_link_target: Path | None = None
    resolved_link_target: Path | None = None
    link_points_to_canonical = False
    target_lstat: Any = None

    if target_path.is_symlink():
        entry_type = "symlink"
        try:
            target_lstat = target_path.lstat()
            resolved_link_target = _resolve_symlink_target(target_path)
            raw_val = os.readlink(target_path)
            raw_link_target = (
                target_path.parent / raw_val
                if not os.path.isabs(raw_val)
                else Path(raw_val)
            )
        except OSError:
            pass

        if expected_canonical is not None:
            # Check resolved target
            if resolved_link_target is not None:
                try:
                    if os.path.normcase(
                        str(resolved_link_target.resolve(strict=False))
                    ) == os.path.normcase(
                        str(expected_canonical.resolve(strict=False))
                    ):
                        link_points_to_canonical = True
                except (ValueError, OSError):
                    pass
            # Check raw target for broken links
            if not link_points_to_canonical and raw_link_target is not None:
                try:
                    if os.path.normcase(
                        str(raw_link_target.resolve(strict=False))
                    ) == os.path.normcase(
                        str(expected_canonical.resolve(strict=False))
                    ):
                        link_points_to_canonical = True
                except (ValueError, OSError):
                    pass
    elif target_path.is_dir():
        entry_type = "dir"
        try:
            target_lstat = target_path.stat()
        except OSError:
            pass
    elif target_path.is_file():
        entry_type = "file"
        try:
            target_lstat = target_path.stat()
        except OSError:
            pass
    elif not target_path.exists():
        entry_type = "missing"
    else:
        entry_type = "unsupported"

    return ObservedLink(
        target_path=target_path,
        entry_type=entry_type,
        expected_canonical=expected_canonical,
        canonical_valid=canonical_valid,
        canonical_error=canonical_error,
        raw_link_target=raw_link_target,
        resolved_link_target=resolved_link_target,
        link_points_to_canonical=link_points_to_canonical,
        is_same_object=is_same_object,
        target_lstat=target_lstat,
        target_kind=target_kind,
    )


def plan_link_target(
    observed: ObservedLink,
    desired_mode: str = "link",
    *,
    availability_status: str = "installed",
    parent_exists: bool | None = None,
    has_state_record: bool = False,
    is_legacy_container: bool = False,
    resource_name: str = "",
) -> LinkOperation:
    """Evaluate observed link facts against desired state to produce a deterministic LinkOperation."""
    target_path = observed.target_path
    canonical = observed.expected_canonical
    res_label = f" for '{resource_name}'" if resource_name else ""

    # 1. Legacy container migration
    if is_legacy_container:
        if observed.entry_type == "symlink":
            if observed.link_points_to_canonical:
                return LinkOperation(
                    action="MIGRATE_CONTAINER",
                    rule_id="INV-GLB-04",
                    target_path=target_path,
                    canonical_path=canonical,
                    reason=f"Migrate legacy container symlink at {target_path} to directory",
                    expected_representation="symlink",
                    desired_representation="dir",
                    is_authorized=True,
                )
            return LinkOperation(
                action="CONFLICT",
                rule_id="INV-GLB-04",
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Legacy container symlink points outside workspace: {target_path}",
                finding=f"Legacy container symlink points outside workspace: {target_path}",
                expected_representation="symlink",
                desired_representation="dir",
                is_authorized=False,
            )
        if observed.entry_type == "dir":
            return LinkOperation(
                action="NOOP",
                rule_id="INV-GLB-04",
                target_path=target_path,
                canonical_path=canonical,
                reason="Managed container directory already exists",
                expected_representation="dir",
                desired_representation="dir",
                is_authorized=True,
            )
        if observed.entry_type == "missing":
            return LinkOperation(
                action="CREATE",
                rule_id="INV-GLB-04",
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Create managed container directory: {target_path}",
                expected_representation="missing",
                desired_representation="dir",
                is_authorized=True,
            )
        return LinkOperation(
            action="CONFLICT",
            rule_id="INV-GLB-04",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Container path is an invalid entry type: {observed.entry_type}",
            finding=f"Container path is invalid entry type: {target_path}",
            expected_representation=observed.entry_type,
            desired_representation="dir",
            is_authorized=False,
        )

    # 2. Same-object disposition
    if observed.is_same_object:
        return LinkOperation(
            action="SHARED_PATH",
            rule_id="INV-GLB-06",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Target path is the same physical object as canonical container; no link required{res_label}",
            expected_representation=observed.entry_type,
            desired_representation="link",
            is_same_object=True,
            is_authorized=True,
        )

    # 3. Selected link (desired_mode == "link")
    if desired_mode == "link":
        # Check canonical validity first
        if not observed.canonical_valid:
            if observed.entry_type == "symlink" and observed.link_points_to_canonical:
                return LinkOperation(
                    action="CONFLICT",
                    rule_id="INV-TR-04",
                    target_path=target_path,
                    canonical_path=canonical,
                    reason=f"Broken symbolic link points to missing canonical skill '{resource_name}'"
                    if resource_name
                    else "Broken symbolic link points to missing canonical resource",
                    finding=f"Broken symbolic link points to missing canonical skill: {target_path}"
                    if resource_name
                    else f"Broken symbolic link points to missing canonical resource: {target_path}",
                    expected_representation="symlink",
                    desired_representation="link",
                    is_authorized=False,
                )
            err = (
                observed.canonical_error
                or (
                    f"Canonical skill '{resource_name}' is missing or unreadable"
                    if resource_name
                    else "Canonical source is missing or unreadable"
                )
            )
            return LinkOperation(
                action="CONFLICT",
                rule_id="INV-TR-02",
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Canonical skill source missing or unreadable: {err}"
                if resource_name
                else f"Canonical source missing or unreadable: {err}",
                finding=f"Canonical skill source missing or unreadable: {err}"
                if resource_name
                else f"Canonical source missing or unreadable: {err}",
                expected_representation=observed.entry_type,
                desired_representation="link",
                is_authorized=False,
            )

        # Consumer link parent availability check
        requires_parent = False
        if observed.target_kind == "consumer_link":
            parent = target_path.parent
            p_exists = parent_exists if parent_exists is not None else parent.exists()
            if not p_exists:
                if availability_status == "not_installed":
                    return LinkOperation(
                        action="SKIP",
                        rule_id="INV-GLB-05",
                        target_path=target_path,
                        canonical_path=canonical,
                        reason=f"Agent not detected: {parent}",
                        expected_representation="missing",
                        desired_representation="link",
                        is_authorized=True,
                    )
                if availability_status == "unknown":
                    return LinkOperation(
                        action="SKIP",
                        rule_id="INV-GLB-05",
                        target_path=target_path,
                        canonical_path=canonical,
                        reason=f"Agent installation unknown; parent directory does not exist: {parent}",
                        finding=f"Agent installation unknown and parent missing: {parent}",
                        expected_representation="missing",
                        desired_representation="link",
                        is_authorized=True,
                    )
                requires_parent = True

        if observed.entry_type == "missing":
            return LinkOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Create symbolic link for skill '{resource_name}'"
                if resource_name
                else f"Create symbolic link: {target_path} -> {canonical}",
                expected_representation="missing",
                desired_representation="link",
                requires_parent_creation=requires_parent,
                is_authorized=True,
            )

        if observed.entry_type == "symlink":
            if observed.link_points_to_canonical:
                return LinkOperation(
                    action="NOOP",
                    rule_id="INV-TR-03",
                    target_path=target_path,
                    canonical_path=canonical,
                    reason=f"Symbolic link for skill '{resource_name}' already points to canonical resource"
                    if resource_name
                    else "Symbolic link already points to canonical resource",
                    expected_representation="link",
                    desired_representation="link",
                    is_authorized=True,
                )
            dest = (
                observed.raw_link_target
                or observed.resolved_link_target
                or "unknown"
            )
            if observed.target_kind == "consumer_link":
                rule = "INV-GLB-05"
            elif has_state_record:
                rule = "INV-TR-06"
            else:
                rule = "INV-TR-05"
            return LinkOperation(
                action="CONFLICT",
                rule_id=rule,
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Symbolic link points to unauthorized destination: {dest}",
                finding=f"Symbolic link points to unauthorized destination: {target_path} -> {dest}",
                expected_representation="symlink",
                desired_representation="link",
                is_authorized=False,
            )

        if observed.entry_type == "dir":
            rule = (
                "INV-GLB-01"
                if observed.target_kind != "managed_entry"
                else "INV-GLB-02"
            )
            return LinkOperation(
                action="CONFLICT",
                rule_id=rule,
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Cannot switch copy to link for skill '{resource_name}': directory is drifted, unmanaged, or inactive"
                if resource_name
                else f"Target path is an unmanaged directory: {target_path}",
                finding=f"Cannot switch copy to link for skill '{resource_name}': {target_path} is not an unchanged active copy"
                if resource_name
                else f"Target path is an unmanaged directory: {target_path}",
                expected_representation="copy" if resource_name else "dir",
                desired_representation="link",
                is_authorized=False,
            )

        return LinkOperation(
            action="CONFLICT",
            rule_id="INV-TR-13",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Unsupported target filesystem entry for skill '{resource_name}'"
            if resource_name
            else f"Unsupported target filesystem entry: {target_path}",
            finding=f"Unsupported target filesystem entry: {target_path}",
            expected_representation="unsupported",
            desired_representation="link",
            is_authorized=False,
        )

    # 4. Deselected link (desired_mode == "absent")
    if observed.entry_type == "symlink":
        if observed.link_points_to_canonical:
            return LinkOperation(
                action="UNLINK",
                rule_id="INV-TR-14",
                target_path=target_path,
                canonical_path=canonical,
                reason=f"Remove deselected symbolic link for skill '{resource_name}'"
                if resource_name
                else f"Remove deselected symbolic link: {target_path}",
                expected_representation="link",
                desired_representation="absent",
                is_authorized=True,
            )
        dest = (
            observed.raw_link_target
            or observed.resolved_link_target
            or "unknown"
        )
        return LinkOperation(
            action="NOOP",
            rule_id="INV-TR-15",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Preserve unmanaged symbolic link for deselected skill '{resource_name}'"
            if resource_name
            else "Preserve unmanaged symbolic link for deselected entry",
            finding=None,
            expected_representation="symlink",
            desired_representation="absent",
            is_authorized=True,
        )

    if observed.entry_type == "dir":
        return LinkOperation(
            action="NOOP",
            rule_id="INV-GLB-03",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Preserve unmanaged directory for deselected skill '{resource_name}'"
            if resource_name
            else "Preserve unmanaged directory for deselected entry",
            expected_representation="copy" if resource_name else "dir",
            desired_representation="absent",
            is_authorized=True,
        )

    if observed.entry_type == "missing":
        return LinkOperation(
            action="NOOP",
            rule_id="INV-GLB-03",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Entry is already absent{res_label}",
            expected_representation="missing",
            desired_representation="absent",
            is_authorized=True,
        )

    return LinkOperation(
        action="NOOP",
        rule_id="INV-GLB-03",
        target_path=target_path,
        canonical_path=canonical,
        reason=f"Preserve unmanaged entry of type {observed.entry_type}{res_label}",
        expected_representation=observed.entry_type,
        desired_representation="absent",
        is_authorized=True,
    )


class SymlinkVerdict(Enum):
    OK = "OK"  # is_symlink, target exists, resolves to expected
    DANGLING = "DANGLING"  # is_symlink, target does not exist
    WRONG_TARGET = "WRONG_TARGET"  # is_symlink, target exists but resolves elsewhere
    NOT_SYMLINK = "NOT_SYMLINK"  # path exists but is a regular file or directory
    MISSING = "MISSING"  # path does not exist at all and is not a symlink


def classify_symlink(path: Path, expected_source: Path) -> SymlinkVerdict:
    """Classify a symlink target against an expected canonical source.

    Args:
        path: The filesystem path to inspect.
        expected_source: The canonical source that ``path`` should point to.

    Returns:
        A :class:`SymlinkVerdict` value describing the relationship.
    """
    if path.is_symlink():
        # Use strict=True so dangling symlinks raise OSError rather than
        # silently resolving to a non-existent path.
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return SymlinkVerdict.DANGLING
        if os.path.normcase(str(resolved)) == os.path.normcase(
            str(expected_source.resolve())
        ):
            return SymlinkVerdict.OK
        return SymlinkVerdict.WRONG_TARGET
    if path.exists():
        return SymlinkVerdict.NOT_SYMLINK
    return SymlinkVerdict.MISSING


# Coarse-grained mappings used by status (backwards-compatible labels)
_STATUS_MAP: dict[SymlinkVerdict, str] = {
    SymlinkVerdict.OK: "OK",
    SymlinkVerdict.DANGLING: "CONFLICT",
    SymlinkVerdict.WRONG_TARGET: "CONFLICT",
    SymlinkVerdict.NOT_SYMLINK: "CONFLICT",
    SymlinkVerdict.MISSING: "MISSING",
}


def symlink_verdict_to_status(verdict: SymlinkVerdict) -> str:
    """Convert a :class:`SymlinkVerdict` to a coarse status string.

    This mapping is intentionally conservative: any broken symlink state
    becomes ``"CONFLICT"`` so that ``aikito status`` surfaces it without
    exposing fine-grained detail. ``aikito doctor`` uses the verdict
    directly for precise diagnostics.
    """
    return _STATUS_MAP[verdict]
