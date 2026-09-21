"""Shared symlink classification utilities for aikito.

Both status and doctor depend on this module so that they
produce consistent verdicts about the same filesystem state. Neither
command module imports the other.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any

from .compat import (
    _resolve_symlink_target,
    get_physical_path,
    require_symlink_support,
    safe_symlink,
)


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
    target_kind: str = (
        "managed_entry"  # "managed_entry", "consumer_link", "managed_container"
    )
    scope: str = "project"  # "global", "project"


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
    target_kind: str = "managed_entry"
    resource_name: str = ""


def inspect_link_target(
    target_path: Path,
    expected_canonical: Path | None = None,
    *,
    canonical_valid: bool = True,
    canonical_error: str | None = None,
    target_kind: str = "managed_entry",
    scope: str = "project",
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
                        str(get_physical_path(resolved_link_target))
                    ) == os.path.normcase(str(get_physical_path(expected_canonical))):
                        link_points_to_canonical = True
                except (ValueError, OSError):
                    pass
            # Check raw target for broken links
            if not link_points_to_canonical and raw_link_target is not None:
                try:
                    if os.path.normcase(
                        str(get_physical_path(raw_link_target))
                    ) == os.path.normcase(str(get_physical_path(expected_canonical))):
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
        scope=scope,
    )


def _plan_link_target_impl(
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

    # 1. Container disposition
    if is_legacy_container or observed.target_kind == "managed_container":
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
            dest = (
                observed.raw_link_target or observed.resolved_link_target or "unknown"
            )
            is_sub = False
            if canonical is not None:
                try:
                    dest_resolved = get_physical_path(Path(dest))
                    canon_resolved = get_physical_path(canonical)
                    dest_str = os.path.normcase(str(dest_resolved))
                    canon_str = os.path.normcase(str(canon_resolved))
                    is_sub = dest_str != canon_str and (
                        dest_str.startswith(canon_str + os.sep)
                        or (
                            hasattr(dest_resolved, "is_relative_to")
                            and dest_resolved.is_relative_to(canon_resolved)
                        )
                    )
                except Exception:
                    is_sub = False
            detail = (
                "points to a subpath instead of skills root"
                if is_sub
                else "points outside current workspace"
            )
            return LinkOperation(
                action="CONFLICT",
                rule_id="INV-GLB-04",
                target_path=target_path,
                canonical_path=canonical,
                reason=(
                    f"Target preserved: {target_path}. "
                    f"Current legacy container symlink {detail}: {dest}, expected: {canonical}. "
                    f"Legacy container migration only allows exact root link to current workspace skills; "
                    f"inspect manually, then run 'aikito sync global' again."
                ),
                finding=f"Legacy container symlink {detail}: {target_path} -> {dest}",
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
            reason=(
                f"Target preserved: {target_path}. "
                f"Container path is an invalid entry type '{observed.entry_type}' (expected directory). "
                f"Will not overwrite automatically; inspect manually, then run 'aikito sync global' again."
            ),
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
            err = observed.canonical_error or (
                f"Canonical skill '{resource_name}' is missing or unreadable"
                if resource_name
                else "Canonical source is missing or unreadable"
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
                        reason=f"{resource_name} not detected: {parent}"
                        if resource_name
                        else f"Agent not detected: {parent}",
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
                observed.raw_link_target or observed.resolved_link_target or "unknown"
            )
            if observed.target_kind == "consumer_link":
                rule = "INV-GLB-05"
                reason_msg = (
                    f"Target preserved: {target_path}. "
                    f"Symbolic link points to unauthorized destination: {dest} (expected {canonical}). "
                    f"External or unexpected consumer symlink will not be overwritten automatically; "
                    f"inspect manually, then run 'aikito sync global' again."
                )
            elif has_state_record:
                rule = "INV-TR-06"
                reason_msg = (
                    f"Target preserved: {target_path}. "
                    f"Symbolic link points to unauthorized destination: {dest} (expected {canonical}). "
                    f"Other workspace or unmanaged skill symlink will not be overwritten automatically; "
                    f"inspect manually, then run 'aikito sync global' again."
                )
            else:
                rule = "INV-TR-05"
                reason_msg = (
                    f"Target preserved: {target_path}. "
                    f"Symbolic link points to unauthorized destination: {dest} (expected {canonical}). "
                    f"Other workspace or unmanaged skill symlink will not be overwritten automatically; "
                    f"inspect manually, then run 'aikito sync global' again."
                )
            return LinkOperation(
                action="CONFLICT",
                rule_id=rule,
                target_path=target_path,
                canonical_path=canonical,
                reason=reason_msg,
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
                reason=(
                    f"Target preserved: {target_path}. "
                    f"Target is a regular directory (expected symlink to {canonical}). "
                    f"Unmanaged or matching directory will not be overwritten automatically; "
                    f"move or merge it manually, then run 'aikito sync global' again."
                )
                if observed.scope == "global"
                else (
                    f"Cannot switch copy to link for skill '{resource_name}': directory is drifted, unmanaged, or inactive"
                    if resource_name
                    else f"Target path is an unmanaged directory: {target_path}"
                ),
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
            reason=(
                f"Target preserved: {target_path}. "
                f"Target is an unexpected entry type '{observed.entry_type}' (expected link to {canonical}). "
                f"Will not overwrite automatically; inspect manually, then run 'aikito sync global' again."
            )
            if observed.scope == "global"
            else (
                f"Unsupported target filesystem entry for skill '{resource_name}'"
                if resource_name
                else f"Unsupported target filesystem entry: {target_path}"
            ),
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
        dest = observed.raw_link_target or observed.resolved_link_target or "unknown"
        if observed.scope == "global":
            return LinkOperation(
                action="CONFLICT",
                rule_id="INV-GLB-03",
                target_path=target_path,
                canonical_path=canonical,
                reason=(
                    f"Target preserved: {target_path}. "
                    f"Stale entry symlink destination: {dest}, expected: {canonical}. "
                    f"Other workspace or unmanaged skill symlink will not be deleted automatically; "
                    f"inspect manually, then run 'aikito sync global' again."
                ),
                finding=f"Unmanaged global skill item: {target_path}",
                expected_representation="symlink",
                desired_representation="absent",
                is_authorized=False,
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
        if observed.scope == "global":
            return LinkOperation(
                action="CONFLICT",
                rule_id="INV-GLB-03",
                target_path=target_path,
                canonical_path=canonical,
                reason=(
                    f"Target preserved: {target_path}. "
                    f"Stale target is a regular directory (expected symlink to {canonical}). "
                    f"Matching directory is not owned by link-only global skills and will not be deleted; "
                    f"inspect or remove manually, then run 'aikito sync global' again."
                ),
                finding=f"Stale target is a regular directory: {target_path}",
                expected_representation="dir",
                desired_representation="absent",
                is_authorized=False,
            )
        return LinkOperation(
            action="NOOP",
            rule_id="INV-TR-17",
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
            rule_id="INV-GLB-03" if observed.scope == "global" else "INV-TR-17",
            target_path=target_path,
            canonical_path=canonical,
            reason=f"Entry is already absent{res_label}",
            expected_representation="missing",
            desired_representation="absent",
            is_authorized=True,
        )

    if observed.scope == "global":
        return LinkOperation(
            action="CONFLICT",
            rule_id="INV-GLB-03",
            target_path=target_path,
            canonical_path=canonical,
            reason=(
                f"Target preserved: {target_path}. "
                f"Stale target is an unexpected entry type '{observed.entry_type}'. "
                f"Unmanaged item will not be deleted automatically; inspect manually, then run 'aikito sync global' again."
            ),
            finding=f"Unmanaged global skill item: {target_path}",
            expected_representation=observed.entry_type,
            desired_representation="absent",
            is_authorized=False,
        )

    return LinkOperation(
        action="NOOP",
        rule_id="INV-TR-15",
        target_path=target_path,
        canonical_path=canonical,
        reason=f"Preserve unmanaged entry of type {observed.entry_type}{res_label}",
        expected_representation=observed.entry_type,
        desired_representation="absent",
        is_authorized=True,
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
    op = _plan_link_target_impl(
        observed,
        desired_mode=desired_mode,
        availability_status=availability_status,
        parent_exists=parent_exists,
        has_state_record=has_state_record,
        is_legacy_container=is_legacy_container,
        resource_name=resource_name,
    )
    t_kind = "managed_container" if is_legacy_container else observed.target_kind
    return replace(op, target_kind=t_kind, resource_name=resource_name)


@dataclass(frozen=True)
class LinkExecutionResult:
    """Result of applying a single LinkOperation."""

    operation: LinkOperation
    success: bool
    applied: bool  # True if filesystem was actually mutated
    error_message: str | None = None


def apply_link_operation(
    op: LinkOperation,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> LinkExecutionResult:
    """Apply a planned LinkOperation with strict preflight verification."""
    target = op.target_path
    canonical = op.canonical_path

    if op.action in ("NOOP", "SHARED_PATH"):
        if op.action == "SHARED_PATH":
            if op.target_kind == "consumer_link":
                print(f"[OK] {op.resource_name} skills: shared path {target}")
            elif verbose:
                print(f"[OK] shared path {target}")
        elif op.target_kind == "consumer_link":
            print(f"[OK] {op.resource_name} skills: {target} -> {canonical}")
        return LinkExecutionResult(operation=op, success=True, applied=False)

    if op.action == "SKIP":
        if op.reason:
            print(f"[SKIP] {op.reason}")
        return LinkExecutionResult(operation=op, success=True, applied=False)

    if op.action == "CONFLICT":
        return LinkExecutionResult(
            operation=op, success=False, applied=False, error_message=op.reason
        )

    if op.action == "MIGRATE_CONTAINER":
        # Preflight: must still be a symlink pointing to canonical root
        if not target.is_symlink():
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: container is no longer a symlink: {target}",
            )
        resolved = _resolve_symlink_target(target)
        if canonical is not None and resolved is not None:
            if os.path.normcase(str(get_physical_path(resolved))) != os.path.normcase(
                str(get_physical_path(canonical))
            ):
                return LinkExecutionResult(
                    operation=op,
                    success=False,
                    applied=False,
                    error_message=f"Preflight failed: container symlink changed destination: {target} -> {resolved}",
                )
        print(f"[INFO] Replacing old top-level symlink at {target} with directory")
        if not dry_run:
            try:
                target.unlink()
                target.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return LinkExecutionResult(
                    operation=op,
                    success=False,
                    applied=False,
                    error_message=f"Failed to migrate container {target}: {exc}",
                )
        return LinkExecutionResult(operation=op, success=True, applied=not dry_run)

    if op.action == "CREATE":
        if op.desired_representation == "dir":
            if not dry_run:
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    return LinkExecutionResult(
                        operation=op,
                        success=False,
                        applied=False,
                        error_message=f"Failed to create directory {target}: {exc}",
                    )
            return LinkExecutionResult(operation=op, success=True, applied=not dry_run)

        require_symlink_support()
        if dry_run:
            if op.target_kind == "consumer_link":
                print(
                    f"[DRY RUN LINK] {op.resource_name} skills: {target} -> {canonical}"
                )
            else:
                print(f"[DRY RUN LINK] {canonical} -> {target}")
            return LinkExecutionResult(operation=op, success=True, applied=False)

        if canonical is None or not canonical.exists():
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: canonical source does not exist: {canonical} (stale plan)",
            )
        if op.target_kind == "managed_entry" and not canonical.is_dir():
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: canonical skill source is not a directory: {canonical} (stale plan)",
            )
        if op.target_kind == "consumer_link" and not (
            canonical.is_dir() and not canonical.is_symlink()
        ):
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: managed container {canonical} is not a valid directory (stale plan)",
            )

        # Preflight: verify container identity for managed entry
        if op.target_kind == "managed_entry":
            container = target.parent
            if not (container.is_dir() and not container.is_symlink()):
                return LinkExecutionResult(
                    operation=op,
                    success=False,
                    applied=False,
                    error_message=f"Preflight failed: managed container {container} is not a valid directory (stale plan)",
                )

        # Preflight: verify consumer parent availability
        if op.target_kind == "consumer_link" and not op.requires_parent_creation:
            if not target.parent.exists():
                return LinkExecutionResult(
                    operation=op,
                    success=False,
                    applied=False,
                    error_message=f"Preflight failed: consumer parent directory missing for {target} (stale plan)",
                )

        # Preflight: target must not exist
        if target.is_symlink() or target.exists():
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: target already exists or changed: {target} (stale plan)",
            )

        try:
            if op.requires_parent_creation:
                target.parent.mkdir(parents=True, exist_ok=True)
            if not safe_symlink(canonical, target):
                return LinkExecutionResult(
                    operation=op,
                    success=False,
                    applied=False,
                    error_message=f"Failed to create symlink: {target} -> {canonical}",
                )
            if op.target_kind == "consumer_link":
                print(f"[LINK] {op.resource_name} skills: {target} -> {canonical}")
            else:
                print(f"[LINK] {target} -> {canonical}")
            return LinkExecutionResult(operation=op, success=True, applied=True)
        except OSError as exc:
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Failed to create symlink: {exc}",
            )

    if op.action == "UNLINK":
        # Preflight: must still be a symlink pointing to canonical
        if not target.is_symlink():
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: target is not a symlink: {target}",
            )
        # Check ownership evidence before deleting
        raw_val = ""
        try:
            raw_val = os.readlink(target)
        except OSError:
            pass
        resolved = _resolve_symlink_target(target)
        owned = False
        if canonical is not None:
            canon_norm = os.path.normcase(str(get_physical_path(canonical)))
            if (
                resolved is not None
                and os.path.normcase(str(get_physical_path(resolved))) == canon_norm
            ):
                owned = True
            elif raw_val:
                raw_path = (
                    target.parent / raw_val
                    if not os.path.isabs(raw_val)
                    else Path(raw_val)
                )
                if os.path.normcase(str(get_physical_path(raw_path))) == canon_norm:
                    owned = True
        if not owned:
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Preflight failed: link {target} no longer points to canonical {canonical}",
            )

        if dry_run:
            print(f"[DRY RUN CLEANUP] Would remove stale managed item: {target}")
            return LinkExecutionResult(operation=op, success=True, applied=False)

        try:
            target.unlink()
            print(f"[CLEANUP] Removed stale managed item: {target}")
            return LinkExecutionResult(operation=op, success=True, applied=True)
        except OSError as exc:
            return LinkExecutionResult(
                operation=op,
                success=False,
                applied=False,
                error_message=f"Failed to remove stale link {target}: {exc}",
            )

    return LinkExecutionResult(
        operation=op,
        success=False,
        applied=False,
        error_message=f"Unknown link action: {op.action}",
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
