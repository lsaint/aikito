"""Resolve and persist the active Aikito workspace, and provide public Workspace facade."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .compat import get_workspace_config_dir
from .diagnostics import Finding
from .doctor import run_doctor
from .mcp import load_agents
from .project import ProjectSummary, collect_project_summaries
from .subagent import load_subagent_definitions
from .workspace_sync import WorkspaceSyncPlan, plan_workspace_sync


class WorkspaceError(RuntimeError):
    """Base class for public Aikito workspace API errors."""


class WorkspaceNotFoundError(WorkspaceError, FileNotFoundError):
    """Raised when a specified or resolved workspace directory does not exist."""


class InvalidWorkspaceError(WorkspaceError, ValueError):
    """Raised when a workspace path is relative or malformed."""


@dataclass(frozen=True)
class WorkspaceInspection:
    """Read-only diagnostic and configuration snapshot of an Aikito workspace."""

    workspace_dir: Path
    configured_agents: tuple[str, ...] = ()
    projects: tuple[ProjectSummary, ...] = ()
    diagnostics: tuple[Finding, ...] = ()
    mcps: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    ready_for_sync: bool = True


@dataclass(frozen=True)
class WorkspaceSyncPreview:
    """Read-only preview of workspace synchronization operations."""

    plan: WorkspaceSyncPlan
    changes: int
    unchanged: int
    offline: int
    warnings: int
    conflicts: int
    errors: int
    can_apply: bool
    will_mutate: bool
    findings: tuple[Finding, ...] = ()
    operations: tuple[str, ...] = ()


@dataclass(frozen=True)
class Workspace:
    """Public facade for inspecting and planning operations on an Aikito workspace."""

    path: Path
    home: Path

    @classmethod
    def load(
        cls,
        workspace: Path | str | None = None,
        home: Path | str | None = None,
    ) -> Workspace:
        """Load a workspace strictly read-only without modifying pointers or files."""
        home_path = (
            Path.home().resolve() if home is None else Path(home).expanduser().resolve()
        )
        if workspace is None:
            workspace_path = resolve_workspace(home_path)
        else:
            supplied = Path(workspace).expanduser()
            if not supplied.is_absolute():
                raise InvalidWorkspaceError(
                    f"Aikito workspace path must be absolute: {workspace}"
                )
            workspace_path = supplied.resolve()

        if not workspace_path.is_dir():
            raise WorkspaceNotFoundError(
                f"Aikito workspace directory not found: {workspace_path}"
            )

        return cls(path=workspace_path, home=home_path)

    def inspect(self) -> WorkspaceInspection:
        """Return a strictly read-only structured inspection of the workspace."""
        # 1. Configured agents
        agents_file = self.path / "agents.toml"
        if agents_file.is_file():
            try:
                agents_def = load_agents(self.path, self.home)
                configured_agents = tuple(sorted(agents_def.keys()))
            except Exception:
                configured_agents = ()
        else:
            configured_agents = ()

        # 2. Projects
        projects = tuple(collect_project_summaries(self.path, self.home))

        # 3. MCPs
        mcps_dir = self.path / "mcps"
        mcps = (
            tuple(sorted(p.stem for p in mcps_dir.glob("*.toml") if p.is_file()))
            if mcps_dir.is_dir()
            else ()
        )

        # 4. Skills
        skills_dir = self.path / "skills"
        skills = (
            tuple(
                sorted(
                    p.name
                    for p in skills_dir.iterdir()
                    if p.is_dir() and not p.name.startswith(".")
                )
            )
            if skills_dir.is_dir()
            else ()
        )

        # 5. Subagents
        subagents_set: set[str] = set()
        subagents_toml = self.path / "subagents.toml"
        if subagents_toml.is_file():
            try:
                subs = load_subagent_definitions(self.path)
                subagents_set.update(subs.keys())
            except Exception:
                pass
        subagents_dir = self.path / "subagents"
        if subagents_dir.is_dir():
            for p in subagents_dir.glob("*.md"):
                if p.is_file():
                    subagents_set.add(p.stem)
        subagents = tuple(sorted(subagents_set))

        # 6. Diagnostics & readiness
        doctor_report = run_doctor(self.path, self.home)
        findings: list[Finding] = []
        for section in doctor_report.sections:
            for f in section.findings:
                if f.status in ("FAIL", "WARN"):
                    findings.append(f)

        return WorkspaceInspection(
            workspace_dir=self.path,
            configured_agents=configured_agents,
            projects=projects,
            diagnostics=tuple(findings),
            mcps=mcps,
            skills=skills,
            subagents=subagents,
            ready_for_sync=doctor_report.fail_count == 0,
        )

    def plan_sync(self) -> WorkspaceSyncPreview:
        """Return a strictly read-only synchronization preview."""
        plan = plan_workspace_sync(self.path, self.home)

        operations: list[str] = []
        if plan.global_plan.instruction_plan:
            for target, action, _ in getattr(
                plan.global_plan.instruction_plan, "planned_operations", ()
            ):
                operations.append(f"Instructions {action}: {target}")
        if plan.global_plan.skill_plan:
            for op in getattr(plan.global_plan.skill_plan, "operations", ()):
                operations.append(
                    f"Skill {getattr(op, 'action', '')}: {getattr(op, 'agent', '')}/{getattr(op, 'skill', '')}"
                )
        if plan.subagent_plan:
            for op in plan.subagent_plan.operations:
                if op.is_authorized:
                    operations.append(f"Subagent {op.action}: {op.agent}/{op.subagent}")
        if plan.mcp_plan:
            for op in getattr(plan.mcp_plan, "operations", ()):
                operations.append(
                    f"MCP {getattr(op, 'action', '')}: {getattr(op, 'agent', '')}/{getattr(op, 'server', '')}"
                )
        for entry in plan.project_entries:
            if entry.batch:
                operations.append(
                    f"Project {entry.project_name}: {entry.batch.agent} ({entry.candidate_path})"
                )

        return WorkspaceSyncPreview(
            plan=plan,
            changes=plan.changes,
            unchanged=plan.unchanged,
            offline=plan.offline,
            warnings=plan.warnings,
            conflicts=plan.conflicts,
            errors=plan.errors,
            can_apply=plan.can_apply,
            will_mutate=plan.changes > 0,
            findings=plan.findings,
            operations=tuple(operations),
        )


def get_workspace_pointer_path(home: Path) -> Path:
    """Return the user-level file that stores the default workspace path."""
    return get_workspace_config_dir(home) / "workspace"


def resolve_workspace_with_source(home: Path) -> tuple[Path, str]:
    """Resolve the workspace and identify whether it came from env, config, or default."""
    env_dir = os.environ.get("AIKITO_DIR")
    if env_dir:
        return Path(env_dir).expanduser().resolve(), "AIKITO_DIR"

    pointer_path = get_workspace_pointer_path(home)
    try:
        configured_dir = pointer_path.read_text(encoding="utf-8").strip()
    except OSError:
        configured_dir = ""
    if configured_dir:
        return Path(configured_dir).expanduser().resolve(), "configured"

    return (home / "aikito").resolve(), "default"


def resolve_workspace(home: Path) -> Path:
    """Resolve the workspace using environment, persisted choice, then default."""
    return resolve_workspace_with_source(home)[0]


def persist_workspace(workspace: Path, home: Path) -> Path:
    """Persist the workspace selected by a successful explicit initialization."""
    pointer_path = get_workspace_pointer_path(home)
    pointer_path.parent.mkdir(parents=True, exist_ok=True)
    pointer_path.write_text(f"{workspace.expanduser().resolve()}\n", encoding="utf-8")
    return pointer_path
