"""Project memory runtime visibility models, planning, and execution.

Provides unified Link-only planning and execution for project memory runtime visibility,
encompassing workspace memory references and project notes canonical sources.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


@dataclass(frozen=True)
class MemoryBatch:
    """Structure representing a project memory synchronization request."""

    project_name: str
    workspace_root: Path
    active_checkouts: tuple[Path, ...]
    offline_checkouts: tuple[Path, ...] = ()
    resources: tuple[MemoryResource, ...] = ()
    selected_references: tuple[str, ...] = ()

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
    if proj_mem.exists():
        return proj_mem / "notes", True
    return workspace_root / "memory" / project_name / "notes", False


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

    # 1. Workspace memory references
    ws_mem_root = workspace_root / "memory"
    for ref in memory_refs:
        rel = Path(ref)
        canonical = ws_mem_root / rel
        resources.append(
            MemoryResource(
                identity=f"workspace_ref:{ref}",
                canonical_source=canonical,
                relative_target=rel,
                source_kind="workspace_ref",
                exists=canonical.exists(),
                is_dir=canonical.is_dir(),
            )
        )

    # 2. Project notes special-case
    notes_canonical, has_proj_mem = resolve_project_notes_source(
        workspace_root, project_name
    )
    if notes_canonical.is_dir():
        resources.append(
            MemoryResource(
                identity="project_notes",
                canonical_source=notes_canonical,
                relative_target=Path("notes"),
                source_kind="project_notes",
                exists=True,
                is_dir=True,
            )
        )

    return MemoryBatch(
        project_name=project_name,
        workspace_root=workspace_root,
        active_checkouts=tuple(active_checkouts),
        offline_checkouts=tuple(offline_checkouts),
        resources=tuple(resources),
        selected_references=tuple(memory_refs),
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

    # 2. Active checkout planning
    for checkout in batch.active_checkouts:
        runtime_memory_dir = checkout / ".agents" / "memory"

        # Plan desired resources
        for res in batch.resources:
            target_path = runtime_memory_dir / res.relative_target
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

                    # Stale entry detected. Evaluate exact ownership candidates.
                    candidates: list[Path] = []
                    if entry_name == "notes":
                        candidates.append(
                            batch.workspace_root
                            / "projects"
                            / batch.project_name
                            / "memory"
                            / "notes"
                        )
                        candidates.append(
                            batch.workspace_root
                            / "memory"
                            / batch.project_name
                            / "notes"
                        )
                        if "notes" in batch.selected_references:
                            candidates.append(batch.workspace_root / "memory" / "notes")
                    else:
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

    applied = 0
    skipped = 0
    noop = 0
    conflicts = 0

    for op in plan.operations:
        if op.action == "SKIP":
            skipped += 1
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
