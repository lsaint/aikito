"""Instruction batch model, Target resolution, pure planning, and execution."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentRegistry, Target, check_target_availability, resolve_targets
from .compat import is_same_target_location
from .link import (
    LinkOperation,
    apply_link_operation,
    inspect_link_target,
    plan_link_target,
)


@dataclass(frozen=True)
class InstructionBatch:
    """Structure representing an instruction synchronization request.

    scope: "global" or "project"
    canonical_source: Path to canonical instruction file (e.g. global/AGENTS.md or projects/<name>/AGENTS.md)
    targets: Deduplicated physical instruction targets declared by agent platforms
    enabled: True if instructions are enabled (for project, canonical is non-empty)
    stale_targets: Legacy instruction targets to clean up (e.g. ~/.grok/AGENTS.md or checkout/.agents/AGENTS.md)
    checkout: Optional physical checkout path (for project scope)
    project_name: Optional project name (for project scope)
    """

    scope: str
    canonical_source: Path
    targets: tuple[Target, ...] = ()
    enabled: bool = True
    stale_targets: tuple[Target, ...] = ()
    checkout: Path | None = None
    project_name: str | None = None
    offline_checkouts: tuple[Path, ...] = ()

    @property
    def resource_count(self) -> int:
        return 1 if self.canonical_source.exists() else 0

    @property
    def target_count(self) -> int:
        return len(self.targets)

    @property
    def consumer_count(self) -> int:
        return sum(len(t.consumers) for t in self.targets)


@dataclass(frozen=True)
class InstructionPlan:
    """Immutable, fully-evaluated synchronization plan for instructions."""

    batch: InstructionBatch
    operations: tuple[LinkOperation, ...]

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
    def same_object_count(self) -> int:
        return sum(1 for op in self.operations if op.action == "SHARED_PATH")

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
class InstructionExecutionResult:
    """Result of executing an InstructionPlan."""

    operations: tuple[LinkOperation, ...]
    success: bool
    applied_count: int = 0
    skipped_count: int = 0
    conflict_count: int = 0
    shared_path_count: int = 0
    noop_count: int = 0
    error_message: str | None = None


def build_global_instruction_batch(
    aikito_dir: Path,
    home: Path,
    *,
    registry: AgentRegistry | None = None,
) -> InstructionBatch:
    """Build an InstructionBatch for global instructions."""
    if registry is None:
        registry = AgentRegistry.load(aikito_dir, home)
    canonical = aikito_dir / "global" / "AGENTS.md"
    targets = resolve_targets(
        "global_instructions", aikito_dir, home, registry=registry
    )

    stale: list[Target] = []
    # Legacy grok path (~/.grok/AGENTS.md)
    if "grok" in registry:
        legacy_grok = home / ".grok" / "AGENTS.md"
        is_formal = any(is_same_target_location(t.path, legacy_grok) for t in targets)
        if not is_formal:
            if legacy_grok.is_symlink() or legacy_grok.exists():
                stale.append(
                    Target(
                        kind="instruction_link",
                        scope="global",
                        path=legacy_grok,
                        canonical_source=canonical,
                        consumers=("grok",),
                        consumer_display_names=("Grok (legacy)",),
                    )
                )

    return InstructionBatch(
        scope="global",
        canonical_source=canonical,
        targets=targets,
        enabled=True,
        stale_targets=tuple(stale),
    )


def build_project_instruction_batch(
    workspace_root: Path,
    project_name: str,
    checkout: Path | Sequence[Path],
    home: Path,
    *,
    registry: AgentRegistry | None = None,
    is_offline: bool = False,
    offline_checkouts: Sequence[Path] = (),
) -> InstructionBatch:
    """Build an InstructionBatch for project checkout(s)."""
    if registry is None:
        registry = AgentRegistry.load(workspace_root, home)
    canonical = workspace_root / "projects" / project_name / "AGENTS.md"

    active_cos: tuple[Path, ...]
    if isinstance(checkout, Path):
        active_cos = (checkout,)
    else:
        active_cos = tuple(checkout)

    offline_cos = tuple(offline_checkouts)
    all_targets: list[Target] = []
    stale: list[Target] = []

    def _add_target(t: Target) -> None:
        for i, existing in enumerate(all_targets):
            if is_same_target_location(t.path, existing.path):
                merged_consumers = tuple(
                    dict.fromkeys(existing.consumers + t.consumers)
                )
                merged_display = tuple(
                    dict.fromkeys(
                        existing.consumer_display_names + t.consumer_display_names
                    )
                )
                all_targets[i] = Target(
                    kind=existing.kind,
                    scope=existing.scope,
                    path=existing.path,
                    canonical_source=existing.canonical_source,
                    consumers=merged_consumers,
                    consumer_display_names=merged_display,
                )
                return
        all_targets.append(t)

    def _add_stale(st: Target) -> None:
        if not any(
            is_same_target_location(st.path, existing.path) for existing in stale
        ):
            stale.append(st)

    for co in active_cos:
        targets = resolve_targets(
            "project_instructions",
            workspace_root,
            home,
            project_path=co,
            project_name=project_name,
            registry=registry,
        )
        for t in targets:
            _add_target(t)

        if not is_offline:
            legacy_agents = co / ".agents" / "AGENTS.md"
            is_formal = any(
                is_same_target_location(t.path, legacy_agents) for t in all_targets
            )
            if not is_formal:
                if legacy_agents.is_symlink() or legacy_agents.exists():
                    _add_stale(
                        Target(
                            kind="instruction_link",
                            scope="project",
                            path=legacy_agents,
                            canonical_source=canonical,
                            consumers=(),
                            consumer_display_names=("Legacy .agents",),
                        )
                    )

    for off_co in offline_cos:
        off_targets = resolve_targets(
            "project_instructions",
            workspace_root,
            home,
            project_path=off_co,
            project_name=project_name,
            registry=registry,
        )
        for t in off_targets:
            _add_target(t)

    enabled = False
    if canonical.is_file():
        try:
            content = canonical.read_text(encoding="utf-8", errors="replace").strip()
            enabled = bool(content)
        except OSError:
            enabled = False

    first_checkout = (
        active_cos[0] if active_cos else (offline_cos[0] if offline_cos else None)
    )

    return InstructionBatch(
        scope="project",
        canonical_source=canonical,
        targets=tuple(all_targets),
        enabled=enabled,
        stale_targets=tuple(stale),
        checkout=first_checkout,
        project_name=project_name,
        offline_checkouts=offline_cos,
    )


def plan_instructions(
    batch: InstructionBatch,
    home: Path,
    *,
    is_offline: bool = False,
) -> InstructionPlan:
    """Pure planning: evaluate targets and produce deterministic LinkOperations without filesystem writes."""
    operations: list[LinkOperation] = []

    canonical_valid = batch.canonical_source.is_file()
    canonical_error = None
    if batch.scope == "global" and not canonical_valid:
        canonical_error = f"Global instruction file not found: {batch.canonical_source}"

    if is_offline:
        for t in batch.targets:
            operations.append(
                LinkOperation(
                    action="SKIP",
                    rule_id="INV-INST-11",
                    target_path=t.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Checkout is offline: {t.path}",
                    expected_representation="missing",
                    desired_representation="link",
                    target_kind="instruction_link",
                    resource_name="/".join(t.consumer_display_names) or t.path.name,
                    is_authorized=True,
                )
            )
        return InstructionPlan(batch=batch, operations=tuple(operations))

    desired_mode = "link" if batch.enabled else "absent"

    for target in batch.targets:
        target_offline = any(
            target.path == off or target.path.is_relative_to(off)
            for off in batch.offline_checkouts
        )
        if target_offline:
            operations.append(
                LinkOperation(
                    action="SKIP",
                    rule_id="INV-INST-11",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Checkout is offline: {target.path}",
                    expected_representation="missing",
                    desired_representation="link",
                    target_kind="instruction_link",
                    resource_name="/".join(target.consumer_display_names)
                    or target.path.name,
                    is_authorized=True,
                )
            )
            continue

        is_same_obj = target.is_same_object and not target.path.is_symlink()
        observed = inspect_link_target(
            target.path,
            expected_canonical=batch.canonical_source,
            canonical_valid=canonical_valid,
            canonical_error=canonical_error,
            target_kind="instruction_link",
            scope=batch.scope,
            is_same_object=is_same_obj,
        )

        avail = check_target_availability(target, home)
        parent_exists = target.path.parent.exists()
        resource_name = "/".join(target.consumer_display_names) or target.path.name

        op = plan_link_target(
            observed,
            desired_mode=desired_mode,
            availability_status=avail.status,
            parent_exists=parent_exists,
            resource_name=resource_name,
        )

        # Refine operation according to instruction invariants
        if batch.enabled:
            if observed.is_same_object:
                op = LinkOperation(
                    action="SHARED_PATH",
                    rule_id="INV-INST-05",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Target path is the same physical object as canonical instructions; no link required for '{resource_name}'",
                    expected_representation=observed.entry_type,
                    desired_representation="link",
                    is_same_object=True,
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=True,
                )
            elif not canonical_valid:
                op = LinkOperation(
                    action="CONFLICT",
                    rule_id="INV-TR-02",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Canonical instruction file not found: {batch.canonical_source}",
                    finding=f"Missing instruction source: {batch.canonical_source}",
                    expected_representation=observed.entry_type,
                    desired_representation="link",
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=False,
                )
            elif observed.entry_type == "file":
                op = LinkOperation(
                    action="CONFLICT",
                    rule_id="INV-INST-03",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=(
                        f"Target preserved: {target.path}. "
                        f"Target is a regular file; Aikito does not overwrite existing instruction files. "
                        f"Move or remove it manually, then run sync again."
                    ),
                    finding=f"Pre-existing regular instruction file: {target.path}",
                    expected_representation="file",
                    desired_representation="link",
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=False,
                )
            elif observed.entry_type == "symlink":
                if observed.link_points_to_canonical:
                    op = LinkOperation(
                        action="NOOP",
                        rule_id="INV-INST-12",
                        target_path=target.path,
                        canonical_path=batch.canonical_source,
                        reason=f"Symbolic link already points to canonical instructions for '{resource_name}'",
                        expected_representation="symlink",
                        desired_representation="link",
                        target_kind="instruction_link",
                        resource_name=resource_name,
                        is_authorized=True,
                    )
                else:
                    rule = "INV-INST-06" if batch.scope == "global" else "INV-INST-02"
                    dest = (
                        observed.raw_link_target
                        or observed.resolved_link_target
                        or "unknown"
                    )
                    op = LinkOperation(
                        action="CONFLICT",
                        rule_id=rule,
                        target_path=target.path,
                        canonical_path=batch.canonical_source,
                        reason=(
                            f"Target preserved: {target.path}. "
                            f"Symbolic link points to unauthorized destination: {dest} (expected {batch.canonical_source}). "
                            f"Foreign or external instruction symlinks will not be overwritten automatically; "
                            f"inspect manually, then run sync again."
                        ),
                        finding=f"Unauthorized instruction symlink destination: {target.path} -> {dest}",
                        expected_representation="symlink",
                        desired_representation="link",
                        target_kind="instruction_link",
                        resource_name=resource_name,
                        is_authorized=False,
                    )
            elif observed.entry_type == "missing":
                if op.action == "CREATE":
                    op = LinkOperation(
                        action="CREATE",
                        rule_id="INV-INST-07"
                        if batch.scope == "project"
                        else "INV-TR-01",
                        target_path=target.path,
                        canonical_path=batch.canonical_source,
                        reason=f"Create symbolic link: {target.path} -> {batch.canonical_source}",
                        expected_representation="missing",
                        desired_representation="link",
                        requires_parent_creation=op.requires_parent_creation,
                        target_kind="instruction_link",
                        resource_name=resource_name,
                        is_authorized=True,
                    )
        else:
            # batch.enabled is False (empty canonical in project)
            if observed.is_same_object:
                op = LinkOperation(
                    action="NOOP",
                    rule_id="INV-INST-05",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Canonical instructions file itself is not deleted: {target.path}",
                    expected_representation=observed.entry_type,
                    desired_representation="absent",
                    is_same_object=True,
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=True,
                )
            elif observed.entry_type == "symlink":
                if observed.link_points_to_canonical:
                    op = LinkOperation(
                        action="UNLINK",
                        rule_id="INV-INST-08",
                        target_path=target.path,
                        canonical_path=batch.canonical_source,
                        reason=f"Remove unselected instruction symbolic link: {target.path}",
                        expected_representation="symlink",
                        desired_representation="absent",
                        target_kind="instruction_link",
                        resource_name=resource_name,
                        is_authorized=True,
                    )
                else:
                    dest = (
                        observed.raw_link_target
                        or observed.resolved_link_target
                        or "unknown"
                    )
                    op = LinkOperation(
                        action="NOOP",
                        rule_id="INV-INST-08",
                        target_path=target.path,
                        canonical_path=batch.canonical_source,
                        reason=f"Preserve foreign or unmanaged symbolic link: {target.path} -> {dest}",
                        finding=f"Unmanaged instruction symlink: {target.path}",
                        expected_representation="symlink",
                        desired_representation="absent",
                        target_kind="instruction_link",
                        resource_name=resource_name,
                        is_authorized=True,
                    )
            elif observed.entry_type == "file":
                op = LinkOperation(
                    action="NOOP",
                    rule_id="INV-INST-10",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Preserve project-owned instruction file: {target.path}",
                    finding=f"Project-owned instruction file preserved: {target.path}",
                    expected_representation="file",
                    desired_representation="absent",
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=True,
                )
            elif observed.entry_type == "missing":
                op = LinkOperation(
                    action="NOOP",
                    rule_id="INV-INST-12",
                    target_path=target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Instruction link is already absent: {target.path}",
                    expected_representation="missing",
                    desired_representation="absent",
                    target_kind="instruction_link",
                    resource_name=resource_name,
                    is_authorized=True,
                )

        operations.append(op)

    # Process stale targets (legacy grok or legacy .agents/AGENTS.md)
    for stale_target in batch.stale_targets:
        observed = inspect_link_target(
            stale_target.path,
            expected_canonical=batch.canonical_source,
            canonical_valid=canonical_valid,
            target_kind="instruction_link",
            scope=batch.scope,
        )
        res_name = (
            "/".join(stale_target.consumer_display_names) or stale_target.path.name
        )
        if observed.entry_type == "symlink":
            if observed.link_points_to_canonical:
                op = LinkOperation(
                    action="UNLINK",
                    rule_id="INV-INST-09",
                    target_path=stale_target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Remove legacy instruction link: {stale_target.path}",
                    expected_representation="symlink",
                    desired_representation="absent",
                    target_kind="instruction_link",
                    resource_name=res_name,
                    is_authorized=True,
                )
            else:
                op = LinkOperation(
                    action="NOOP",
                    rule_id="INV-INST-09",
                    target_path=stale_target.path,
                    canonical_path=batch.canonical_source,
                    reason=f"Preserve unmanaged legacy item: {stale_target.path}",
                    finding=f"Unmanaged legacy instruction item: {stale_target.path}",
                    expected_representation="symlink",
                    desired_representation="absent",
                    target_kind="instruction_link",
                    resource_name=res_name,
                    is_authorized=True,
                )
        elif observed.entry_type == "missing":
            op = LinkOperation(
                action="NOOP",
                rule_id="INV-INST-09",
                target_path=stale_target.path,
                canonical_path=batch.canonical_source,
                reason=f"Legacy instruction entry is already absent: {stale_target.path}",
                expected_representation="missing",
                desired_representation="absent",
                target_kind="instruction_link",
                resource_name=res_name,
                is_authorized=True,
            )
        else:
            op = LinkOperation(
                action="NOOP",
                rule_id="INV-INST-09",
                target_path=stale_target.path,
                canonical_path=batch.canonical_source,
                reason=f"Preserve unmanaged legacy item of type {observed.entry_type}: {stale_target.path}",
                finding=f"Unmanaged legacy instruction entry: {stale_target.path}",
                expected_representation=observed.entry_type,
                desired_representation="absent",
                target_kind="instruction_link",
                resource_name=res_name,
                is_authorized=True,
            )
        operations.append(op)

    return InstructionPlan(batch=batch, operations=tuple(operations))


def execute_instruction_plan(
    plan: InstructionPlan,
    home: Path,
    *,
    dry_run: bool = False,
    verbose: bool = False,
) -> InstructionExecutionResult:
    """Execute an InstructionPlan with precondition re-checking and idempotent link operations."""
    if not plan.can_apply and not dry_run:
        return InstructionExecutionResult(
            operations=plan.operations,
            success=False,
            conflict_count=len(plan.conflicts),
            error_message="Instruction plan contains unresolved conflicts.",
        )

    # Preflight canonical source state
    if plan.batch.enabled:
        if not plan.batch.canonical_source.is_file():
            return InstructionExecutionResult(
                operations=plan.operations,
                success=False,
                error_message=f"Preflight failed: canonical instruction file not found: {plan.batch.canonical_source} (stale plan)",
            )
        if plan.batch.scope == "project":
            try:
                curr_content = plan.batch.canonical_source.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if not curr_content:
                    return InstructionExecutionResult(
                        operations=plan.operations,
                        success=False,
                        error_message=f"Preflight failed: canonical instruction file changed from non-empty to empty: {plan.batch.canonical_source} (stale plan)",
                    )
            except OSError as exc:
                return InstructionExecutionResult(
                    operations=plan.operations,
                    success=False,
                    error_message=f"Preflight failed: could not read canonical instruction file: {exc}",
                )
    else:
        if plan.batch.canonical_source.is_file():
            try:
                curr_content = plan.batch.canonical_source.read_text(
                    encoding="utf-8", errors="replace"
                ).strip()
                if curr_content:
                    return InstructionExecutionResult(
                        operations=plan.operations,
                        success=False,
                        error_message=f"Preflight failed: canonical instruction file changed from empty to non-empty: {plan.batch.canonical_source} (stale plan)",
                    )
            except OSError as exc:
                return InstructionExecutionResult(
                    operations=plan.operations,
                    success=False,
                    error_message=f"Preflight failed: could not read canonical instruction file: {exc}",
                )

    applied_count = 0
    skipped_count = 0
    conflict_count = 0
    shared_path_count = 0
    noop_count = 0

    executed_ops: list[LinkOperation] = []

    for op in plan.operations:
        res = apply_link_operation(op, dry_run=dry_run, verbose=verbose)
        executed_ops.append(res.operation)
        if not res.success:
            conflict_count += 1
            return InstructionExecutionResult(
                operations=tuple(executed_ops),
                success=False,
                applied_count=applied_count,
                skipped_count=skipped_count,
                conflict_count=conflict_count,
                shared_path_count=shared_path_count,
                noop_count=noop_count,
                error_message=res.error_message
                or f"Failed operation on {op.target_path}",
            )

        if op.action == "SHARED_PATH":
            shared_path_count += 1
        elif op.action == "SKIP":
            skipped_count += 1
        elif op.action == "NOOP":
            noop_count += 1
        elif res.applied:
            applied_count += 1

    return InstructionExecutionResult(
        operations=tuple(executed_ops),
        success=True,
        applied_count=applied_count,
        skipped_count=skipped_count,
        conflict_count=conflict_count,
        shared_path_count=shared_path_count,
        noop_count=noop_count,
    )
