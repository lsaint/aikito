"""Resolve and persist the active Aikito workspace, and provide public Workspace facade."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from .compat import get_workspace_config_dir
from .doctor import run_doctor
from .agents import load_agent_definitions
from .plan_observation import OperationEffect, PlanOperationView
from .project import collect_project_summaries
from .subagent import load_subagent_definitions
from .workspace_sync import build_workspace_sync_plan


class WorkspaceError(RuntimeError):
    """Base class for public Aikito workspace API errors."""


class WorkspaceNotFoundError(WorkspaceError, FileNotFoundError):
    """Raised when a specified or resolved workspace directory does not exist."""


class InvalidWorkspaceError(WorkspaceError, ValueError):
    """Raised when a workspace path is relative or malformed."""


@dataclass(frozen=True)
class WorkspaceFinding:
    """Read-only diagnostic finding presented by the public Workspace API."""

    status: str
    code: str
    message: str
    resource: str = ""
    fix_hint: str = ""


@dataclass(frozen=True)
class WorkspaceProjectView:
    """Read-only view of a project configured in an Aikito workspace."""

    name: str
    status: str
    active_paths: tuple[str, ...] = ()
    offline_paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkspaceInspection:
    """Read-only diagnostic and configuration snapshot of an Aikito workspace."""

    workspace_dir: Path
    configured_agents: tuple[str, ...] = ()
    projects: tuple[WorkspaceProjectView, ...] = ()
    diagnostics: tuple[WorkspaceFinding, ...] = ()
    mcps: tuple[str, ...] = ()
    skills: tuple[str, ...] = ()
    subagents: tuple[str, ...] = ()
    ready_for_sync: bool = True


@dataclass(frozen=True)
class WorkspaceOperationView:
    """Read-only, structured projection of a planned workspace operation.

    Provides safe, typed metadata about a resource mutation or inspection
    without exposing raw domain mutation objects, file handles, or secrets.

    Migration Strategy:
        This model replaces the legacy human-readable string descriptions
        found in `WorkspaceSyncPreview.operations`. Callers should prefer
        inspecting `WorkspaceSyncPreview.operation_views` for programmatic
        filtering, grouping, and metrics.
    """

    resource_type: str
    """Category of the resource (e.g. 'global_skill', 'bundled_skill', 'instruction', 'subagent', 'mcp', 'project')."""

    resource_name: str
    """Logical identifier or name of the resource."""

    effect: str
    """Planned lifecycle effect: 'create', 'update', 'remove', 'state_only', 'noop', 'skip', or 'none'."""

    scope: str = ""
    """Scope of operation: 'global', 'project', etc."""

    agent: str = ""
    """Agent associated with the operation (e.g. 'claude', 'codex'), if agent-scoped."""

    project: str = ""
    """Project name associated with the operation, if project-scoped."""

    target: str = ""
    """Target path or destination representation."""

    source: str = ""
    """Canonical or source path representation."""

    reason: str = ""
    """Human-readable explanation or justification for the operation/effect."""

    domain_action: str = ""
    """Underlying domain action code (e.g. 'CREATE', 'UPDATE', 'REFRESH', 'UNLINK', etc.)."""

    authorized: bool = True
    """Whether the operation is authorized to proceed without conflict."""

    @classmethod
    def from_plan_operation(cls, view: PlanOperationView) -> WorkspaceOperationView:
        """Project an internal PlanOperationView into the public WorkspaceOperationView."""
        return cls(
            resource_type=view.resource_type,
            resource_name=view.resource_name,
            effect=view.effect.value,
            scope=view.scope,
            agent=view.agent,
            project=view.project,
            target=view.target,
            source=view.source,
            reason=view.reason,
            domain_action=view.domain_action,
            authorized=view.authorized,
        )


@dataclass(frozen=True)
class WorkspaceSyncPreview:
    """Read-only preview of workspace synchronization operations.

    Attributes:
        workspace_path: Absolute path to the inspected workspace.
        changes: Total count of mutative changes.
        unchanged: Total count of unchanged (NOOP) resources.
        offline: Count of offline project checkouts.
        warnings: Count of warning-level diagnostics.
        conflicts: Count of blocking conflicts.
        errors: Count of error-level diagnostics.
        can_apply: True if the plan is authorized and ready to execute.
        will_mutate: True if applying the plan will perform file writes.
        findings: Sequence of diagnostic findings.
        operations: Legacy human-readable string summaries of operations.
            Prefer `operation_views` for structured inspection.
        operation_views: Structured, frozen projections of each planned operation.
    """

    workspace_path: Path
    changes: int
    unchanged: int
    offline: int
    warnings: int
    conflicts: int
    errors: int
    can_apply: bool
    will_mutate: bool
    findings: tuple[WorkspaceFinding, ...] = ()
    operations: tuple[str, ...] = ()
    operation_views: tuple[WorkspaceOperationView, ...] = ()


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
        agents_dir = self.path / "agents"
        if agents_dir.is_dir():
            try:
                agents_def = load_agent_definitions(self.path, self.home)
                configured_agents = tuple(sorted(agents_def.keys()))
            except Exception:
                configured_agents = ()
        else:
            configured_agents = ()

        # 2. Projects
        project_summaries = collect_project_summaries(self.path, self.home)
        projects = tuple(
            WorkspaceProjectView(
                name=p.name,
                status=p.runtime_status,
                active_paths=tuple(str(path) for _, path in p.active_paths),
                offline_paths=tuple(str(path) for _, path in p.offline_paths),
            )
            for p in project_summaries
        )

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
        subagents_dir = self.path / "subagents"
        if subagents_dir.is_dir():
            try:
                subs = load_subagent_definitions(self.path)
                subagents_set.update(subs.keys())
            except Exception:
                pass
        if subagents_dir.is_dir():
            for p in subagents_dir.glob("*.md"):
                if p.is_file():
                    subagents_set.add(p.stem)
        subagents = tuple(sorted(subagents_set))

        # 6. Diagnostics & readiness
        doctor_report = run_doctor(self.path, self.home)
        findings: list[WorkspaceFinding] = []
        for section in doctor_report.sections:
            for f in section.findings:
                if f.status in ("FAIL", "WARN"):
                    findings.append(
                        WorkspaceFinding(
                            status=f.status,
                            code=f.code,
                            message=f.message,
                            resource=f.resource,
                            fix_hint=f.fix_hint,
                        )
                    )

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
        plan = build_workspace_sync_plan(self.path, self.home)

        operations: list[str] = []
        structured_views: list[WorkspaceOperationView] = []
        obs = plan.observe()
        for view in obs.operations:
            structured_views.append(WorkspaceOperationView.from_plan_operation(view))
            if (
                view.resource_type == "bundled_skill"
                and view.effect == OperationEffect.UPDATE
            ):
                operations.append(
                    f"Bundled Skill {view.resource_name}: {view.domain_action}"
                )
            elif view.resource_type == "global_skill" and view.scope == "global":
                if view.effect in (
                    OperationEffect.CREATE,
                    OperationEffect.UPDATE,
                    OperationEffect.REMOVE,
                ) or (view.effect == OperationEffect.NONE and not view.authorized):
                    name = view.resource_name or Path(view.target).name
                    operations.append(f"Global Skill {view.domain_action}: {name}")
            elif view.resource_type == "instruction" and view.scope == "global":
                if view.effect in (
                    OperationEffect.CREATE,
                    OperationEffect.REMOVE,
                ) or (view.effect == OperationEffect.NONE and not view.authorized):
                    name = view.resource_name or Path(view.target).name
                    operations.append(
                        f"Global Instructions {view.domain_action}: {name}"
                    )
            elif view.resource_type == "subagent" and view.authorized:
                operations.append(
                    f"Subagent {view.domain_action}: {view.agent}/{view.resource_name}"
                )
            elif view.resource_type == "mcp" and view.authorized:
                operations.append(
                    f"MCP {view.domain_action}: {view.agent}/{view.resource_name}"
                )

        for entry in plan.project_entries:
            if entry.batch:
                b = entry.batch
                if b.active_checkouts:
                    checkouts_str = ", ".join(str(c) for c in b.active_checkouts)
                    operations.append(f"Project {entry.project_name}: {checkouts_str}")
                    structured_views.append(
                        WorkspaceOperationView(
                            resource_type="project",
                            resource_name=entry.project_name,
                            effect="noop",
                            project=entry.project_name,
                            target=checkouts_str,
                            domain_action="active_checkouts",
                            reason=f"Active checkouts: {checkouts_str}",
                        )
                    )
                else:
                    operations.append(
                        f"Project {entry.project_name}: no active checkouts"
                    )
                    structured_views.append(
                        WorkspaceOperationView(
                            resource_type="project",
                            resource_name=entry.project_name,
                            effect="noop",
                            project=entry.project_name,
                            domain_action="no_active_checkouts",
                            reason="No active checkouts",
                        )
                    )
            elif entry.binding_status == "offline":
                operations.append(f"Project {entry.project_name}: offline")
                structured_views.append(
                    WorkspaceOperationView(
                        resource_type="project",
                        resource_name=entry.project_name,
                        effect="skip",
                        project=entry.project_name,
                        domain_action="offline",
                        reason="Project binding is offline",
                    )
                )

        preview_findings = tuple(
            WorkspaceFinding(
                status=f.status,
                code=f.code,
                message=f.message,
                resource=f.resource,
                fix_hint=f.fix_hint,
            )
            for f in obs.findings
        )

        return WorkspaceSyncPreview(
            workspace_path=self.path,
            changes=plan.changes,
            unchanged=plan.unchanged,
            offline=plan.offline,
            warnings=len(plan.warnings),
            conflicts=len(plan.conflicts),
            errors=len(plan.errors),
            can_apply=obs.can_apply and plan.can_apply,
            will_mutate=plan.changes > 0,
            findings=preview_findings,
            operations=tuple(operations),
            operation_views=tuple(structured_views),
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
