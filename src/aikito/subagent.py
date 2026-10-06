"""Synchronize canonical Aikito Subagent definitions into supported agent configs."""

import os
import re
import shutil
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from collections.abc import Mapping, Sequence

from .agents import (
    Agent,
    AgentRegistryError,
    is_agent_installed,
    load_agent_definitions,
)
from .config_runtime import (
    ConfigOperation,
    ConfigTarget,
    FileMutationPlan,
    StaleConfigPlanError,
    aggregate_file_plans,
)
from .diagnostics import Finding, is_error_finding
from .inspection import InspectionStatus, ResourceInspectionView
from .plan_observation import (
    OperationEffect,
    PlanObservation,
    PlanOperationView,
    UnknownPlanActionError,
)


from .subagent_adapters import (
    SubagentConfigError as SubagentConfigError,
    MARKER_PREFIX as MARKER_PREFIX,
    get_marker_text as get_marker_text,
    has_aikito_marker_text as has_aikito_marker_text,
    has_aikito_marker as has_aikito_marker,
    render_copilot_markdown as render_copilot_markdown,
    render_claude_markdown as render_claude_markdown,
    render_opencode_markdown as render_opencode_markdown,
    render_grok_markdown as render_grok_markdown,
    render_pi_markdown as render_pi_markdown,
    render_codex_toml as render_codex_toml,
    render_agy_markdown as render_agy_markdown,
    render_dsh_cordis_subagent as render_dsh_cordis_subagent,
    get_dsh_cordis_subagent_item as get_dsh_cordis_subagent_item,
    get_all_dsh_cordis_subagents as get_all_dsh_cordis_subagents,
    update_dsh_cordis_subagent as update_dsh_cordis_subagent,
    remove_dsh_cordis_subagent as remove_dsh_cordis_subagent,
    check_codex_enabled as check_codex_enabled,
    get_subagent_adapter,
)
from .subagent_validation import validate_platform_opts as validate_platform_opts


DEFAULT_SUBAGENTS_CONFIG = Path("subagents")
SUBAGENTS_DIR = Path("subagents")
BACKUP_DIR = Path(".local/state/aikito/backups")

SUBAGENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9-]*$")


@dataclass(frozen=True)
class AgentSubagentConfig:
    agent_name: str
    display_name: str
    config_path: Path
    config_format: str
    requires_path: Path | None = None
    definition: Agent | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True)
class SubagentDefinition:
    name: str
    description: str
    agents: list[str]
    platform_configs: dict[str, dict[str, Any]]
    instructions: str


@dataclass(frozen=True)
class SubagentPlan:
    """Immutable, fully-evaluated synchronization plan for Subagents."""

    operations: tuple[ConfigOperation, ...]
    file_plans: tuple[FileMutationPlan, ...]
    agent_configs: Mapping[str, AgentSubagentConfig] = field(default_factory=dict)

    @property
    def can_apply(self) -> bool:
        return not any(
            (op.action == "CONFLICT" and not op.is_authorized) or op.action == "ERROR"
            for op in self.operations
        )

    @property
    def changes_count(self) -> int:
        return sum(
            1
            for op in self.operations
            if op.action in ("CREATE", "UPDATE", "REMOVE") and op.is_authorized
        )

    @property
    def conflicts_count(self) -> int:
        return sum(
            1
            for op in self.operations
            if op.action == "CONFLICT" and not op.is_authorized
        )

    def observe(self) -> PlanObservation:
        """Project plan into a pure PlanObservation."""
        views: list[PlanOperationView] = []
        findings: list[Finding] = []
        for op in self.operations:
            view, finding = observe_subagent_operation(op)
            views.append(view)
            if finding is not None:
                findings.append(finding)
        can_apply = self.can_apply and not any(is_error_finding(f) for f in findings)
        return PlanObservation(
            operations=tuple(views),
            findings=tuple(findings),
            can_apply=can_apply,
        )

    def inspect(self) -> tuple[ResourceInspectionView, ...]:
        """Project plan into structured resource inspection views."""
        views: list[ResourceInspectionView] = []
        for op in self.operations:
            if op.action in ("OK", "NOOP"):
                status = InspectionStatus.OK
            elif op.action == "CREATE":
                status = InspectionStatus.MISSING
            elif op.action == "UPDATE":
                status = InspectionStatus.UPDATE
            elif op.action in ("ORPHAN", "REMOVE"):
                status = InspectionStatus.ORPHAN
            elif op.action == "CONFLICT":
                status = InspectionStatus.CONFLICT
            elif op.action == "SKIP":
                status = InspectionStatus.SKIP
            elif op.action == "ERROR":
                status = InspectionStatus.ERROR
            else:
                status = InspectionStatus.ERROR

            finding = subagent_operation_finding(op)
            views.append(
                ResourceInspectionView(
                    resource_type="subagent",
                    resource_name=op.target.logical_identity,
                    status=status,
                    scope="global",
                    agent=op.target.agent,
                    target_path=op.target.path,
                    reason=op.reason,
                    finding=finding,
                )
            )
        return tuple(views)


def subagent_operation_effect(op: ConfigOperation) -> OperationEffect:
    """Map subagent operation action to canonical OperationEffect."""
    if op.action in ("CREATE", "UPDATE", "REMOVE") and not op.is_authorized:
        return OperationEffect.NONE
    match op.action:
        case "CREATE":
            return OperationEffect.CREATE
        case "UPDATE":
            return OperationEffect.UPDATE
        case "REMOVE":
            return OperationEffect.REMOVE
        case "NOOP":
            return OperationEffect.NOOP
        case "SKIP" | "ORPHAN":
            return OperationEffect.SKIP
        case "CONFLICT" | "ERROR":
            return OperationEffect.NONE
        case _:
            raise UnknownPlanActionError(f"Unhandled subagent action: {op.action}")


def subagent_operation_finding(op: ConfigOperation) -> Finding | None:
    """Produce a Finding for subagent orphan, conflict, or error conditions."""
    if op.action == "ORPHAN":
        return Finding(
            status="WARNING",
            code="SUBAGENT_ORPHAN",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    if (op.action == "CONFLICT" and not op.is_authorized) or (
        op.action in ("CREATE", "UPDATE", "REMOVE") and not op.is_authorized
    ):
        return Finding(
            status="CONFLICT",
            code="SUBAGENT_CONFLICT",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    if op.action == "ERROR":
        return Finding(
            status="ERROR",
            code="SUBAGENT_ERROR",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    return None


def observe_subagent_operation(
    op: ConfigOperation,
) -> tuple[PlanOperationView, Finding | None]:
    """Project a ConfigOperation into a PlanOperationView and optional Finding."""
    try:
        effect = subagent_operation_effect(op)
        finding = subagent_operation_finding(op)
    except UnknownPlanActionError as err:
        effect = OperationEffect.NONE
        finding = Finding(
            status="ERROR",
            code="UNKNOWN_PLAN_ACTION",
            message=str(err),
            resource=str(op.target.path),
        )

    view = PlanOperationView(
        resource_type="subagent",
        resource_name=op.target.logical_identity,
        effect=effect,
        scope="global",
        agent=op.target.agent,
        target=str(op.target.path),
        reason=op.reason,
        domain_action=op.action,
        authorized=op.is_authorized,
    )
    return view, finding


@dataclass(frozen=True)
class SubagentExecutionResult:
    """Structured execution result of applying a SubagentPlan."""

    success: bool
    applied_count: int
    noop_count: int
    skipped_count: int
    conflict_count: int
    failed_count: int
    failed_files: tuple[Path, ...] = ()
    backup_warnings: tuple[str, ...] = ()
    error_message: str | None = None
    partial_completion: bool = False
    recovery_required: bool = False


def load_all_agents(
    aikito_dir: Path, home: Path
) -> tuple[dict[str, AgentSubagentConfig], set[str]]:
    try:
        agents_data = load_agent_definitions(aikito_dir, home)
    except AgentRegistryError as exc:
        raise SubagentConfigError(str(exc)) from exc
    subagent_configs: dict[str, AgentSubagentConfig] = {}
    for agent_name, agent in agents_data.items():
        capability = agent.subagents
        if capability is None:
            continue
        try:
            get_subagent_adapter(capability.config_format)
        except SubagentConfigError as exc:
            raise SubagentConfigError(
                f"Agent '{agent_name}' unsupported config_format '{capability.config_format}'"
            ) from exc
        subagent_configs[agent_name] = AgentSubagentConfig(
            agent_name=agent_name,
            display_name=agent.display_name,
            config_path=capability.config_path,
            config_format=capability.config_format,
            requires_path=capability.requires_path,
            definition=agent,
        )

    return subagent_configs, set(agents_data)


def load_subagent_definitions(
    aikito_dir: Path, allow_empty: bool = False, *, home: Path | None = None
) -> dict[str, SubagentDefinition]:
    from .workspace.layout import (
        WorkspaceLayoutError,
        parse_subagent_file,
        require_current_layout,
    )

    try:
        require_current_layout(aikito_dir)
    except WorkspaceLayoutError as exc:
        raise SubagentConfigError(str(exc)) from exc
    directory = aikito_dir / SUBAGENTS_DIR
    if not directory.is_dir() or directory.is_symlink():
        raise SubagentConfigError(f"Subagents directory missing or unsafe: {directory}")
    try:
        agents = load_agent_definitions(
            aikito_dir, home if home is not None else Path.home()
        )
    except AgentRegistryError as exc:
        raise SubagentConfigError(str(exc)) from exc
    definitions: dict[str, SubagentDefinition] = {}
    for instr_path in sorted(directory.iterdir()):
        if instr_path.name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            continue
        name = instr_path.stem
        if instr_path.suffix != ".md" or not SUBAGENT_NAME_PATTERN.fullmatch(name):
            raise SubagentConfigError(f"Unsupported subagent entry: {instr_path}")
        try:
            subagent_info, body = parse_subagent_file(instr_path)
        except WorkspaceLayoutError as exc:
            raise SubagentConfigError(str(exc)) from exc
        description = subagent_info.get("description")
        target_agents = subagent_info.get("agents")
        platform_configs: dict[str, dict[str, Any]] = {}
        for key, val in subagent_info.items():
            if key in ("description", "agents"):
                continue
            # Read paths tolerate foreign platform metadata; write paths validate strictly.
            if key in agents:
                validate_platform_opts(key, name, val, agents=agents)
            platform_configs[key] = val

        definitions[name] = SubagentDefinition(
            name=name,
            description=description,
            agents=target_agents,
            platform_configs=platform_configs,
            instructions=body.strip(),
        )
    if not definitions and not allow_empty:
        raise SubagentConfigError("No subagents are defined")
    return definitions


def render_subagent(
    definition: SubagentDefinition, agent_config: AgentSubagentConfig
) -> str:
    adapter = get_subagent_adapter(agent_config.config_format)
    options = adapter.validate_options(
        agent_config.agent_name,
        definition.name,
        definition.platform_configs.get(agent_config.agent_name, {}),
    )
    instructions = definition.instructions.replace("\r\n", "\n").replace("\r", "\n")
    return adapter.render(
        definition.name, definition.description, options, instructions
    )


def get_target_subagent_path(
    agent_config: AgentSubagentConfig, subagent_name: str
) -> Path:
    return get_subagent_adapter(agent_config.config_format).resolve_target_path(
        agent_config.config_path, subagent_name
    )


def build_subagent_plan(
    aikito_dir: Path,
    home: Path,
    allow_empty: bool = False,
    gate_installed: bool = True,
    force_targets: Sequence[str] | None = None,
    prune: bool = False,
) -> SubagentPlan:
    subagent_configs, all_agent_names = load_all_agents(aikito_dir, home)
    subagent_defs = load_subagent_definitions(
        aikito_dir, allow_empty=allow_empty, home=home
    )

    authorized_force: set[str] = set()
    if force_targets is not None:
        if not force_targets:
            raise SubagentConfigError(
                "--force requires explicit <agent>/<subagent> target(s), e.g. --force claude-code/verifier"
            )
        for ft in force_targets:
            if "/" not in ft or len(ft.split("/")) != 2:
                raise SubagentConfigError(
                    f"Invalid --force target '{ft}'. Must be in format <agent>/<subagent>"
                )
            authorized_force.add(ft.strip())

    # Check that referenced agents exist in agents/<name>.toml with subagents config
    for sub_name, definition in subagent_defs.items():
        for ag_name in definition.agents:
            if ag_name not in subagent_configs:
                raise SubagentConfigError(
                    f"Subagent '{sub_name}' targets agent '{ag_name}', but '{ag_name}' has no [agents.{ag_name}.subagents] configuration in agents/<name>.toml"
                )

    operations: list[ConfigOperation] = []

    # Handle agents without subagents config (SKIP)
    for ag_name in sorted(all_agent_names):
        if ag_name not in subagent_configs:
            target = ConfigTarget(
                path=Path(""),
                logical_identity="*",
                agent=ag_name,
            )
            operations.append(
                ConfigOperation(
                    target=target,
                    action="SKIP",
                    reason="Agent has no subagents section in agents/<name>.toml",
                )
            )

    for agent_name, agent_config in sorted(subagent_configs.items()):
        adapter = get_subagent_adapter(agent_config.config_format)
        if (
            gate_installed
            and is_agent_installed(agent_config.definition or agent_name, home) is False
        ):
            has_subagents = False
            for sub_name, definition in sorted(subagent_defs.items()):
                if agent_name in definition.agents:
                    has_subagents = True
                    target_path = get_target_subagent_path(agent_config, sub_name)
                    key_path = (
                        ("subagent", sub_name)
                        if adapter.layout == "shared_patch"
                        else ()
                    )
                    target = ConfigTarget(
                        path=target_path,
                        logical_identity=sub_name,
                        key_path=key_path,
                        format=agent_config.config_format,
                        agent=agent_name,
                    )
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="SKIP",
                            reason=f"Agent '{agent_name}' is not installed on this host",
                        )
                    )
            if not has_subagents:
                target = ConfigTarget(
                    path=agent_config.config_path,
                    logical_identity="*",
                    format=agent_config.config_format,
                    agent=agent_name,
                )
                operations.append(
                    ConfigOperation(
                        target=target,
                        action="SKIP",
                        reason=f"Agent '{agent_name}' is not installed on this host",
                    )
                )
            continue

        if (
            agent_config.requires_path is not None
            and not agent_config.requires_path.exists()
        ):
            target = ConfigTarget(
                path=agent_config.config_path,
                logical_identity="*",
                format=agent_config.config_format,
                agent=agent_name,
            )
            operations.append(
                ConfigOperation(
                    target=target,
                    action="SKIP",
                    reason=f"Optional subagent capability is not installed at {agent_config.requires_path}",
                )
            )
            continue

        if adapter.availability_check:
            enabled, msg = adapter.availability_check(agent_config.config_path)
            if not enabled:
                target = ConfigTarget(
                    path=agent_config.config_path,
                    logical_identity="*",
                    format=agent_config.config_format,
                    agent=agent_name,
                )
                operations.append(
                    ConfigOperation(
                        target=target,
                        action="ERROR",
                        reason=msg,
                        is_authorized=False,
                    )
                )

        # Subagents targeting this agent
        defined_subagents_for_agent: set[str] = set()

        if adapter.layout == "shared_patch":
            patch_file = agent_config.config_path
            patch_text = (
                patch_file.read_text(encoding="utf-8") if patch_file.is_file() else ""
            )
            for sub_name in sorted(subagent_defs.keys()):
                definition = subagent_defs[sub_name]
                if agent_name not in definition.agents:
                    continue
                defined_subagents_for_agent.add(sub_name)
                target = ConfigTarget(
                    path=patch_file,
                    logical_identity=sub_name,
                    key_path=("subagent", sub_name),
                    format=agent_config.config_format,
                    agent=agent_name,
                )
                try:
                    rendered = render_subagent(definition, agent_config)
                except SubagentConfigError as exc:
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="ERROR",
                            reason=str(exc),
                            is_authorized=False,
                        )
                    )
                    continue

                existing_block = adapter.read_item(patch_text, sub_name)
                force_id = f"{agent_name}/{sub_name}"
                is_force_auth = force_id in authorized_force

                if existing_block is None:
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="CREATE",
                            reason="Subagent plugin item does not exist in cordis.patch.yml",
                            rendered_payload=rendered,
                        )
                    )
                elif has_aikito_marker_text(existing_block):
                    if existing_block.strip() == rendered.strip():
                        operations.append(
                            ConfigOperation(
                                target=target,
                                action="NOOP",
                                reason="Up to date",
                                rendered_payload=rendered,
                            )
                        )
                    else:
                        operations.append(
                            ConfigOperation(
                                target=target,
                                action="UPDATE",
                                reason="Subagent configuration changed in cordis.patch.yml",
                                rendered_payload=rendered,
                            )
                        )
                else:
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="CONFLICT" if not is_force_auth else "UPDATE",
                            reason="Target plugin item exists without Aikito marker",
                            requires_force=True,
                            force_identity=force_id,
                            is_authorized=is_force_auth,
                            rendered_payload=rendered,
                        )
                    )

            # Detect orphans in cordis.patch.yml
            for managed_name in adapter.list_managed(patch_file):
                if managed_name not in defined_subagents_for_agent:
                    target = ConfigTarget(
                        path=patch_file,
                        logical_identity=managed_name,
                        key_path=("subagent", managed_name),
                        format=agent_config.config_format,
                        agent=agent_name,
                    )
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="REMOVE" if prune else "ORPHAN",
                            reason="Managed subagent in cordis.patch.yml is no longer defined in subagents/<name>.toml",
                            requires_prune=True,
                            is_authorized=prune,
                        )
                    )
            continue

        for sub_name in sorted(subagent_defs.keys()):
            definition = subagent_defs[sub_name]
            if agent_name not in definition.agents:
                continue

            defined_subagents_for_agent.add(sub_name)
            target_path = get_target_subagent_path(agent_config, sub_name)
            target = ConfigTarget(
                path=target_path,
                logical_identity=sub_name,
                key_path=(),
                format=agent_config.config_format,
                agent=agent_name,
            )

            try:
                rendered = render_subagent(definition, agent_config)
            except SubagentConfigError as exc:
                operations.append(
                    ConfigOperation(
                        target=target,
                        action="ERROR",
                        reason=str(exc),
                        is_authorized=False,
                    )
                )
                continue

            force_id = f"{agent_name}/{sub_name}"
            is_force_auth = force_id in authorized_force

            if not target_path.exists():
                operations.append(
                    ConfigOperation(
                        target=target,
                        action="CREATE",
                        reason="Target file does not exist",
                        rendered_payload=rendered,
                    )
                )
            else:
                if has_aikito_marker(target_path):
                    current_content = target_path.read_text(
                        encoding="utf-8", errors="replace"
                    )
                    if current_content.replace("\r\n", "\n") == rendered.replace(
                        "\r\n", "\n"
                    ):
                        operations.append(
                            ConfigOperation(
                                target=target,
                                action="NOOP",
                                reason="Up to date",
                                rendered_payload=rendered,
                            )
                        )
                    else:
                        operations.append(
                            ConfigOperation(
                                target=target,
                                action="UPDATE",
                                reason="Content changed",
                                rendered_payload=rendered,
                            )
                        )
                else:
                    operations.append(
                        ConfigOperation(
                            target=target,
                            action="CONFLICT" if not is_force_auth else "UPDATE",
                            reason="Target file exists without Aikito marker",
                            requires_force=True,
                            force_identity=force_id,
                            is_authorized=is_force_auth,
                            rendered_payload=rendered,
                        )
                    )

        for sub_name, target_path in adapter.list_managed(
            agent_config.config_path
        ).items():
            if sub_name not in defined_subagents_for_agent:
                target = ConfigTarget(
                    path=target_path,
                    logical_identity=sub_name,
                    format=agent_config.config_format,
                    agent=agent_name,
                )
                operations.append(
                    ConfigOperation(
                        target=target,
                        action="REMOVE" if prune else "ORPHAN",
                        reason="Managed subagent is no longer defined in subagents/<name>.toml",
                        requires_prune=True,
                        is_authorized=prune,
                    )
                )

    valid_ops = [
        op
        for op in operations
        if str(op.target.path) and op.target.path != Path("") and op.action != "SKIP"
    ]
    raw_file_plans = aggregate_file_plans(valid_ops)

    # Compute and freeze final_content for each FileMutationPlan at plan time (INV-CFG-02)
    file_plans: list[FileMutationPlan] = []
    for fp in raw_file_plans:
        adapter = get_subagent_adapter(fp.format)
        if adapter.layout == "shared_patch":
            curr_text = fp.path.read_text(encoding="utf-8") if fp.path.is_file() else ""
            new_text = adapter.merge(curr_text, fp.operations)
            file_plans.append(
                FileMutationPlan(
                    path=fp.path,
                    physical_identity=fp.physical_identity,
                    format=fp.format,
                    sensitive=fp.sensitive,
                    pre_image=fp.pre_image,
                    operations=fp.operations,
                    final_content=new_text,
                )
            )
        else:
            final_content = None
            for op in fp.operations:
                if op.is_authorized:
                    if op.action in ("CREATE", "UPDATE"):
                        final_content = op.rendered_payload or ""
                    elif op.action in ("REMOVE", "ORPHAN"):
                        final_content = None
            file_plans.append(
                FileMutationPlan(
                    path=fp.path,
                    physical_identity=fp.physical_identity,
                    format=fp.format,
                    sensitive=fp.sensitive,
                    pre_image=fp.pre_image,
                    operations=fp.operations,
                    final_content=final_content,
                )
            )

    return SubagentPlan(
        operations=tuple(operations),
        file_plans=tuple(file_plans),
        agent_configs=subagent_configs,
    )


def _backup_file(home: Path, agent_name: str, target_path: Path) -> Path | None:
    if not target_path.is_file():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_path = home / BACKUP_DIR / agent_name / f"{timestamp}-{target_path.name}"
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(target_path, backup_path)
    return backup_path


def _write_file_atomic(target_path: Path, content: str) -> None:
    target_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target_path.with_suffix(f"{target_path.suffix}.tmp.{os.getpid()}")
    tmp_path.write_text(content, encoding="utf-8", newline="")
    tmp_path.replace(target_path)


def execute_subagent_plan(
    plan: SubagentPlan,
    home: Path,
) -> SubagentExecutionResult:
    """Execute a SubagentPlan, aggregating mutations per physical file."""
    if not plan.can_apply:
        return SubagentExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=plan.conflicts_count,
            failed_count=0,
            error_message="Subagent synchronization plan cannot be applied due to unhandled conflicts or errors.",
        )

    # First validate preconditions on all file plans
    for fp in plan.file_plans:
        if fp.has_mutations:
            try:
                fp.validate_precondition()
            except StaleConfigPlanError as e:
                return SubagentExecutionResult(
                    success=False,
                    applied_count=0,
                    noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
                    skipped_count=sum(
                        1 for op in plan.operations if op.action == "SKIP"
                    ),
                    conflict_count=0,
                    failed_count=1,
                    failed_files=(fp.path,),
                    error_message=f"Plan is stale: {e}",
                )

    applied_count = 0
    noop_count = sum(1 for op in plan.operations if op.action == "NOOP")
    skipped_count = sum(1 for op in plan.operations if op.action == "SKIP")
    failed_files: list[Path] = []
    backup_warnings: list[str] = []

    for fp in plan.file_plans:
        if not fp.has_mutations:
            continue

        try:
            if get_subagent_adapter(fp.format).layout == "shared_patch":
                # Precondition check passed; write frozen final_content without re-reading or re-planning
                if fp.final_content is not None:
                    if fp.path.is_file():
                        curr_text = fp.path.read_text(encoding="utf-8")
                        if curr_text != fp.final_content:
                            _backup_file(home, fp.operations[0].target.agent, fp.path)
                            _write_file_atomic(fp.path, fp.final_content)
                    else:
                        _write_file_atomic(fp.path, fp.final_content)
                applied_count += sum(
                    1
                    for op in fp.operations
                    if op.is_authorized
                    and op.action in ("CREATE", "UPDATE", "REMOVE", "ORPHAN")
                )

            else:
                for op in fp.operations:
                    if not op.is_authorized:
                        continue
                    content_to_write = (
                        fp.final_content
                        if fp.final_content is not None
                        else (op.rendered_payload or "")
                    )
                    if op.action == "CREATE":
                        _write_file_atomic(fp.path, content_to_write)
                        applied_count += 1
                    elif op.action == "UPDATE":
                        _backup_file(home, op.target.agent, fp.path)
                        _write_file_atomic(fp.path, content_to_write)
                        applied_count += 1
                    elif op.action in ("REMOVE", "ORPHAN"):
                        _backup_file(home, op.target.agent, fp.path)
                        if fp.path.is_file():
                            fp.path.unlink()
                        applied_count += 1

        except OSError as exc:
            failed_files.append(fp.path)
            return SubagentExecutionResult(
                success=False,
                applied_count=applied_count,
                noop_count=noop_count,
                skipped_count=skipped_count,
                conflict_count=0,
                failed_count=len(failed_files),
                failed_files=tuple(failed_files),
                backup_warnings=tuple(backup_warnings),
                error_message=f"Failed writing configuration to '{fp.path}': {exc}",
                partial_completion=applied_count > 0,
            )

    return SubagentExecutionResult(
        success=True,
        applied_count=applied_count,
        noop_count=noop_count,
        skipped_count=skipped_count,
        conflict_count=0,
        failed_count=0,
        backup_warnings=tuple(backup_warnings),
    )


def sync_subagent_configs(
    aikito_dir: Path,
    home: Path,
    dry_run: bool = False,
    force_targets: list[str] | None = None,
    prune: bool = False,
    plan: SubagentPlan | None = None,
) -> bool:
    if plan is None:
        normalized_force: set[str] = set()
        if force_targets is not None:
            if not force_targets:
                raise SubagentConfigError(
                    "--force requires explicit <agent>/<subagent> target(s), e.g. --force claude-code/verifier"
                )
            for ft in force_targets:
                if "/" not in ft or len(ft.split("/")) != 2:
                    raise SubagentConfigError(
                        f"Invalid --force target '{ft}'. Must be in format <agent>/<subagent>"
                    )
                normalized_force.add(ft.strip())

        plan = build_subagent_plan(
            aikito_dir=aikito_dir,
            home=home,
            allow_empty=True,
            force_targets=force_targets,
            prune=prune,
        )

    has_errors = any(op.action == "ERROR" for op in plan.operations)
    has_unforced_conflicts = any(
        op.action == "CONFLICT" and not op.is_authorized for op in plan.operations
    )

    print(f"[INFO] Subagent synchronization plan (dry_run={dry_run}):")

    for op in plan.operations:
        target_key = f"{op.target.agent}/{op.target.logical_identity}"
        if op.action == "SKIP":
            print(f"  [SKIP] {op.target.agent} ({op.reason})")
        elif op.action == "NOOP":
            print(f"  [OK] {target_key}")
        elif op.action == "CREATE":
            print(f"  [CREATE] {target_key} -> {op.target.path}")
        elif op.action == "UPDATE":
            if op.requires_force:
                print(f"  [FORCE UPDATE] {target_key} -> {op.target.path}")
            else:
                print(f"  [UPDATE] {target_key} -> {op.target.path}")
        elif op.action == "CONFLICT":
            if op.is_authorized:
                print(f"  [FORCE UPDATE] {target_key} -> {op.target.path}")
            else:
                print(
                    f"  [CONFLICT] {target_key} -> {op.target.path} ({op.reason}. Use --force {target_key} to overwrite)"
                )
        elif op.action == "REMOVE":
            print(f"  [PRUNE] {target_key} -> {op.target.path}")
        elif op.action == "ORPHAN":
            if prune:
                print(f"  [PRUNE] {target_key} -> {op.target.path}")
            else:
                print(
                    f"  [ORPHAN] {target_key} -> {op.target.path} ({op.reason}. Use --prune to remove)"
                )
        elif op.action == "ERROR":
            print(f"  [ERROR] {target_key}: {op.reason}")

    if has_errors:
        print(
            "[ERROR] Synchronization aborted due to errors in plan. No changes were made.",
            file=sys.stderr,
        )
        return False

    if has_unforced_conflicts:
        print(
            "[WARN] Synchronization aborted due to unhandled conflicts. No changes were made.",
            file=sys.stderr,
        )
        return False

    if dry_run:
        print("[SUCCESS] Subagent synchronization plan completed (dry-run).")
        return True

    result = execute_subagent_plan(plan, home)
    if not result.success:
        print(
            f"[ERROR] Subagent synchronization failed: {result.error_message}",
            file=sys.stderr,
        )
        return False

    print("[SUCCESS] Subagent synchronization completed successfully.")
    return True


def status_subagent_configs(aikito_dir: Path, home: Path) -> bool:
    plan = build_subagent_plan(aikito_dir, home)
    all_ok = True

    print("[INFO] Subagent Status Report:")
    for op in plan.operations:
        target_key = f"{op.target.agent}/{op.target.logical_identity}"
        if op.action == "SKIP":
            print(f"  [SKIP] {op.target.agent}: no subagents configured")
        elif op.action == "NOOP":
            print(f"  [OK] {target_key}")
        elif op.action in ("CREATE", "UPDATE"):
            all_ok = False
            print(f"  [{op.action}] {target_key} -> {op.target.path} ({op.reason})")
        elif op.action == "CONFLICT":
            all_ok = False
            print(f"  [CONFLICT] {target_key} -> {op.target.path}")
        elif op.action in ("ORPHAN", "REMOVE"):
            all_ok = False
            print(f"  [ORPHAN] {target_key} -> {op.target.path}")
        elif op.action == "ERROR":
            all_ok = False
            print(f"  [ERROR] {target_key}: {op.reason}")

    if all_ok:
        print("[SUCCESS] All subagent configurations are up-to-date.")
    return all_ok
