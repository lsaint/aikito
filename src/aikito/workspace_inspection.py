"""Build each resource plan once for a read-only workspace inspection request.

Callers share this snapshot while retaining their own presentation and error
boundaries. A failed resource plan does not prevent other resources from being
inspected.
"""

from __future__ import annotations

from functools import cached_property
from pathlib import Path
from typing import Any, Sequence

from .agents import AgentRegistry, load_agent_definitions
from .global_skills import build_global_skill_batch, plan_global_skills
from .instructions import build_global_instruction_batch, plan_instructions
from .mcp import build_mcp_plan, evaluate_spec_status, load_agent_specs
from .memory_runtime import build_project_memory_batch, plan_project_memory
from .subagent import build_subagent_plan


class WorkspaceInspection:
    """Lazy, request-scoped source of resource inspection facts and plan metadata."""

    def __init__(self, workspace_root: Path, home: Path) -> None:
        self.workspace_root = workspace_root
        self.home = home
        self._skill_plans: dict[tuple[tuple[str, ...], bool], Any] = {}
        self._memory_plans: dict[tuple[str, Path], Any] = {}

    @cached_property
    def agents(self) -> Any:
        return load_agent_definitions(self.workspace_root, self.home)

    @cached_property
    def instruction_plan(self) -> Any:
        registry = AgentRegistry(self.agents)
        batch = build_global_instruction_batch(
            self.workspace_root, self.home, registry=registry
        )
        return plan_instructions(batch, self.home)

    @cached_property
    def instruction_views(self) -> tuple[Any, ...]:
        return self.instruction_plan.inspect()

    @property
    def instruction_targets(self) -> tuple[Any, ...]:
        return self.instruction_plan.batch.targets

    def skill_plan(
        self,
        skills: Sequence[str],
        *,
        registry: AgentRegistry | None = None,
        use_registered_agents: bool = True,
    ) -> Any:
        key = (tuple(sorted(skills)), use_registered_agents)
        if key not in self._skill_plans:
            effective_registry = (
                registry or AgentRegistry(self.agents)
                if use_registered_agents
                else registry
            )
            batch = build_global_skill_batch(
                self.workspace_root,
                self.home,
                skills=skills,
                registry=effective_registry,
                container_path=self.home / ".agents" / "skills",
            )
            self._skill_plans[key] = plan_global_skills(batch, self.home, dry_run=True)
        return self._skill_plans[key]

    def skill_views(
        self,
        skills: Sequence[str],
        *,
        registry: AgentRegistry | None = None,
        use_registered_agents: bool = True,
    ) -> tuple[Any, ...]:
        return self.skill_plan(
            skills,
            registry=registry,
            use_registered_agents=use_registered_agents,
        ).inspect()

    def skill_consumers(self, skills: Sequence[str]) -> tuple[Any, ...]:
        return self.skill_plan(skills).batch.consumers

    @cached_property
    def subagent_plan(self) -> Any:
        return build_subagent_plan(self.workspace_root, self.home, allow_empty=True)

    @cached_property
    def subagent_views(self) -> tuple[Any, ...]:
        return self.subagent_plan.inspect()

    @property
    def subagent_configs(self) -> Any:
        return self.subagent_plan.agent_configs

    @cached_property
    def mcp_specs(self) -> Any:
        return load_agent_specs(self.workspace_root, self.home)

    @cached_property
    def mcp_plan(self) -> Any:
        return build_mcp_plan(self.workspace_root, home=self.home, specs=self.mcp_specs)

    @cached_property
    def mcp_views(self) -> tuple[Any, ...]:
        return self.mcp_plan.inspect()

    def ensure_mcp_plan(self) -> None:
        """Expose plan construction failures without leaking the plan to callers."""
        self.mcp_plan

    def mcp_status(self, spec: Any) -> str:
        """Return the domain's inspection status, preserving per-spec fallback."""
        try:
            plan = self.mcp_plan
        except Exception:
            return evaluate_spec_status(spec, home=self.home)
        for view in self.mcp_views:
            if view.agent == spec.agent and view.resource_name == spec.server:
                return view.status.value
        return evaluate_spec_status(spec, home=self.home, plan=plan)

    def project_memory_views(
        self, project_name: str, config: dict[str, Any], checkout: Path
    ) -> tuple[Any, ...]:
        key = (project_name, checkout)
        if key not in self._memory_plans:
            batch = build_project_memory_batch(
                self.workspace_root,
                project_name,
                config,
                active_checkouts=[checkout],
            )
            self._memory_plans[key] = plan_project_memory(batch)
        return self._memory_plans[key].inspect()


def inspect_workspace(workspace_root: Path, home: Path) -> WorkspaceInspection:
    """Start a shared read-only inspection snapshot for one command request."""
    return WorkspaceInspection(workspace_root, home)
