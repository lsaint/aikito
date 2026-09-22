"""Workspace-level synchronization coordinator, global planning, and bundled skill lifecycle."""

from __future__ import annotations

import os
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from .agents import AgentRegistry
from .bundled_skills import (
    BundledSkillRefreshError,
    outdated_bundled_skills,
    refresh_bundled_skills,
)
from .conflict import collect_resource_conflicts
from .diagnostics import Finding
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
from .mcp import MCPConfigError, load_agents
from .skill_state import SkillWriterLock
from .templating import BUNDLED_SKILL_NAMES


@dataclass(frozen=True)
class BundledSkillRefreshOperation:
    skill_name: str
    action: str  # "NOOP" or "REFRESH"
    reason: str = ""


@dataclass(frozen=True)
class BundledSkillRefreshPlan:
    operations: tuple[BundledSkillRefreshOperation, ...] = ()
    refreshed_names: tuple[str, ...] = ()
    can_apply: bool = True
    replan_required: bool = False
    error_message: str | None = None


@dataclass(frozen=True)
class GlobalSyncPlan:
    bundled_refresh_plan: BundledSkillRefreshPlan
    skill_plan: Any | None
    instruction_plan: Any | None
    findings: tuple[Finding, ...] = ()
    can_apply: bool = True
    replan_required_after_apply: bool = False
    error_message: str | None = None


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
    fn = outdated_bundled_skills_fn or outdated_bundled_skills
    outdated = tuple(fn(workspace_root))
    operations = tuple(
        BundledSkillRefreshOperation(
            skill_name=name,
            action="REFRESH" if name in outdated else "NOOP",
            reason="Bundled skill diverged from package" if name in outdated else "Bundled skill matches package"
        )
        for name in BUNDLED_SKILL_NAMES
    )
    return BundledSkillRefreshPlan(
        operations=operations,
        refreshed_names=outdated,
        can_apply=True,
        replan_required=len(outdated) > 0,
    )


def execute_bundled_refresh_plan(
    plan: BundledSkillRefreshPlan,
    workspace_root: Path,
    home: Path,
    *,
    dry_run: bool = False,
    refresh_fn: Optional[Callable[..., Sequence[str]]] = None,
) -> tuple[str, ...]:
    if not plan.refreshed_names and not any(op.action == "REFRESH" for op in plan.operations):
        return ()
    fn = refresh_fn or refresh_bundled_skills
    try:
        res = fn(workspace_root, home, dry_run=dry_run)
        return tuple(res)
    except TypeError:
        return fn(workspace_root, home, dry_run=dry_run)


def build_global_sync_plan(
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
    container_path: Path | None = None,
    outdated_bundled_skills_fn: Optional[Callable[[Path], Sequence[str]]] = None,
    load_agents_fn: Optional[Callable[[Path, Path], Any]] = None,
) -> GlobalSyncPlan:
    """Construct a read-only plan for global skills and instructions."""
    skills_toml_path = aikito_dir / "skills.toml"
    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    findings: list[Finding] = []

    if container_path is None:
        agents_env = os.environ.get("AIKITO_AGENTS_DIR")
        container_path = Path(agents_env) / "skills" if agents_env else home / ".agents" / "skills"

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
        agent_loader = load_agents_fn or load_agents
        agents = agent_loader(aikito_dir, home)
    except MCPConfigError as exc:
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
    for op in skill_plan.all_operations:
        if op.action == "CONFLICT":
            can_apply = False
            findings.append(
                Finding(
                    status="CONFLICT",
                    code=op.rule_id or "SKILL_CONFLICT",
                    message=op.reason,
                    resource=str(getattr(op, "canonical_path", None) or getattr(op, "target_path", "")),
                )
            )

    if not global_instruction_source.is_file():
        can_apply = False
        findings.append(
            Finding(
                status="ERROR",
                code="GLOBAL_INSTRUCTION_MISSING",
                message=f"Global instruction file not found: {global_instruction_source}",
                resource=str(global_instruction_source),
            )
        )

    if instruction_plan.conflicts:
        can_apply = False
        for op in instruction_plan.conflicts:
            findings.append(
                Finding(
                    status="CONFLICT",
                    code=op.rule_id or "INSTRUCTION_CONFLICT",
                    message=op.reason,
                    resource=str(getattr(op, "target_path", "")),
                )
            )

    return GlobalSyncPlan(
        bundled_refresh_plan=bundled_plan,
        skill_plan=skill_plan,
        instruction_plan=instruction_plan,
        findings=tuple(findings),
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
    refresh_bundled_skills_fn: Optional[Callable[..., Sequence[str]]] = None,
    execute_global_skills_fn: Optional[Callable[..., GlobalSkillExecutionResult]] = None,
    execute_instruction_plan_fn: Optional[Callable[..., InstructionExecutionResult]] = None,
) -> GlobalSyncExecutionResult:
    """Execute global skill, instruction, and bundled refresh plans under proper lock boundaries."""
    has_skill_conflicts = (
        plan.skill_plan is not None
        and any(op.action == "CONFLICT" for op in plan.skill_plan.all_operations)
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
                    refresh_fn=refresh_bundled_skills_fn,
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
                refresh_fn=refresh_bundled_skills_fn,
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
            operations=plan.instruction_plan.operations if plan.instruction_plan else (),
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
