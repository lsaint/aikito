"""Workspace-level synchronization coordinator, global planning, and bundled skill lifecycle."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from .agents import AgentRegistry, AgentRegistryError, load_agent_definitions
from .bundled_skills import (
    BundledSkillRefreshError,
    _backup_target,
    _replace_directory,
    directory_digest,
)
from .conflict import collect_resource_conflicts
from .diagnostics import (
    Finding,
    is_conflict_finding,
    is_error_finding,
    is_warning_finding,
)
from .global_skills import (
    GlobalSkillExecutionResult,
    build_global_skill_batch,
    execute_global_skills,
    plan_global_skills,
)
from .instructions import (
    InstructionExecutionResult,
    build_global_instruction_batch,
    execute_instruction_plan,
    plan_instructions,
)
from .mcp import (
    MCPConfigError,
    MCPExecutionResult,
    MCPPlan,
    build_mcp_plan,
    execute_mcp_plan,
)
from .plan_observation import (
    OperationEffect,
    PlanObservation,
    PlanOperationView,
    combine_observations,
    safe_observe_plan,
)
from .project import resolve_project_binding
from .project_sync import (
    ProjectSyncBatch,
    ProjectSyncExecutionResult,
    apply_project_sync_batch,
    build_project_sync_batch,
)
from .skill_state import SkillWriterLock
from .subagent import (
    SubagentConfigError,
    SubagentExecutionResult,
    SubagentPlan,
    build_subagent_plan,
    execute_subagent_plan,
)
from .templating import BUNDLED_SKILL_NAMES, bundled_skill_path


@dataclass(frozen=True)
class BundledSkillRefreshOperation:
    skill_name: str
    action: str  # "NOOP" or "REFRESH"
    package_source_fingerprint: str | None = None
    workspace_target_fingerprint: str | None = None
    reason: str = ""


@dataclass(frozen=True)
class BundledSkillRefreshPlan:
    operations: tuple[BundledSkillRefreshOperation, ...] = ()
    refreshed_names: tuple[str, ...] = ()
    can_apply: bool = True
    replan_required: bool = False
    error_message: str | None = None

    def observe(self) -> PlanObservation:
        """Project plan into a pure PlanObservation."""
        views: list[PlanOperationView] = []
        findings: list[Finding] = []
        for op in self.operations:
            if op.action == "REFRESH":
                effect = OperationEffect.UPDATE
            elif op.action == "NOOP":
                effect = OperationEffect.NOOP
            else:
                effect = OperationEffect.NONE
                findings.append(
                    Finding(
                        status="ERROR",
                        code="UNKNOWN_PLAN_ACTION",
                        message=f"Unhandled bundled refresh action: {op.action}",
                        resource=op.skill_name,
                    )
                )
            views.append(
                PlanOperationView(
                    resource_type="bundled_skill",
                    resource_name=op.skill_name,
                    effect=effect,
                    scope="global",
                    target=op.skill_name,
                    reason=op.reason,
                    domain_action=op.action,
                    authorized=True,
                )
            )
        if self.error_message:
            findings.append(
                Finding(
                    status="ERROR",
                    code="BUNDLED_REFRESH_ERROR",
                    message=self.error_message,
                    resource="bundled_skills",
                )
            )
        can_apply = self.can_apply and not any(is_error_finding(f) for f in findings)
        return PlanObservation(
            operations=tuple(views),
            findings=tuple(findings),
            can_apply=can_apply,
        )


@dataclass(frozen=True)
class GlobalSyncPlan:
    bundled_refresh_plan: BundledSkillRefreshPlan
    skill_plan: Any | None
    instruction_plan: Any | None
    findings: tuple[Finding, ...] = ()
    can_apply: bool = True
    replan_required_after_apply: bool = False
    error_message: str | None = None
    # None treats findings on directly constructed plans as owned.
    owned_findings: tuple[Finding, ...] | None = None

    def observe(self) -> PlanObservation:
        """Project global sync plan and child plans into a unified PlanObservation."""
        children: list[PlanObservation] = []
        b_obs = safe_observe_plan(self.bundled_refresh_plan)
        if b_obs is not None:
            children.append(b_obs)
        s_obs = safe_observe_plan(self.skill_plan)
        if s_obs is not None:
            children.append(s_obs)
        i_obs = safe_observe_plan(self.instruction_plan)
        if i_obs is not None:
            children.append(i_obs)

        global_findings = (
            self.owned_findings if self.owned_findings is not None else self.findings
        )

        return combine_observations(
            children,
            additional_findings=global_findings,
            can_apply=self.can_apply,
        )


@dataclass(frozen=True)
class GlobalSyncExecutionResult:
    success: bool

    def __bool__(self) -> bool:
        return self.success

    skill_result: Any | None = None
    instruction_result: Any | None = None
    refreshed_bundled: tuple[str, ...] = ()
    findings: tuple[Finding, ...] = ()
    replan_required: bool = False
    instruction_success: bool = True
    error_message: str | None = None


GlobalSyncResult = GlobalSyncExecutionResult


def build_bundled_refresh_plan(
    workspace_root: Path,
    home: Path,
    *,
    outdated_bundled_skills_fn: Optional[Callable[[Path], Sequence[str]]] = None,
) -> BundledSkillRefreshPlan:
    skills_root = workspace_root / "skills"
    outdated_set = (
        set(outdated_bundled_skills_fn(workspace_root))
        if outdated_bundled_skills_fn
        else None
    )

    operations: list[BundledSkillRefreshOperation] = []
    outdated: list[str] = []

    names = list(BUNDLED_SKILL_NAMES)
    if outdated_set:
        for extra_name in outdated_set:
            if extra_name not in names:
                names.append(extra_name)

    for name in names:
        source = bundled_skill_path(name)
        target = skills_root / name
        source_fp = directory_digest(source) if source.is_dir() else None
        target_fp = directory_digest(target) if target.exists() else None

        if outdated_set is not None:
            diverged = name in outdated_set
        else:
            diverged = source_fp != target_fp

        action = "REFRESH" if diverged else "NOOP"
        if diverged:
            outdated.append(name)

        operations.append(
            BundledSkillRefreshOperation(
                skill_name=name,
                action=action,
                package_source_fingerprint=source_fp,
                workspace_target_fingerprint=target_fp,
                reason=(
                    "Bundled skill diverged from package"
                    if diverged
                    else "Bundled skill matches package"
                ),
            )
        )
    return BundledSkillRefreshPlan(
        operations=tuple(operations),
        refreshed_names=tuple(outdated),
        can_apply=True,
        replan_required=len(outdated) > 0,
    )


def execute_bundled_refresh_plan(
    plan: BundledSkillRefreshPlan,
    workspace_root: Path,
    home: Path,
    *,
    dry_run: bool = False,
) -> tuple[str, ...]:
    b_obs = plan.observe()
    refreshed_names = {
        view.resource_name
        for view in b_obs.operations
        if view.effect != OperationEffect.NOOP
    }
    refresh_ops = [op for op in plan.operations if op.skill_name in refreshed_names]
    if not refresh_ops:
        return ()

    if dry_run:
        for op in refresh_ops:
            print(f"[DRY-RUN] Would refresh bundled skill: {op.skill_name}")
        return tuple(op.skill_name for op in refresh_ops)

    skills_root = workspace_root / "skills"
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_root = home / ".aikito" / "backups" / f"bundled-skills_{timestamp}"

    with SkillWriterLock(home):
        for op in refresh_ops:
            target = skills_root / op.skill_name
            source = bundled_skill_path(op.skill_name)
            curr_source_fp = directory_digest(source) if source.is_dir() else None
            curr_target_fp = directory_digest(target) if target.exists() else None

            if (
                curr_source_fp != op.package_source_fingerprint
                or curr_target_fp != op.workspace_target_fingerprint
            ):
                raise BundledSkillRefreshError(
                    f"Bundled skill '{op.skill_name}' state diverged from plan snapshot; re-plan required"
                )

        refreshed: list[str] = []
        try:
            for op in refresh_ops:
                target = skills_root / op.skill_name
                source = bundled_skill_path(op.skill_name)
                if target.exists() or target.is_symlink():
                    backup = backup_root / op.skill_name
                    _backup_target(target, backup)
                    print(f"[BACKUP] Bundled skill '{op.skill_name}': {backup}")
                _replace_directory(source, target)
                print(
                    f"[REFRESH] Bundled skill '{op.skill_name}' updated from installed Aikito"
                )
                refreshed.append(op.skill_name)
        except OSError as exc:
            raise BundledSkillRefreshError(
                f"Failed to refresh bundled skill '{op.skill_name}': {exc}"
            ) from exc

        return tuple(refreshed)


def build_global_sync_plan(
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
    container_path: Path | None = None,
    skills: Sequence[str] | None = None,
    outdated_bundled_skills_fn: Optional[Callable[[Path], Sequence[str]]] = None,
    load_agent_definitions_fn: Optional[Callable[[Path, Path], Any]] = None,
) -> GlobalSyncPlan:
    """Construct a read-only plan for global skills and instructions."""
    skills_toml_path = aikito_dir / "skills.toml"
    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    findings: list[Finding] = []
    owned_findings: list[Finding] = []

    if container_path is None:
        agents_env = os.environ.get("AIKITO_AGENTS_DIR")
        container_path = (
            Path(agents_env) / "skills" if agents_env else home / ".agents" / "skills"
        )

    if not skills_toml_path.exists():
        finding = Finding(
            status="ERROR",
            code="GLOBAL_SKILLS_MISSING",
            message="Global skills configuration not found.",
            resource=str(skills_toml_path),
        )
        return GlobalSyncPlan(
            bundled_refresh_plan=BundledSkillRefreshPlan(),
            skill_plan=None,
            instruction_plan=None,
            findings=(finding,),
            can_apply=False,
            error_message="Global skills configuration not found.",
        )

    toml_conflicts = collect_resource_conflicts([skills_toml_path], home)
    if toml_conflicts:
        findings.extend(
            Finding(
                status="ERROR",
                code="CONFLICT_MARKER",
                message=err,
                resource=str(skills_toml_path),
            )
            for err in toml_conflicts
        )
        return GlobalSyncPlan(
            bundled_refresh_plan=BundledSkillRefreshPlan(),
            skill_plan=None,
            instruction_plan=None,
            findings=tuple(findings),
            can_apply=False,
            error_message="Conflict markers detected in skills.toml.",
        )

    try:
        with open(skills_toml_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError) as exc:
        finding = Finding(
            status="ERROR",
            code="TOML_DECODE_ERROR",
            message=f"Failed to read global skills configuration: {exc}",
            resource=str(skills_toml_path),
        )
        return GlobalSyncPlan(
            bundled_refresh_plan=BundledSkillRefreshPlan(),
            skill_plan=None,
            instruction_plan=None,
            findings=(finding,),
            can_apply=False,
            error_message=str(exc),
        )

    if skills is None:
        skills = data.get("skills", [])
        if not isinstance(skills, list):
            finding = Finding(
                status="ERROR",
                code="INVALID_CONFIG",
                message="Global skills configuration is malformed (expected a list of skill names).",
                resource=str(skills_toml_path),
            )
            return GlobalSyncPlan(
                bundled_refresh_plan=BundledSkillRefreshPlan(),
                skill_plan=None,
                instruction_plan=None,
                findings=(finding,),
                can_apply=False,
                error_message="Global skills configuration is malformed.",
            )

    bundled_plan = build_bundled_refresh_plan(
        aikito_dir, home, outdated_bundled_skills_fn=outdated_bundled_skills_fn
    )
    outdated_bundled = set(bundled_plan.refreshed_names)

    global_resources: list[Path] = []
    if global_instruction_source.is_file():
        global_resources.append(global_instruction_source)
    for skill_name in skills:
        s_dir = aikito_dir / "skills" / str(skill_name)
        if s_dir.is_dir() and str(skill_name) not in outdated_bundled:
            global_resources.append(s_dir)

    global_conflicts = collect_resource_conflicts(global_resources, home)
    if global_conflicts:
        findings.extend(
            Finding(
                status="ERROR",
                code="CONFLICT_MARKER",
                message=err,
            )
            for err in global_conflicts
        )
        return GlobalSyncPlan(
            bundled_refresh_plan=bundled_plan,
            skill_plan=None,
            instruction_plan=None,
            findings=tuple(findings),
            can_apply=False,
            error_message="Conflict markers detected in global resources.",
        )

    try:
        agent_loader = load_agent_definitions_fn or load_agent_definitions
        agents = agent_loader(aikito_dir, home)
    except AgentRegistryError as exc:
        finding = Finding(
            status="ERROR",
            code="MCP_CONFIG_ERROR",
            message=str(exc),
        )
        return GlobalSyncPlan(
            bundled_refresh_plan=bundled_plan,
            skill_plan=None,
            instruction_plan=None,
            findings=(finding,),
            can_apply=False,
            error_message=str(exc),
        )

    registry = AgentRegistry(agents)
    batch = build_global_skill_batch(
        aikito_dir,
        home,
        skills=skills,
        registry=registry,
        container_path=container_path,
    )
    skill_plan = plan_global_skills(
        batch, home, dry_run=dry_run, refreshed_bundled=outdated_bundled
    )

    instruction_batch = build_global_instruction_batch(
        aikito_dir, home, registry=registry
    )
    instruction_plan = plan_instructions(instruction_batch, home)

    can_apply = True
    skill_obs = skill_plan.observe()
    if skill_obs.summary.blocked or not skill_obs.can_apply:
        can_apply = False
    findings.extend(skill_plan.blocking_findings())

    if not global_instruction_source.is_file():
        can_apply = False
        missing_instruction = Finding(
            status="ERROR",
            code="GLOBAL_INSTRUCTION_MISSING",
            message=f"Global instruction file not found: {global_instruction_source}",
            resource=str(global_instruction_source),
        )
        findings.append(missing_instruction)
        owned_findings.append(missing_instruction)

    instruction_obs = instruction_plan.observe()
    if instruction_obs.summary.blocked or not instruction_obs.can_apply:
        can_apply = False
    findings.extend(instruction_plan.blocking_findings())

    return GlobalSyncPlan(
        bundled_refresh_plan=bundled_plan,
        skill_plan=skill_plan,
        instruction_plan=instruction_plan,
        findings=tuple(findings),
        owned_findings=tuple(owned_findings),
        can_apply=can_apply,
        replan_required_after_apply=bundled_plan.replan_required,
        error_message="Conflicts detected in global plan." if not can_apply else None,
    )


def execute_global_sync_plan(
    plan: GlobalSyncPlan,
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
    execute_global_skills_fn: Optional[
        Callable[..., GlobalSkillExecutionResult]
    ] = None,
    execute_instruction_plan_fn: Optional[
        Callable[..., InstructionExecutionResult]
    ] = None,
) -> GlobalSyncExecutionResult:
    """Execute global skill, instruction, and bundled refresh plans under proper lock boundaries."""
    skill_obs = safe_observe_plan(plan.skill_plan)
    has_skill_conflicts = skill_obs is not None and (
        skill_obs.summary.blocked or not skill_obs.can_apply
    )
    if plan.skill_plan is None or has_skill_conflicts:
        return GlobalSyncExecutionResult(
            success=False,
            findings=plan.findings,
            replan_required=False,
            error_message=plan.error_message or "Global sync plan cannot be applied.",
        )

    refreshed: tuple[str, ...] = ()
    skill_res: Optional[GlobalSkillExecutionResult] = None
    exec_skills = execute_global_skills_fn or execute_global_skills

    if not dry_run:
        with SkillWriterLock(home):
            try:
                refreshed = execute_bundled_refresh_plan(
                    plan.bundled_refresh_plan,
                    aikito_dir,
                    home,
                    dry_run=False,
                )
            except BundledSkillRefreshError as exc:
                return GlobalSyncExecutionResult(
                    success=False,
                    findings=plan.findings,
                    replan_required=False,
                    error_message=str(exc),
                )

            for skill_name in refreshed:
                skill_dir = aikito_dir / "skills" / skill_name
                if not skill_dir.is_dir():
                    return GlobalSyncExecutionResult(
                        success=False,
                        refreshed_bundled=refreshed,
                        replan_required=False,
                        error_message=f"Canonical skill '{skill_name}' not found after refresh.",
                    )

            skill_res = exec_skills(
                plan.skill_plan,
                dry_run=False,
                refreshed_bundled=refreshed,
            )
    else:
        try:
            refreshed = execute_bundled_refresh_plan(
                plan.bundled_refresh_plan,
                aikito_dir,
                home,
                dry_run=True,
            )
        except BundledSkillRefreshError as exc:
            return GlobalSyncExecutionResult(
                success=False,
                findings=plan.findings,
                replan_required=False,
                error_message=str(exc),
            )

        skill_res = exec_skills(
            plan.skill_plan,
            dry_run=True,
            refreshed_bundled=refreshed,
        )

    if skill_res is not None and not skill_res.success:
        return GlobalSyncExecutionResult(
            success=False,
            skill_result=skill_res,
            refreshed_bundled=refreshed,
            replan_required=False,
            error_message=skill_res.error_message,
        )

    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    instruction_res: Optional[InstructionExecutionResult] = None

    if not global_instruction_source.is_file():
        instruction_res = InstructionExecutionResult(
            operations=plan.instruction_plan.operations
            if plan.instruction_plan
            else (),
            success=False,
            conflict_count=1,
            error_message=f"Global instruction file not found: {global_instruction_source}",
        )
        return GlobalSyncExecutionResult(
            success=False,
            skill_result=skill_res,
            instruction_result=instruction_res,
            refreshed_bundled=refreshed,
            instruction_success=False,
            replan_required=False,
            error_message=f"Global instruction file not found: {global_instruction_source}",
        )

    if plan.instruction_plan is not None and plan.instruction_plan.conflicts:
        instruction_res = InstructionExecutionResult(
            operations=plan.instruction_plan.operations,
            success=False,
            conflict_count=len(plan.instruction_plan.conflicts),
            error_message="Instruction targets have conflicts.",
        )
        return GlobalSyncExecutionResult(
            success=False,
            skill_result=skill_res,
            instruction_result=instruction_res,
            refreshed_bundled=refreshed,
            instruction_success=False,
            replan_required=False,
            error_message="Instruction targets have conflicts.",
        )

    if plan.instruction_plan is not None:
        exec_instructions = execute_instruction_plan_fn or execute_instruction_plan
        instruction_res = exec_instructions(
            plan.instruction_plan,
            home,
            dry_run=dry_run,
        )
        if not instruction_res.success:
            return GlobalSyncExecutionResult(
                success=False,
                skill_result=skill_res,
                instruction_result=instruction_res,
                refreshed_bundled=refreshed,
                instruction_success=False,
                replan_required=False,
                error_message="Instruction targets have conflicts.",
            )

    return GlobalSyncExecutionResult(
        success=True,
        skill_result=skill_res,
        instruction_result=instruction_res,
        refreshed_bundled=refreshed,
        findings=plan.findings,
        replan_required=plan.replan_required_after_apply,
    )


def sync_global_resources(
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
    container_path: Optional[Path] = None,
    load_agent_definitions_fn: Optional[Callable] = None,
    execute_global_skills_fn: Optional[
        Callable[..., GlobalSkillExecutionResult]
    ] = None,
    execute_instruction_plan_fn: Optional[
        Callable[..., InstructionExecutionResult]
    ] = None,
) -> GlobalSyncExecutionResult:
    """Synchronize global resources (skills and instructions) via structured execution plan."""
    if container_path is None:
        container_path = home / ".agents" / "skills"

    plan = build_global_sync_plan(
        aikito_dir,
        home,
        dry_run=dry_run,
        container_path=container_path,
        load_agent_definitions_fn=load_agent_definitions_fn,
    )

    if not plan.can_apply:
        return GlobalSyncExecutionResult(
            success=False,
            findings=plan.findings,
            replan_required=False,
            error_message=plan.error_message or "Global sync plan cannot be applied.",
        )

    res = execute_global_sync_plan(
        plan,
        aikito_dir,
        home,
        dry_run=dry_run,
        execute_global_skills_fn=execute_global_skills_fn,
        execute_instruction_plan_fn=execute_instruction_plan_fn,
    )

    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    if not global_instruction_source.is_file():
        return GlobalSyncExecutionResult(
            success=False,
            findings=res.findings,
            skill_result=res.skill_result,
            instruction_result=res.instruction_result,
            refreshed_bundled=res.refreshed_bundled,
            error_message=f"Global instruction file not found: {global_instruction_source}",
        )

    if plan.instruction_plan and plan.instruction_plan.conflicts:
        return GlobalSyncExecutionResult(
            success=False,
            findings=res.findings,
            skill_result=res.skill_result,
            instruction_result=res.instruction_result,
            refreshed_bundled=res.refreshed_bundled,
            error_message="Conflicts detected in global instruction plan.",
        )

    return res


@dataclass(frozen=True)
class WorkspaceSyncRequest:
    """Request parameters for building a WorkspaceSyncPlan."""

    workspace_root: Path
    home: Path
    force: bool = False
    prune: bool = False
    force_targets: tuple[str, ...] = ()
    skills: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ProjectSyncEntry:
    """Structured representation of a configured project within a workspace."""

    project_name: str
    binding_status: str  # "active", "offline", "unbound", "error"
    config_data: dict[str, Any] = field(default_factory=dict)
    batch: ProjectSyncBatch | None = None
    offline_paths: tuple[str, ...] = ()
    error_message: str | None = None

    def observe(self) -> PlanObservation:
        """Project entry into a PlanObservation."""
        if self.batch is not None:
            batch_obs = self.batch.observe()
            if self.binding_status == "error" or self.error_message:
                finding = Finding(
                    status="ERROR",
                    code="PROJECT_CONFIG_ERROR",
                    message=self.error_message
                    or f"Project '{self.project_name}' has configuration errors.",
                    resource=self.project_name,
                )
                return combine_observations(
                    [batch_obs],
                    additional_findings=(finding,),
                    can_apply=False,
                )
            return batch_obs

        if self.binding_status == "error" or self.error_message:
            finding = Finding(
                status="ERROR",
                code="PROJECT_CONFIG_ERROR",
                message=self.error_message
                or f"Project '{self.project_name}' has configuration errors.",
                resource=self.project_name,
            )
            return PlanObservation(
                operations=(),
                findings=(finding,),
                can_apply=False,
            )

        return PlanObservation(
            operations=(),
            findings=(),
            can_apply=True,
        )


@dataclass(frozen=True)
class WorkspaceSyncPlan:
    """Fully evaluated, immutable workspace-wide synchronization plan.

    Aggregates global resources, subagents, MCP servers, and project batches
    into a single structured plan before any mutations are performed.
    """

    workspace_root: Path
    home: Path
    global_plan: GlobalSyncPlan
    subagent_plan: SubagentPlan | None
    mcp_plan: MCPPlan | None
    project_entries: tuple[ProjectSyncEntry, ...]
    findings: tuple[Finding, ...] = ()
    can_apply: bool = True
    replan_required_after_apply: bool = False
    # Builders set this explicitly so legacy child copies stay out of observations.
    owned_findings: tuple[Finding, ...] | None = None

    def __post_init__(self) -> None:
        if self.can_apply:
            obs = self.observe()
            if not obs.can_apply:
                object.__setattr__(self, "can_apply", False)

    def observe(self) -> PlanObservation:
        """Project workspace sync plan and all child components into a unified PlanObservation."""
        children: list[PlanObservation] = []
        g_obs = safe_observe_plan(self.global_plan)
        if g_obs is not None:
            children.append(g_obs)
        if self.subagent_plan is not None:
            s_obs = safe_observe_plan(self.subagent_plan)
            if s_obs is not None:
                children.append(s_obs)
        if self.mcp_plan is not None:
            m_obs = safe_observe_plan(self.mcp_plan)
            if m_obs is not None:
                children.append(m_obs)
        for entry in self.project_entries:
            e_obs = safe_observe_plan(entry)
            if e_obs is not None:
                children.append(e_obs)

        workspace_findings = (
            self.owned_findings if self.owned_findings is not None else self.findings
        )

        return combine_observations(
            children,
            additional_findings=workspace_findings,
            can_apply=self.can_apply,
        )

    @property
    def changes(self) -> int:
        return self.observe().summary.changes

    @property
    def unchanged(self) -> int:
        return self.observe().summary.unchanged

    @property
    def offline(self) -> int:
        return sum(
            1 for entry in self.project_entries if entry.binding_status == "offline"
        )

    @property
    def conflicts(self) -> tuple[str, ...]:
        result: list[str] = []
        obs = self.observe()

        # 1. Observation conflicts
        for f in obs.findings:
            if is_conflict_finding(f) and f.message not in result:
                result.append(f.message)

        # 2. Legacy MCP ERROR inclusion (in legacy v1.50, MCP errors counted as conflicts)
        for f in obs.findings:
            if f.code == "MCP_ERROR" and f.message not in result:
                result.append(f.message)

        # 3. Include any legacy conflict findings from global_plan.findings or self.findings
        for f in (*self.global_plan.findings, *self.findings):
            if is_conflict_finding(f) and f.message not in result:
                result.append(f.message)

        return tuple(result)

    @property
    def errors(self) -> tuple[str, ...]:
        result: list[str] = []
        if (
            self.global_plan.error_message
            and self.global_plan.error_message not in result
        ):
            result.append(self.global_plan.error_message)

        # Observation errors (excluding MCP_ERROR which legacy put in conflicts)
        obs = self.observe()
        for f in obs.findings:
            if (
                is_error_finding(f)
                and f.code != "MCP_ERROR"
                and f.message not in result
            ):
                result.append(f.message)

        for entry in self.project_entries:
            if entry.error_message and entry.error_message not in result:
                result.append(entry.error_message)
            if entry.batch is not None:
                for err in entry.batch.preflight_findings:
                    if err not in result:
                        result.append(err)

        for f in self.findings:
            if is_error_finding(f) and f.message not in result:
                result.append(f.message)

        return tuple(result)

    @property
    def warnings(self) -> tuple[str, ...]:
        result: list[str] = []
        obs = self.observe()
        for f in obs.findings:
            if is_warning_finding(f) and f.message not in result:
                result.append(f.message)

        for f in self.findings:
            if is_warning_finding(f) and f.message not in result:
                result.append(f.message)

        return tuple(result)

    @property
    def observation(self) -> PlanObservation:
        return self.observe()

    def render(self, *, verbose: bool = False) -> str:
        lines = [
            "Sync plan",
            "",
            f"  Changes:   {self.changes}",
            f"  Unchanged: {self.unchanged}",
            f"  Offline:   {self.offline}",
            f"  Warnings:  {len(self.warnings)}",
            f"  Conflicts: {len(self.conflicts)}",
            f"  Errors:    {len(self.errors)}",
        ]
        important = (*self.warnings, *self.conflicts, *self.errors)
        if important:
            lines.extend(("", "Needs attention:"))
            lines.extend(f"  {line}" for line in important)
        lines.extend(
            (
                "",
                "Safe to apply" if self.can_apply else "Blocked; no changes were made",
            )
        )
        if verbose:
            details: list[str] = []
            if self.global_plan.bundled_refresh_plan:
                b_obs = safe_observe_plan(self.global_plan.bundled_refresh_plan)
                if b_obs is not None:
                    for view in b_obs.operations:
                        if view.effect != OperationEffect.NOOP:
                            details.append(
                                f"  [{view.domain_action}] bundled skill '{view.resource_name}'"
                            )
            if self.global_plan.skill_plan:
                s_obs = safe_observe_plan(self.global_plan.skill_plan)
                if s_obs is not None:
                    for view in s_obs.operations:
                        if view.effect != OperationEffect.NOOP:
                            details.append(
                                f"  [{view.domain_action}] {view.source} -> {view.target}"
                            )
            if self.global_plan.instruction_plan:
                i_obs = safe_observe_plan(self.global_plan.instruction_plan)
                if i_obs is not None:
                    for view in i_obs.operations:
                        if view.effect != OperationEffect.NOOP:
                            details.append(
                                f"  [{view.domain_action}] {view.source} -> {view.target}"
                            )
            if self.subagent_plan:
                sub_obs = safe_observe_plan(self.subagent_plan)
                if sub_obs is not None:
                    for view in sub_obs.operations:
                        if view.effect != OperationEffect.NOOP:
                            details.append(
                                f"  [{view.domain_action}] {view.agent}/{view.resource_name} -> {view.target}"
                            )
            if self.mcp_plan:
                mcp_obs = safe_observe_plan(self.mcp_plan)
                if mcp_obs is not None:
                    for view in mcp_obs.operations:
                        if view.effect != OperationEffect.NOOP:
                            details.append(
                                f"  [{view.domain_action}] {view.agent}/{view.resource_name} ({view.reason})"
                            )
            for entry in self.project_entries:
                if entry.binding_status == "offline":
                    candidates_str = ", ".join(entry.offline_paths) or "-"
                    details.append(
                        f"  Project '{entry.project_name}': offline on this host ({candidates_str}), skipping."
                    )
                elif entry.binding_status == "unbound":
                    details.append(
                        f"  Project '{entry.project_name}': no configured paths (unbound), skipping."
                    )
                elif entry.binding_status == "active" and entry.batch:
                    p_skill_obs = safe_observe_plan(entry.batch.skill_plan)
                    if p_skill_obs is not None:
                        for view in p_skill_obs.operations:
                            if view.effect != OperationEffect.NOOP:
                                details.append(
                                    f"  [{view.domain_action}] {view.project}/{view.resource_name} -> {view.target}"
                                )
            if details:
                lines.extend(("", "Details", "", *details))
        return "\n".join(lines)


@dataclass(frozen=True)
class WorkspaceSyncExecutionResult:
    """Segmented execution outcome of a WorkspaceSyncPlan."""

    success: bool
    global_result: GlobalSyncExecutionResult | None = None
    subagent_result: SubagentExecutionResult | None = None
    mcp_result: MCPExecutionResult | None = None
    project_results: tuple[ProjectSyncExecutionResult, ...] = ()
    findings: tuple[Finding, ...] = ()
    replan_required: bool = False
    error_message: str | None = None

    def __bool__(self) -> bool:
        return self.success


def build_workspace_sync_plan(
    request: WorkspaceSyncRequest | Path,
    home: Path | None = None,
    *,
    force: bool = False,
    prune: bool = False,
    force_targets: Sequence[str] | None = None,
    load_agent_definitions_fn: Optional[Callable[..., Any]] = None,
    outdated_bundled_skills_fn: Optional[Callable[[Path], Sequence[str]]] = None,
    build_subagent_plan_fn: Optional[Callable[..., Any]] = None,
    build_mcp_plan_fn: Optional[Callable[..., Any]] = None,
) -> WorkspaceSyncPlan:
    """Construct an immutable, fully-evaluated WorkspaceSyncPlan strictly read-only.

    Enforces INV-APP-01, INV-APP-06, and INV-APP-07.
    """
    if isinstance(request, Path):
        h = home or Path.home()
        req = WorkspaceSyncRequest(
            workspace_root=request,
            home=h,
            force=force,
            prune=prune,
            force_targets=tuple(force_targets or ()),
        )
    else:
        req = request

    workspace_root = req.workspace_root
    user_home = req.home

    # 1. Global sync plan
    global_plan = build_global_sync_plan(
        workspace_root,
        user_home,
        skills=req.skills,
        load_agent_definitions_fn=load_agent_definitions_fn,
        outdated_bundled_skills_fn=outdated_bundled_skills_fn,
    )

    # 2. Subagent plan
    subagent_plan: SubagentPlan | None = None
    sub_builder = build_subagent_plan_fn or build_subagent_plan
    try:
        subagent_plan = sub_builder(
            aikito_dir=workspace_root,
            home=user_home,
            allow_empty=True,
            force_targets=list(req.force_targets) if req.force_targets else None,
            prune=req.prune,
        )
    except SubagentConfigError as exc:
        subagent_err_finding = Finding(
            status="error",
            message=f"Subagent configuration error: {exc}",
            resource="subagents",
            code="SUBAGENT_CONFIG_ERROR",
        )
        return WorkspaceSyncPlan(
            workspace_root=workspace_root,
            home=user_home,
            global_plan=global_plan,
            subagent_plan=None,
            mcp_plan=None,
            project_entries=(),
            findings=(*global_plan.findings, subagent_err_finding),
            owned_findings=(subagent_err_finding,),
            can_apply=False,
        )

    # 3. MCP plan
    mcp_plan: MCPPlan | None = None
    mcp_builder = build_mcp_plan_fn or build_mcp_plan
    try:
        mcp_plan = mcp_builder(
            aikito_dir=workspace_root,
            home=user_home,
        )
    except MCPConfigError as exc:
        mcp_err_finding = Finding(
            status="error",
            message=f"MCP configuration error: {exc}",
            resource="mcp",
            code="MCP_CONFIG_ERROR",
        )
        return WorkspaceSyncPlan(
            workspace_root=workspace_root,
            home=user_home,
            global_plan=global_plan,
            subagent_plan=subagent_plan,
            mcp_plan=None,
            project_entries=(),
            findings=(*global_plan.findings, mcp_err_finding),
            owned_findings=(mcp_err_finding,),
            can_apply=False,
        )

    # 4. Project entries
    project_entries: list[ProjectSyncEntry] = []
    project_findings: list[Finding] = []
    projects_dir = workspace_root / "projects"

    if projects_dir.is_dir():
        proj_dirs = sorted(
            [
                p
                for p in projects_dir.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            ],
            key=lambda p: p.name,
        )
        for proj_dir in proj_dirs:
            project_name = proj_dir.name
            agent_toml = proj_dir / "agent.toml"
            if not agent_toml.is_file():
                continue

            try:
                with open(agent_toml, "rb") as f:
                    data = tomllib.load(f)
            except (OSError, tomllib.TOMLDecodeError) as exc:
                err_msg = (
                    f"Failed to read configuration for project '{project_name}': {exc}"
                )
                project_findings.append(
                    Finding(
                        status="error",
                        message=err_msg,
                        resource=project_name,
                        code="PROJECT_CONFIG_ERROR",
                    )
                )
                project_entries.append(
                    ProjectSyncEntry(
                        project_name=project_name,
                        binding_status="error",
                        error_message=err_msg,
                    )
                )
                continue

            binding = resolve_project_binding(data, user_home)
            if not binding.entries:
                project_entries.append(
                    ProjectSyncEntry(
                        project_name=project_name,
                        binding_status="unbound",
                        config_data=data,
                    )
                )
                continue

            offline_paths = tuple(e.raw_path for e in binding.offline_entries)
            if not binding.active_entries:
                project_entries.append(
                    ProjectSyncEntry(
                        project_name=project_name,
                        binding_status="offline",
                        config_data=data,
                        offline_paths=offline_paths,
                    )
                )
                continue

            batch = build_project_sync_batch(
                workspace_root,
                user_home,
                project_name,
                data,
                force=req.force,
            )
            for err in batch.preflight_findings:
                project_findings.append(
                    Finding(
                        status="error",
                        message=err,
                        resource=project_name,
                        code="PREFLIGHT_ERROR",
                    )
                )
            skill_obs = safe_observe_plan(batch.skill_plan)
            if skill_obs is not None:
                for f in skill_obs.findings:
                    if is_conflict_finding(f):
                        project_findings.append(
                            Finding(
                                status="conflict",
                                message=f.message,
                                resource=project_name,
                                code="SKILL_CONFLICT",
                            )
                        )

            project_entries.append(
                ProjectSyncEntry(
                    project_name=project_name,
                    binding_status="active",
                    config_data=data,
                    batch=batch,
                    offline_paths=offline_paths,
                )
            )

    all_findings = (*global_plan.findings, *project_findings)
    can_apply = (
        global_plan.can_apply
        and (subagent_plan is None or subagent_plan.can_apply)
        and (mcp_plan is None or mcp_plan.can_apply)
        and all(e.batch.can_apply for e in project_entries if e.batch is not None)
        and not any(e.binding_status == "error" for e in project_entries)
        and not any(f.status.lower() in ("error", "fail") for f in all_findings)
    )

    replan_required_after_apply = global_plan.replan_required_after_apply and any(
        e.batch is not None for e in project_entries
    )

    return WorkspaceSyncPlan(
        workspace_root=workspace_root,
        home=user_home,
        global_plan=global_plan,
        subagent_plan=subagent_plan,
        mcp_plan=mcp_plan,
        project_entries=tuple(project_entries),
        findings=all_findings,
        owned_findings=(),
        can_apply=can_apply,
        replan_required_after_apply=replan_required_after_apply,
    )


def execute_workspace_sync_plan(
    plan: WorkspaceSyncPlan,
    workspace: Path,
    home: Path,
    *,
    dry_run: bool = False,
    execute_global_fn: Optional[Callable[..., GlobalSyncExecutionResult]] = None,
    execute_subagent_fn: Optional[Callable[..., SubagentExecutionResult]] = None,
    execute_mcp_fn: Optional[Callable[..., MCPExecutionResult]] = None,
    apply_project_batch_fn: Optional[Callable[..., ProjectSyncExecutionResult]] = None,
) -> WorkspaceSyncExecutionResult:
    """Execute a WorkspaceSyncPlan with segmented failure boundaries and same-plan fidelity.

    Enforces INV-APP-02, INV-APP-04, and INV-APP-05.
    """
    plan_obs = plan.observe()
    if not dry_run and (not plan.can_apply or not plan_obs.can_apply):
        findings = plan.findings + tuple(
            finding for finding in plan_obs.findings if finding not in plan.findings
        )
        return WorkspaceSyncExecutionResult(
            success=False,
            findings=findings,
            replan_required=False,
            error_message="Workspace sync plan contains unhandled conflicts or errors; cannot apply.",
        )

    # 1. Global Sync
    exec_global = execute_global_fn or execute_global_sync_plan
    global_res = exec_global(plan.global_plan, workspace, home, dry_run=dry_run)
    if not global_res.success:
        return WorkspaceSyncExecutionResult(
            success=False,
            global_result=global_res,
            findings=plan.findings,
            replan_required=False,
            error_message=global_res.error_message or "Global sync failed.",
        )

    # Check if bundled skills refresh requires replan before project sync
    has_projects_with_batches = any(e.batch is not None for e in plan.project_entries)
    if not dry_run and global_res.replan_required and has_projects_with_batches:
        return WorkspaceSyncExecutionResult(
            success=False,
            global_result=global_res,
            findings=plan.findings,
            replan_required=True,
            error_message=(
                "Bundled skills refreshed or canonical skills changed during global sync; "
                "workspace sync plan invalidated. Please re-run 'aikito sync'."
            ),
        )

    # 2. Subagents
    sub_res: SubagentExecutionResult | None = None
    if plan.subagent_plan is not None:
        if dry_run:
            sub_obs = plan.subagent_plan.observe()
            sub_res = SubagentExecutionResult(
                success=plan.subagent_plan.can_apply,
                applied_count=0,
                noop_count=sub_obs.summary.unchanged,
                skipped_count=sub_obs.summary.skipped,
                conflict_count=plan.subagent_plan.conflicts_count,
                failed_count=0,
            )
        else:
            exec_sub = execute_subagent_fn or execute_subagent_plan
            sub_res = exec_sub(plan.subagent_plan, home=home)
        if not sub_res.success:
            return WorkspaceSyncExecutionResult(
                success=False,
                global_result=global_res,
                subagent_result=sub_res,
                findings=plan.findings,
                replan_required=False,
                error_message=sub_res.error_message or "Subagents sync failed.",
            )

    # 3. MCP
    mcp_res: MCPExecutionResult | None = None
    if plan.mcp_plan is not None:
        if dry_run:
            mcp_obs = plan.mcp_plan.observe()
            mcp_res = MCPExecutionResult(
                success=plan.mcp_plan.can_apply,
                applied_count=0,
                noop_count=mcp_obs.summary.unchanged,
                skipped_count=mcp_obs.summary.skipped,
                conflict_count=plan.mcp_plan.conflicts_count,
                failed_count=0,
            )
        else:
            exec_mcp = execute_mcp_fn or execute_mcp_plan
            mcp_res = exec_mcp(plan.mcp_plan, home=home)
        if not mcp_res.success:
            return WorkspaceSyncExecutionResult(
                success=False,
                global_result=global_res,
                subagent_result=sub_res,
                mcp_result=mcp_res,
                findings=plan.findings,
                replan_required=False,
                error_message=mcp_res.error_message or "MCP sync failed.",
            )

    # 4. Projects
    project_results: list[ProjectSyncExecutionResult] = []
    projects_ok = True
    first_proj_error: str | None = None
    apply_batch = apply_project_batch_fn or apply_project_sync_batch

    for entry in plan.project_entries:
        if entry.batch is None:
            continue
        p_res = apply_batch(entry.batch, entry.config_data, home, dry_run=dry_run)
        project_results.append(p_res)
        if not p_res.is_success:
            projects_ok = False
            if first_proj_error is None:
                first_proj_error = (
                    p_res.error_message
                    or f"Project '{entry.project_name}' sync failed."
                )

    overall_success = projects_ok and plan.can_apply
    return WorkspaceSyncExecutionResult(
        success=overall_success,
        global_result=global_res,
        subagent_result=sub_res,
        mcp_result=mcp_res,
        project_results=tuple(project_results),
        findings=plan.findings,
        replan_required=False,
        error_message=first_proj_error if not overall_success else None,
    )


plan_workspace_sync = build_workspace_sync_plan
