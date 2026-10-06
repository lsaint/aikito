"""Validate platform fields against the definitions in the resulting workspace."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .agents import (
    AgentDefinition,
    AgentRegistry,
    _load_subagent_capability,
    bundled_agent_spec,
)
from .subagent_adapters import SubagentConfigError, get_subagent_adapter


def validate_platform_opts(
    agent_name: str,
    subagent_name: str,
    platform_opts: dict[str, Any],
    *,
    agents: Mapping[str, AgentDefinition] | None = None,
) -> dict[str, Any]:
    # The optional bundled lookup preserves the standalone validation API.
    # Workspace write paths must supply the batch's resulting definitions.
    if agents is None:
        capability = _load_subagent_capability(
            bundled_agent_spec(agent_name), agent_name, Path.home()
        )
    else:
        definition = agents.get(agent_name)
        capability = definition.subagents if definition else None
    if capability is None:
        raise SubagentConfigError(
            f"Subagent '{subagent_name}' platform '{agent_name}' has no defined subagents capability"
        )
    return get_subagent_adapter(capability.config_format).validate_options(
        agent_name, subagent_name, platform_opts
    )


def validate_subagent_metadata(
    metadata: Mapping[str, Any], name: str, agents: Mapping[str, AgentDefinition]
) -> None:
    for platform, options in metadata.items():
        if platform not in ("description", "agents"):
            validate_platform_opts(platform, name, options, agents=agents)


def definitions_from_document(
    document: Mapping[str, Any], home: Path
) -> dict[str, AgentDefinition]:
    registry = AgentRegistry.from_document(document, home)
    return {
        name: AgentDefinition(
            name=agent.name,
            display_name=agent.display_name,
            instruction_path=agent.instruction_path,
            project_instruction_path=agent.project_instruction_path,
            skills_path=agent.skills_path,
            detect=agent.detect,
            subagents=_load_subagent_capability(document["agents"][name], name, home),
        )
        for name, agent in registry.items()
    }
