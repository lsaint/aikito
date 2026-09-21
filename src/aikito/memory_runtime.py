"""Project memory runtime visibility models, planning, and execution.

Provides unified Link-only planning and execution for project memory runtime visibility,
encompassing workspace memory references and project notes canonical sources.
"""

from __future__ import annotations

import os
import stat
import tomllib
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compat import safe_symlink
from .link import (
    LinkOperation,
    apply_link_operation,
    inspect_link_target,
)


@dataclass(frozen=True)
class MemoryResource:
    """A logical canonical memory resource to expose to project runtime."""

    identity: str
    canonical_source: Path
    relative_target: Path
    source_kind: str  # "workspace_ref" | "project_notes"
    exists: bool = True
    is_dir: bool = False
    is_safe: bool = True
    safety_error: str | None = None
    expected_mode_type: int | None = None


@dataclass(frozen=True)
class MemoryBatch:
    """Structure representing a project memory synchronization request."""

    project_name: str
    workspace_root: Path
    active_checkouts: tuple[Path, ...]
    offline_checkouts: tuple[Path, ...] = ()
    resources: tuple[MemoryResource, ...] = ()
    selected_references: tuple[str, ...] = ()
    planned_notes_canonical: Path | None = None
    planned_has_proj_mem: bool = False
    planned_notes_is_dir: bool = False
    planned_agent_toml_memory: tuple[str, ...] | None = None
    planned_agent_toml_exists: bool = False

    @property
    def resource_count(self) -> int:
        return len(self.resources)

    @property
    def has_offline(self) -> bool:
        return bool(self.offline_checkouts)


@dataclass(frozen=True)
class MemoryPlan:
    """Immutable, fully-evaluated synchronization plan for project memory."""

    batch: MemoryBatch
    operations: tuple[LinkOperation, ...] = ()

    @property
    def conflicts(self) -> tuple[LinkOperation, ...]:
        return tuple(op for op in self.operations if op.action == "CONFLICT")

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def can_apply(self) -> bool:
        return not self.has_conflicts and all(
            op.is_authorized for op in self.operations
        )

    @property
    def planned_change_count(self) -> int:
        return sum(1 for op in self.operations if op.action in ("CREATE", "UNLINK"))

    @property
    def noop_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "NOOP")

    @property
    def skip_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "SKIP")

    @property
    def findings(self) -> tuple[str, ...]:
        return tuple(op.finding for op in self.operations if op.finding is not None)


@dataclass(frozen=True)
class MemoryExecutionResult:
    """Result of executing a MemoryPlan."""

    operations: tuple[LinkOperation, ...] = ()
    success: bool = True
    applied_count: int = 0
    skipped_count: int = 0
    noop_count: int = 0
    conflict_count: int = 0
    error_message: str | None = None


def resolve_project_notes_source(
    workspace_root: Path, project_name: str
) -> tuple[Path, bool]:
    """Resolve project memory notes canonical source adhering to INV-MEM-03 precedence.

    Returns:
        (canonical_notes_path, has_project_memory_root)
    """
    proj_mem = workspace_root / "projects" / project_name / "memory"
    if proj_mem.is_symlink() or proj_mem.exists():
        return proj_mem / "notes", True
    return workspace_root / "memory" / project_name / "notes", False


def validate_memory_canonical_source(
    canonical: Path,
    workspace_root: Path,
    source_kind: str,
    *,
    project_name: str | None = None,
) -> tuple[bool, str | None]:
    """Validate canonical memory source safety, type, and boundary adherence.

    Args:
        canonical: Path to canonical source file or directory.
        workspace_root: Canonical workspace root directory.
        source_kind: One of "project_notes", "workspace_ref", "legacy_notes".
        project_name: Optional project name for project_notes.

    Returns:
        (is_safe, error_message)
    """
    try:
        ws_resolved = workspace_root.resolve(strict=False)
    except (ValueError, OSError) as exc:
        return False, f"Invalid workspace root: {exc}"

    if source_kind == "project_notes":
        proj_dir = (
            workspace_root / "projects" / project_name
            if project_name
            else canonical.parent.parent
        )
        try:
            proj_resolved = proj_dir.resolve(strict=False)
        except (ValueError, OSError) as exc:
            return False, f"Invalid project directory: {exc}"

        proj_mem = proj_dir / "memory"

        # 1. Project memory root validation
        if proj_mem.is_symlink() or proj_mem.exists():
            if proj_mem.is_symlink():
                try:
                    if not proj_mem.exists():
                        return (
                            False,
                            f"Project memory root is a broken symlink: {proj_mem}",
                        )
                    mem_resolved = proj_mem.resolve()
                    if not mem_resolved.is_dir():
                        return (
                            False,
                            f"Project memory root must be a directory: {proj_mem}",
                        )
                    if not mem_resolved.is_relative_to(
                        proj_resolved
                    ) or not mem_resolved.is_relative_to(ws_resolved):
                        return (
                            False,
                            f"Project memory root escapes project boundary: {proj_mem}",
                        )
                except OSError as exc:
                    return (
                        False,
                        f"Project memory root symlink resolution error: {exc}",
                    )
            elif not proj_mem.is_dir() or proj_mem.is_file():
                return False, f"Project memory root must be a directory: {proj_mem}"

        # 2. Project notes canonical validation
        if canonical.is_symlink() or canonical.exists():
            if canonical.is_symlink():
                try:
                    if not canonical.exists():
                        return (
                            False,
                            f"Project notes canonical is a broken symlink: {canonical}",
                        )
                    can_resolved = canonical.resolve()
                    if not can_resolved.is_dir():
                        return (
                            False,
                            f"Project notes source must be a directory: {canonical}",
                        )
                    mem_resolved = proj_mem.resolve(strict=False)
                    if not can_resolved.is_relative_to(
                        mem_resolved
                    ) or not can_resolved.is_relative_to(ws_resolved):
                        return (
                            False,
                            f"Project notes canonical escapes project memory boundary: {canonical}",
                        )
                except OSError as exc:
                    return (
                        False,
                        f"Project notes canonical resolution error: {exc}",
                    )
            elif not canonical.is_dir() or canonical.is_file():
                return (
                    False,
                    f"Project notes source must be a directory: {canonical}",
                )

        return True, None

    elif source_kind == "workspace_ref":
        ws_mem_root = workspace_root / "memory"
        # 1. Workspace memory root validation
        if ws_mem_root.is_symlink() or ws_mem_root.exists():
            if ws_mem_root.is_symlink():
                try:
                    if not ws_mem_root.exists():
                        return (
                            False,
                            f"Workspace memory root is a broken symlink: {ws_mem_root}",
                        )
                    mem_resolved = ws_mem_root.resolve()
                    if not mem_resolved.is_dir():
                        return (
                            False,
                            f"Workspace memory root must be a directory: {ws_mem_root}",
                        )
                    if not mem_resolved.is_relative_to(ws_resolved):
                        return (
                            False,
                            f"Workspace memory root escapes workspace boundary: {ws_mem_root}",
                        )
                except OSError as exc:
                    return (
                        False,
                        f"Workspace memory root symlink resolution error: {exc}",
                    )
            elif not ws_mem_root.is_dir():
                return (
                    False,
                    f"Workspace memory root must be a directory: {ws_mem_root}",
                )

        # 2. Canonical reference boundary validation
        try:
            can_resolved = canonical.resolve(strict=False)
            mem_resolved = ws_mem_root.resolve(strict=False)
            if not can_resolved.is_relative_to(
                mem_resolved
            ) or not can_resolved.is_relative_to(ws_resolved):
                return (
                    False,
                    f"Memory canonical path escapes workspace memory root: {canonical}",
                )
        except (ValueError, OSError) as exc:
            return False, f"Invalid canonical path: {exc}"

        if canonical.is_symlink():
            try:
                if not canonical.exists():
                    return (
                        False,
                        f"Memory canonical is a broken symlink: {canonical}",
                    )
                target_resolved = canonical.resolve()
                if not target_resolved.is_relative_to(
                    mem_resolved
                ) or not target_resolved.is_relative_to(ws_resolved):
                    return (
                        False,
                        f"Memory canonical symlink escapes workspace memory root: {canonical}",
                    )
            except OSError as exc:
                return (
                    False,
                    f"Memory canonical symlink resolution error: {exc}",
                )

        return True, None

    elif source_kind == "legacy_notes":
        ws_mem_root = workspace_root / "memory"
        if ws_mem_root.is_symlink() or ws_mem_root.exists():
            if ws_mem_root.is_symlink():
                try:
                    if not ws_mem_root.exists():
                        return (
                            False,
                            f"Workspace memory root is a broken symlink: {ws_mem_root}",
                        )
                    mem_resolved = ws_mem_root.resolve()
                    if not mem_resolved.is_relative_to(ws_resolved):
                        return (
                            False,
                            f"Workspace memory root escapes workspace boundary: {ws_mem_root}",
                        )
                except OSError as exc:
                    return (
                        False,
                        f"Workspace memory root symlink resolution error: {exc}",
                    )

        if canonical.is_symlink() or canonical.exists():
            if canonical.is_symlink():
                try:
                    if not canonical.exists():
                        return (
                            False,
                            f"Legacy notes canonical is a broken symlink: {canonical}",
                        )
                    can_resolved = canonical.resolve()
                    mem_resolved = ws_mem_root.resolve(strict=False)
                    if not can_resolved.is_relative_to(
                        mem_resolved
                    ) or not can_resolved.is_relative_to(ws_resolved):
                        return (
                            False,
                            f"Legacy notes canonical escapes workspace memory root: {canonical}",
                        )
                except OSError as exc:
                    return (
                        False,
                        f"Legacy notes canonical resolution error: {exc}",
                    )
            elif not canonical.is_dir():
                return (
                    False,
                    f"Legacy notes source must be a directory: {canonical}",
                )

        return True, None

    return True, None

    return True, None


def validate_memory_reference(
    ref: str,
    workspace_root: Path,
) -> tuple[bool, Path | None, str | None]:
    """Validate that ref is a safe relative path that does not escape workspace memory root."""
    if not ref or not ref.strip():
        return False, None, "Empty memory reference"

    p = Path(ref)
    if p.is_absolute():
        return False, None, f"Memory reference cannot be absolute: {ref}"
    if ".." in p.parts:
        return (
            False,
            None,
            f"Memory reference cannot contain '..' traversal: {ref}",
        )

    ws_mem_root = workspace_root / "memory"
    canonical = ws_mem_root / p
    is_safe, err = validate_memory_canonical_source(
        canonical, workspace_root, "workspace_ref"
    )
    if not is_safe:
        return False, None, err

    return True, p, None


def validate_checkout_target_boundary(
    checkout: Path,
    relative_target: Path,
) -> tuple[bool, Path | None, str | None]:
    """Validate that checkout/.agents/memory/relative_target does not escape checkout."""
    if relative_target.is_absolute():
        return False, None, f"Target path cannot be absolute: {relative_target}"
    if ".." in relative_target.parts:
        return (
            False,
            None,
            f"Target path cannot contain '..' traversal: {relative_target}",
        )

    agents_dir = checkout / ".agents"
    if agents_dir.is_symlink():
        return (
            False,
            None,
            f"Checkout .agents directory cannot be a symlink: {agents_dir}",
        )

    runtime_memory_dir = agents_dir / "memory"
    if runtime_memory_dir.is_symlink():
        return (
            False,
            None,
            f"Checkout .agents/memory directory cannot be a symlink: {runtime_memory_dir}",
        )

    target_path = runtime_memory_dir / relative_target
    try:
        norm_target = Path(os.path.normpath(target_path))
        norm_mem = Path(os.path.normpath(runtime_memory_dir))
        norm_co = Path(os.path.normpath(checkout))
        if not norm_target.is_relative_to(norm_mem) or not norm_target.is_relative_to(
            norm_co
        ):
            return (
                False,
                None,
                f"Target path escapes checkout memory directory: {target_path}",
            )
    except (ValueError, OSError) as exc:
        return False, None, f"Invalid target path: {exc}"

    # Check intermediate directories under runtime_memory_dir
    curr = runtime_memory_dir
    for part in relative_target.parent.parts:
        curr = curr / part
        if curr.is_symlink():
            return (
                False,
                None,
                f"Intermediate directory in memory target path cannot be a symlink: {curr}",
            )

    return True, target_path, None


def build_project_memory_batch(
    workspace_root: Path,
    project_name: str,
    data: Mapping[str, Any],
    *,
    active_checkouts: Sequence[Path],
    offline_checkouts: Sequence[Path] = (),
) -> MemoryBatch:
    """Construct an immutable MemoryBatch from workspace definitions and checkout scopes."""
    memory_refs = [str(m) for m in data.get("memory", [])]
    resources: list[MemoryResource] = []
    seen_targets: set[Path] = set()

    # Track agent.toml presence and memory config
    agent_toml = workspace_root / "projects" / project_name / "agent.toml"
    agent_toml_exists = agent_toml.is_file()
    agent_toml_memory: tuple[str, ...] | None = None
    if agent_toml_exists:
        try:
            cfg = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
            agent_toml_memory = tuple(str(m) for m in cfg.get("memory", []))
        except Exception:
            agent_toml_memory = None

    # 1. Project notes special-case (INV-MEM-03 precedence over workspace_ref)
    notes_canonical, has_proj_mem = resolve_project_notes_source(
        workspace_root, project_name
    )
    notes_kind = "project_notes" if has_proj_mem else "legacy_notes"
    is_notes_safe, notes_err = validate_memory_canonical_source(
        notes_canonical,
        workspace_root,
        notes_kind,
        project_name=project_name,
    )
    planned_notes_is_dir = (
        notes_canonical.is_dir() if notes_canonical.exists() else False
    )

    if not is_notes_safe:
        resources.append(
            MemoryResource(
                identity="project_notes",
                canonical_source=notes_canonical,
                relative_target=Path("notes"),
                source_kind="project_notes",
                exists=notes_canonical.exists(),
                is_dir=planned_notes_is_dir,
                is_safe=False,
                safety_error=notes_err,
            )
        )
        seen_targets.add(Path("notes"))
    elif planned_notes_is_dir:
        expected_mode_type = None
        try:
            st = notes_canonical.lstat()
            expected_mode_type = stat.S_IFMT(st.st_mode)
        except OSError:
            pass
        resources.append(
            MemoryResource(
                identity="project_notes",
                canonical_source=notes_canonical,
                relative_target=Path("notes"),
                source_kind="project_notes",
                exists=True,
                is_dir=True,
                is_safe=True,
                expected_mode_type=expected_mode_type,
            )
        )
        seen_targets.add(Path("notes"))

    # 2. Workspace memory references
    ws_mem_root = workspace_root / "memory"
    for ref in memory_refs:
        is_safe, rel_path, err = validate_memory_reference(ref, workspace_root)
        if not is_safe or rel_path is None:
            resources.append(
                MemoryResource(
                    identity=f"workspace_ref:{ref}",
                    canonical_source=ws_mem_root / ref,
                    relative_target=Path(ref),
                    source_kind="workspace_ref",
                    exists=False,
                    is_dir=False,
                    is_safe=False,
                    safety_error=err or f"Unsafe memory reference: {ref}",
                )
            )
            continue

        # Project notes takes precedence over same-name workspace reference
        if rel_path in seen_targets:
            continue

        seen_targets.add(rel_path)
        canonical = ws_mem_root / rel_path
        exists = canonical.exists()
        is_dir = False
        expected_mode_type = None
        if exists:
            try:
                st = canonical.lstat()
                expected_mode_type = stat.S_IFMT(st.st_mode)
                is_dir = stat.S_ISDIR(st.st_mode)
            except OSError:
                pass
        resources.append(
            MemoryResource(
                identity=f"workspace_ref:{ref}",
                canonical_source=canonical,
                relative_target=rel_path,
                source_kind="workspace_ref",
                exists=exists,
                is_dir=is_dir,
                is_safe=True,
                expected_mode_type=expected_mode_type,
            )
        )

    return MemoryBatch(
        project_name=project_name,
        workspace_root=workspace_root,
        active_checkouts=tuple(active_checkouts),
        offline_checkouts=tuple(offline_checkouts),
        resources=tuple(resources),
        selected_references=tuple(memory_refs),
        planned_notes_canonical=notes_canonical,
        planned_has_proj_mem=has_proj_mem,
        planned_notes_is_dir=planned_notes_is_dir,
        planned_agent_toml_memory=agent_toml_memory,
        planned_agent_toml_exists=agent_toml_exists,
    )


def plan_project_memory(
    batch: MemoryBatch,
    *,
    is_offline: bool = False,
) -> MemoryPlan:
    """Pure planning: evaluate memory targets and produce deterministic LinkOperations without writes."""
    operations: list[LinkOperation] = []

    # 1. Offline checkout handling (INV-MEM-08)
    if is_offline or batch.offline_checkouts:
        offline_scope = (
            batch.active_checkouts if is_offline else batch.offline_checkouts
        )
        for checkout in offline_scope:
            for res in batch.resources:
                target_path = checkout / ".agents" / "memory" / res.relative_target
                operations.append(
                    LinkOperation(
                        action="SKIP",
                        rule_id="INV-MEM-08",
                        target_path=target_path,
                        canonical_path=res.canonical_source,
                        reason=f"Checkout is offline: {checkout}",
                        expected_representation="missing",
                        desired_representation="link",
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                        is_authorized=True,
                    )
                )
        if is_offline:
            return MemoryPlan(batch=batch, operations=tuple(operations))

    # Check for hierarchical target overlap across resources
    overlap_conflicts: dict[Path, str] = {}
    valid_resources = [r for r in batch.resources if r.is_safe]
    for i, r1 in enumerate(valid_resources):
        for j, r2 in enumerate(valid_resources):
            if i != j:
                p1 = r1.relative_target.parts
                p2 = r2.relative_target.parts
                if len(p2) > len(p1) and p2[: len(p1)] == p1:
                    overlap_conflicts[r2.relative_target] = (
                        f"Hierarchical memory target collision: {r1.relative_target} overlaps with {r2.relative_target}"
                    )

    # 2. Active checkout planning
    for checkout in batch.active_checkouts:
        runtime_memory_dir = checkout / ".agents" / "memory"

        # Boundary checks for .agents and .agents/memory
        agents_dir = checkout / ".agents"
        if agents_dir.is_symlink():
            finding = f"Checkout .agents directory cannot be a symlink: {agents_dir}"
            operations.append(
                LinkOperation(
                    action="CONFLICT",
                    rule_id="INV-MEM-02",
                    target_path=agents_dir,
                    reason=finding,
                    finding=finding,
                    is_authorized=False,
                    target_kind="memory_link",
                )
            )
            continue
        if runtime_memory_dir.is_symlink():
            finding = f"Checkout .agents/memory directory cannot be a symlink: {runtime_memory_dir}"
            operations.append(
                LinkOperation(
                    action="CONFLICT",
                    rule_id="INV-MEM-02",
                    target_path=runtime_memory_dir,
                    reason=finding,
                    finding=finding,
                    is_authorized=False,
                    target_kind="memory_link",
                )
            )
            continue

        # Plan desired resources
        for res in batch.resources:
            # 1. Unsafe resource check (INV-MEM-02)
            if not res.is_safe:
                target_path = runtime_memory_dir / res.relative_target
                finding = (
                    res.safety_error
                    or f"Unsafe memory reference: {res.relative_target}"
                )
                operations.append(
                    LinkOperation(
                        action="CONFLICT",
                        rule_id="INV-MEM-02",
                        target_path=target_path,
                        canonical_path=res.canonical_source,
                        reason=finding,
                        finding=finding,
                        is_authorized=False,
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )
                continue

            # 2. Hierarchical overlap check
            if res.relative_target in overlap_conflicts:
                target_path = runtime_memory_dir / res.relative_target
                finding = overlap_conflicts[res.relative_target]
                operations.append(
                    LinkOperation(
                        action="CONFLICT",
                        rule_id="INV-MEM-02",
                        target_path=target_path,
                        canonical_path=res.canonical_source,
                        reason=finding,
                        finding=finding,
                        is_authorized=False,
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )
                continue

            # 3. Target boundary check
            is_valid_target, validated_target, target_err = (
                validate_checkout_target_boundary(checkout, res.relative_target)
            )
            if not is_valid_target or validated_target is None:
                finding = (
                    target_err
                    or f"Target path escapes checkout memory directory: {res.relative_target}"
                )
                operations.append(
                    LinkOperation(
                        action="CONFLICT",
                        rule_id="INV-MEM-02",
                        target_path=runtime_memory_dir / res.relative_target,
                        canonical_path=res.canonical_source,
                        reason=finding,
                        finding=finding,
                        is_authorized=False,
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )
                continue

            target_path = validated_target
            canonical = res.canonical_source

            # Source missing check (INV-MEM-02, INV-MEM-07)
            if not res.exists:
                finding = f"Project memory source does not exist: {canonical}"
                operations.append(
                    LinkOperation(
                        action="CONFLICT",
                        rule_id="INV-MEM-07",
                        target_path=target_path,
                        canonical_path=canonical,
                        reason=finding,
                        finding=finding,
                        is_authorized=False,
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )
                continue

            obs = inspect_link_target(
                target_path,
                canonical,
                canonical_valid=True,
                target_kind="memory_link",
                scope="project",
            )

            if obs.entry_type == "missing":
                operations.append(
                    LinkOperation(
                        action="CREATE",
                        rule_id="INV-MEM-01",
                        target_path=target_path,
                        canonical_path=canonical,
                        reason=f"Create symbolic link to canonical memory {canonical}",
                        expected_representation="missing",
                        desired_representation="link",
                        requires_parent_creation=True,
                        is_authorized=True,
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )
            elif obs.entry_type == "symlink":
                if obs.link_points_to_canonical:
                    operations.append(
                        LinkOperation(
                            action="NOOP",
                            rule_id="INV-MEM-10",
                            target_path=target_path,
                            canonical_path=canonical,
                            reason=f"Symbolic link already points to {canonical}",
                            expected_representation="symlink",
                            desired_representation="link",
                            is_authorized=True,
                            target_kind="memory_link",
                            resource_name=res.relative_target.as_posix(),
                        )
                    )
                else:
                    # Check if this is a project notes migration from legacy source
                    is_legacy_notes_migration = False
                    legacy_source = (
                        batch.workspace_root / "memory" / batch.project_name / "notes"
                    )
                    if res.source_kind == "project_notes":
                        obs_legacy = inspect_link_target(
                            target_path,
                            legacy_source,
                            target_kind="memory_link",
                            scope="project",
                        )
                        if obs_legacy.link_points_to_canonical:
                            is_legacy_notes_migration = True

                    if is_legacy_notes_migration:
                        operations.append(
                            LinkOperation(
                                action="UNLINK",
                                rule_id="INV-MEM-06",
                                target_path=target_path,
                                canonical_path=legacy_source,
                                reason=f"Remove legacy project notes symlink {target_path} -> {legacy_source}",
                                expected_representation="symlink",
                                desired_representation="absent",
                                is_authorized=True,
                                target_kind="memory_link",
                                resource_name=res.relative_target.as_posix(),
                            )
                        )
                        operations.append(
                            LinkOperation(
                                action="CREATE",
                                rule_id="INV-MEM-03",
                                target_path=target_path,
                                canonical_path=canonical,
                                reason=f"Migrate to new canonical project notes link {target_path} -> {canonical}",
                                expected_representation="missing",
                                desired_representation="link",
                                requires_parent_creation=True,
                                is_authorized=True,
                                target_kind="memory_link",
                                resource_name=res.relative_target.as_posix(),
                            )
                        )
                    else:
                        dest = (
                            obs.raw_link_target or obs.resolved_link_target or "unknown"
                        )
                        finding = f"Unmanaged project runtime item: {target_path}"
                        operations.append(
                            LinkOperation(
                                action="CONFLICT",
                                rule_id="INV-MEM-07",
                                target_path=target_path,
                                canonical_path=canonical,
                                reason=f"Target preserved: {target_path}. Symlink points to unexpected destination: {dest}",
                                finding=finding,
                                is_authorized=False,
                                expected_representation="symlink",
                                desired_representation="link",
                                target_kind="memory_link",
                                resource_name=res.relative_target.as_posix(),
                            )
                        )
            else:
                # Regular file or directory (INV-MEM-07)
                finding = f"Unmanaged project runtime item: {target_path}"
                operations.append(
                    LinkOperation(
                        action="CONFLICT",
                        rule_id="INV-MEM-07",
                        target_path=target_path,
                        canonical_path=canonical,
                        reason=f"Target preserved: {target_path}. Entry is a regular {obs.entry_type} (expected symlink)",
                        finding=finding,
                        is_authorized=False,
                        expected_representation=obs.entry_type,
                        desired_representation="link",
                        target_kind="memory_link",
                        resource_name=res.relative_target.as_posix(),
                    )
                )

        # 3. Deselected / Stale runtime entry cleanup (INV-MEM-05, INV-MEM-06)
        if runtime_memory_dir.is_dir():
            desired_top_names = {
                res.relative_target.parts[0]
                for res in batch.resources
                if res.relative_target.parts
            }
            try:
                for entry in sorted(runtime_memory_dir.iterdir()):
                    entry_name = entry.name
                    if entry_name in desired_top_names:
                        continue

                    # Stale entry detected. Evaluate exact ownership candidates (INV-MEM-05, INV-MEM-06).
                    proj_mem_root = (
                        batch.workspace_root
                        / "projects"
                        / batch.project_name
                        / "memory"
                    )
                    if not proj_mem_root.exists():
                        proj_mem_root = (
                            batch.workspace_root / "memory" / batch.project_name
                        )

                    candidates: list[Path] = [proj_mem_root / entry_name]
                    if entry_name != "notes" or "notes" in batch.selected_references:
                        candidates.append(batch.workspace_root / "memory" / entry_name)

                    obs_stale = inspect_link_target(
                        entry, None, target_kind="memory_link", scope="project"
                    )

                    if obs_stale.entry_type == "symlink":
                        is_owned = False
                        matched_candidate = None
                        for cand in candidates:
                            obs_check = inspect_link_target(
                                entry, cand, target_kind="memory_link", scope="project"
                            )
                            if obs_check.link_points_to_canonical:
                                is_owned = True
                                matched_candidate = cand
                                break

                        if is_owned and matched_candidate is not None:
                            operations.append(
                                LinkOperation(
                                    action="UNLINK",
                                    rule_id="INV-MEM-06",
                                    target_path=entry,
                                    canonical_path=matched_candidate,
                                    reason=f"Remove stale managed memory symlink: {entry} -> {matched_candidate}",
                                    expected_representation="symlink",
                                    desired_representation="absent",
                                    is_authorized=True,
                                    target_kind="memory_link",
                                    resource_name=entry_name,
                                )
                            )
                        else:
                            dest = (
                                obs_stale.raw_link_target
                                or obs_stale.resolved_link_target
                                or "unknown"
                            )
                            finding = f"Unmanaged project runtime item: {entry}"
                            operations.append(
                                LinkOperation(
                                    action="CONFLICT",
                                    rule_id="INV-MEM-07",
                                    target_path=entry,
                                    canonical_path=None,
                                    reason=f"Target preserved: {entry}. Deselected link points to {dest}",
                                    finding=finding,
                                    is_authorized=False,
                                    expected_representation="symlink",
                                    desired_representation="absent",
                                    target_kind="memory_link",
                                    resource_name=entry_name,
                                )
                            )
                    else:
                        # Regular file or directory
                        finding = f"Unmanaged project runtime item: {entry}"
                        operations.append(
                            LinkOperation(
                                action="CONFLICT",
                                rule_id="INV-MEM-07",
                                target_path=entry,
                                canonical_path=None,
                                reason=f"Target preserved: {entry}. Unmanaged {obs_stale.entry_type}",
                                finding=finding,
                                is_authorized=False,
                                expected_representation=obs_stale.entry_type,
                                desired_representation="absent",
                                target_kind="memory_link",
                                resource_name=entry_name,
                            )
                        )
            except OSError:
                pass

    return MemoryPlan(batch=batch, operations=tuple(operations))


def execute_memory_plan(
    plan: MemoryPlan,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> MemoryExecutionResult:
    """Execute a validated MemoryPlan through the shared link execution primitive."""
    if plan.has_conflicts:
        findings_summary = "; ".join(plan.findings)
        return MemoryExecutionResult(
            operations=plan.operations,
            success=False,
            conflict_count=len(plan.conflicts),
            error_message=f"Memory plan has conflicts: {findings_summary}",
        )

    # 1. Plan-level preflight validation before ANY destructive operation (INV-MEM-09)
    # Check A1: agent.toml memory configuration mutation check (INV-MEM-09)
    agent_toml = (
        plan.batch.workspace_root / "projects" / plan.batch.project_name / "agent.toml"
    )
    curr_agent_toml_exists = agent_toml.is_file()
    if curr_agent_toml_exists != plan.batch.planned_agent_toml_exists:
        return MemoryExecutionResult(
            operations=plan.operations,
            success=False,
            applied_count=0,
            conflict_count=1,
            error_message="Stale memory plan: agent.toml file presence changed before apply",
        )
    if curr_agent_toml_exists:
        try:
            curr_cfg = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
            curr_mem = tuple(str(m) for m in curr_cfg.get("memory", []))
            if (
                plan.batch.planned_agent_toml_memory is not None
                and curr_mem != plan.batch.planned_agent_toml_memory
            ):
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=0,
                    conflict_count=1,
                    error_message="Stale memory plan: agent.toml memory configuration changed before apply",
                )
            if curr_mem != plan.batch.selected_references:
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=0,
                    conflict_count=1,
                    error_message="Stale memory plan: agent.toml memory configuration diverged from planned references",
                )
        except Exception as exc:
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=0,
                conflict_count=1,
                error_message=f"Stale memory plan: unable to verify agent.toml: {exc}",
            )

    # Check A2: Project memory source priority & existence check (INV-MEM-09)
    curr_notes_canonical, curr_has_proj_mem = resolve_project_notes_source(
        plan.batch.workspace_root, plan.batch.project_name
    )
    notes_kind = "project_notes" if curr_has_proj_mem else "legacy_notes"
    is_safe_notes, notes_err = validate_memory_canonical_source(
        curr_notes_canonical,
        plan.batch.workspace_root,
        notes_kind,
        project_name=plan.batch.project_name,
    )
    if not is_safe_notes:
        return MemoryExecutionResult(
            operations=plan.operations,
            success=False,
            applied_count=0,
            conflict_count=1,
            error_message=f"Stale memory plan: project memory source safety violation: {notes_err}",
        )
    curr_notes_is_dir = (
        curr_notes_canonical.is_dir() if curr_notes_canonical.exists() else False
    )
    if (
        (
            plan.batch.planned_notes_canonical is not None
            and curr_notes_canonical != plan.batch.planned_notes_canonical
        )
        or (curr_has_proj_mem != plan.batch.planned_has_proj_mem)
        or (curr_notes_is_dir != plan.batch.planned_notes_is_dir)
    ):
        return MemoryExecutionResult(
            operations=plan.operations,
            success=False,
            applied_count=0,
            conflict_count=1,
            error_message="Stale memory plan: project memory source priority changed before apply",
        )

    # Check B: Source identity, existence, type & safety for all resources / CREATE operations
    for res in plan.batch.resources:
        if not res.is_safe:
            continue
        canonical = res.canonical_source
        if not canonical.exists():
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=0,
                conflict_count=1,
                error_message=f"Stale memory plan: canonical source missing: {canonical}",
            )

        try:
            st = canonical.lstat()
            curr_mode_type = stat.S_IFMT(st.st_mode)
        except OSError as exc:
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=0,
                conflict_count=1,
                error_message=f"Stale memory plan: failed to stat canonical source: {canonical}: {exc}",
            )

        if (
            res.expected_mode_type is not None
            and curr_mode_type != res.expected_mode_type
        ):
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=0,
                conflict_count=1,
                error_message=(
                    f"Stale memory plan: canonical source type changed: {canonical} "
                    f"(expected mode type {res.expected_mode_type}, got {curr_mode_type})"
                ),
            )

        is_safe, err = validate_memory_canonical_source(
            canonical,
            plan.batch.workspace_root,
            res.source_kind,
            project_name=plan.batch.project_name,
        )
        if not is_safe:
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=0,
                conflict_count=1,
                error_message=f"Stale memory plan: canonical source safety violation: {err}",
            )

    # Check C: Full target boundary re-validation across all checkouts and operations
    for checkout in plan.batch.active_checkouts:
        runtime_memory_dir = checkout / ".agents" / "memory"
        for res in plan.batch.resources:
            if not res.is_safe:
                continue
            is_valid, _, err = validate_checkout_target_boundary(
                checkout, res.relative_target
            )
            if not is_valid:
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=0,
                    conflict_count=1,
                    error_message=f"Stale memory plan: target boundary violation before apply: {err}",
                )
        for op in plan.operations:
            try:
                rel = op.target_path.relative_to(runtime_memory_dir)
                is_valid, _, err = validate_checkout_target_boundary(checkout, rel)
                if not is_valid:
                    return MemoryExecutionResult(
                        operations=plan.operations,
                        success=False,
                        applied_count=0,
                        conflict_count=1,
                        error_message=f"Stale memory plan: target boundary violation before apply: {err}",
                    )
            except ValueError:
                pass

    # Check D: Preflight target states, ownership evidence, and detect replacement pairs
    target_unlinks = {
        op.target_path: op for op in plan.operations if op.action == "UNLINK"
    }
    target_creates = {
        op.target_path: op for op in plan.operations if op.action == "CREATE"
    }
    replacement_targets = set(target_unlinks.keys()) & set(target_creates.keys())

    for op in plan.operations:
        if op.action == "UNLINK":
            if not op.target_path.is_symlink():
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=0,
                    conflict_count=1,
                    error_message=f"Stale memory plan: target is not a symlink for unlink: {op.target_path}",
                )
            # Ownership evidence preflight: symlink must point to expected canonical
            obs = inspect_link_target(
                op.target_path,
                op.canonical_path,
                canonical_valid=True,
                target_kind="memory_link",
                scope="project",
            )
            if not obs.link_points_to_canonical:
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=0,
                    conflict_count=1,
                    error_message=(
                        f"Stale memory plan: unlink target ownership evidence broken; "
                        f"symlink {op.target_path} does not point to expected canonical {op.canonical_path}"
                    ),
                )
        elif op.action == "CREATE":
            if op.target_path not in replacement_targets:
                if op.target_path.is_symlink() or op.target_path.exists():
                    return MemoryExecutionResult(
                        operations=plan.operations,
                        success=False,
                        applied_count=0,
                        conflict_count=1,
                        error_message=f"Stale memory plan: target already exists: {op.target_path}",
                    )

    # 2. Execution phase
    applied = 0
    skipped = 0
    noop = 0
    conflicts = 0

    handled_replacements: set[Path] = set()

    for op in plan.operations:
        if op.action == "SKIP":
            skipped += 1
            continue

        if op.target_path in replacement_targets:
            if op.target_path in handled_replacements:
                continue
            handled_replacements.add(op.target_path)
            create_op = target_creates[op.target_path]
            unlink_op = target_unlinks[op.target_path]

            if dry_run:
                print(
                    f"[DRY RUN CLEANUP] Would remove legacy project notes symlink: {unlink_op.target_path}"
                )
                print(
                    f"[DRY RUN LINK] {create_op.target_path} -> {create_op.canonical_path}"
                )
                continue

            # Re-verify ownership evidence immediately before replacement
            obs = inspect_link_target(
                unlink_op.target_path,
                unlink_op.canonical_path,
                canonical_valid=True,
                target_kind="memory_link",
                scope="project",
            )
            if not obs.link_points_to_canonical:
                return MemoryExecutionResult(
                    operations=plan.operations,
                    success=False,
                    applied_count=applied,
                    skipped_count=skipped,
                    noop_count=noop,
                    conflict_count=1,
                    error_message=(
                        f"Failed to migrate symlink: ownership evidence broken on {unlink_op.target_path}"
                    ),
                )

            # Atomic / safe replacement: try atomic replace with temp symlink in same directory
            replaced = False
            temp_link = op.target_path.parent / f".tmp_replace_{uuid.uuid4().hex}"
            try:
                if create_op.canonical_path is not None and safe_symlink(
                    create_op.canonical_path, temp_link
                ):
                    os.replace(temp_link, op.target_path)
                    replaced = True
                    applied += 1
                    print(
                        f"[CLEANUP] Removed legacy project notes symlink: {unlink_op.target_path}"
                    )
                    print(
                        f"[LINK] {create_op.target_path} -> {create_op.canonical_path}"
                    )
            except OSError:
                if temp_link.is_symlink() or temp_link.exists():
                    temp_link.unlink(missing_ok=True)

            if not replaced:
                # Fallback: safe unlink and symlink with rollback
                old_dest = (
                    os.readlink(unlink_op.target_path)
                    if unlink_op.target_path.is_symlink()
                    else None
                )
                unlink_op.target_path.unlink()
                if create_op.canonical_path is None or not safe_symlink(
                    create_op.canonical_path, create_op.target_path
                ):
                    if old_dest:
                        Path(unlink_op.target_path).symlink_to(old_dest)
                    return MemoryExecutionResult(
                        operations=plan.operations,
                        success=False,
                        applied_count=applied,
                        skipped_count=skipped,
                        noop_count=noop,
                        conflict_count=1,
                        error_message=f"Failed to migrate symlink: {op.target_path}",
                    )
                applied += 1
                print(
                    f"[CLEANUP] Removed legacy project notes symlink: {unlink_op.target_path}"
                )
                print(f"[LINK] {create_op.target_path} -> {create_op.canonical_path}")
            continue

        res = apply_link_operation(op, dry_run=dry_run, verbose=verbose)
        if not res.success:
            return MemoryExecutionResult(
                operations=plan.operations,
                success=False,
                applied_count=applied,
                skipped_count=skipped,
                noop_count=noop,
                conflict_count=conflicts + 1,
                error_message=res.error_message
                or f"Failed to apply memory operation on {op.target_path}",
            )

        if res.applied:
            applied += 1
        elif op.action == "NOOP":
            noop += 1

    return MemoryExecutionResult(
        operations=plan.operations,
        success=True,
        applied_count=applied,
        skipped_count=skipped,
        noop_count=noop,
        conflict_count=0,
    )
