"""Pure planning for MCP synchronization without side effects."""

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from ..config_runtime import (
    capture_file_snapshot,
    resolve_physical_identity,
    ConfigCollisionError,
)
from ..plan_observation import (
    Finding,
    OperationEffect,
    PlanOperationView,
    UnknownPlanActionError,
)
from .adapters import (
    _entry_matches_desired,
    _fingerprint,
    _read_entry,
    _remove_entry,
    _update_entry,
)
from .loader import _agent_detected, load_agent_specs
from .model import (
    STATE_FILE,
    AgentSpec,
    MCPConfigTarget,
    MCPDesiredEntry,
    MCPFilePlan,
    MCPObservedEntry,
    MCPOperation,
    MCPPlan,
    _state_file_hash,
)


def mcp_operation_effect(op: MCPOperation) -> OperationEffect:
    """Map MCP operation action to canonical OperationEffect."""
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
        case "SKIP":
            return OperationEffect.SKIP
        case "CONFLICT":
            if op.is_authorized:
                raise UnknownPlanActionError(
                    f"Authorized CONFLICT is invalid for MCP: {op.target.logical_identity}"
                )
            return OperationEffect.NONE
        case "ERROR":
            return OperationEffect.NONE
        case _:
            raise UnknownPlanActionError(f"Unhandled MCP action: {op.action}")


def mcp_operation_finding(op: MCPOperation) -> Finding | None:
    """Produce a Finding for MCP conflict or error conditions."""
    if (op.action == "CONFLICT" and not op.is_authorized) or (
        op.action in ("CREATE", "UPDATE", "REMOVE") and not op.is_authorized
    ):
        return Finding(
            status="CONFLICT",
            code="MCP_CONFLICT",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    if op.action == "ERROR":
        return Finding(
            status="ERROR",
            code="MCP_ERROR",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    return None


def observe_mcp_operation(
    op: MCPOperation,
) -> tuple[PlanOperationView, Finding | None]:
    """Project an MCPOperation into a PlanOperationView and optional Finding."""
    try:
        effect = mcp_operation_effect(op)
        finding = mcp_operation_finding(op)
    except UnknownPlanActionError as err:
        effect = OperationEffect.NONE
        finding = Finding(
            status="ERROR",
            code="UNKNOWN_PLAN_ACTION",
            message=str(err),
            resource=str(op.target.path),
        )

    view = PlanOperationView(
        resource_type="mcp",
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


def build_mcp_plan(
    aikito_dir: Path,
    home: Path,
    *,
    specs: Sequence[AgentSpec] | None = None,
    force: bool = False,
    force_targets: set[str] | Sequence[str] | None = None,
    desired_absent_servers: set[str] | Sequence[str] | None = None,
) -> MCPPlan:
    from .executor import _load_state

    """Build a pure, immutable MCP synchronization plan without modifying any files or state.

    Enforces INV-MCP-01, INV-MCP-02, INV-MCP-04, INV-MCP-05, INV-CFG-01, INV-CFG-02, INV-CFG-03.
    """
    if specs is not None:
        raw_specs = list(specs)
    else:
        raw_specs = load_agent_specs(aikito_dir, home)

    absent_servers = set(desired_absent_servers or ())
    force_targets_set = set(force_targets or ())

    state = _load_state(home)
    entries = state.get("entries", {})
    state_snapshot_hash = _state_file_hash(home / STATE_FILE)

    groups: dict[str, list[AgentSpec]] = defaultdict(list)
    canonical_paths: dict[str, Path] = {}
    unsupported_operations: list[MCPOperation] = []

    for s in raw_specs:
        if s.config_format == "unsupported":
            # Unsupported agents never own a config file: skip without reading,
            # grouping, or collision-checking their (possibly empty) path.
            unsupported_operations.append(
                MCPOperation(
                    target=MCPConfigTarget(
                        path=s.config_path,
                        logical_identity=s.server,
                        key_path=("mcpServers", s.target_name),
                        format=s.config_format,
                        agent=s.agent,
                        target_name=s.target_name,
                    ),
                    action="SKIP",
                    reason=s.reason or "MCP synchronization is not supported",
                    spec=s,
                    is_authorized=True,
                )
            )
            continue
        phys_id = resolve_physical_identity(s.config_path)
        groups[phys_id].append(s)
        if phys_id not in canonical_paths:
            canonical_paths[phys_id] = s.config_path

    # Check collisions across specs
    for phys_id, g_specs in groups.items():
        canonical_path = canonical_paths[phys_id]
        formats = {s.config_format for s in g_specs if s.config_format}
        if len(formats) > 1:
            raise ConfigCollisionError(
                f"Conflicting formats declared for physical file '{canonical_path}': {sorted(formats)}"
            )

        seen_target_names: dict[str, AgentSpec] = {}
        for s in g_specs:
            t_name = s.target_name
            is_absent = (s.server in absent_servers) or (s.desired is None)
            if t_name in seen_target_names:
                prev_s = seen_target_names[t_name]
                prev_is_absent = (prev_s.server in absent_servers) or (
                    prev_s.desired is None
                )
                if prev_s.server != s.server:
                    raise ConfigCollisionError(
                        f"Colliding MCP server names: '{prev_s.server}' and '{s.server}' both map to target name '{t_name}' in '{canonical_path}'"
                    )
                elif prev_is_absent != is_absent:
                    raise ConfigCollisionError(
                        f"Conflicting operations on MCP server '{t_name}' in '{canonical_path}': conflicting REMOVE and UPDATE"
                    )
            else:
                seen_target_names[t_name] = s

    all_operations: list[MCPOperation] = []
    file_plans: list[MCPFilePlan] = []

    for phys_id, g_specs in groups.items():
        canonical_path = canonical_paths[phys_id]
        resolved_format = g_specs[0].config_format if g_specs else ""
        file_sensitive = any(
            s.contains_secret or s.config_format in ("claude_json", "agy_json")
            for s in g_specs
        )
        file_snapshot = capture_file_snapshot(
            canonical_path, format=resolved_format, sensitive=file_sensitive
        )
        file_existed = file_snapshot.exists
        orig_text = canonical_path.read_text(encoding="utf-8") if file_existed else ""
        current_text = orig_text
        group_ops: list[MCPOperation] = []

        for spec in g_specs:
            target_key = f"{spec.agent}/{spec.server}"
            is_authorized = (
                force
                or (target_key in force_targets_set)
                or (spec.server in force_targets_set)
            )
            is_absent = (spec.server in absent_servers) or (spec.desired is None)
            config_target = MCPConfigTarget(
                path=canonical_path,
                logical_identity=spec.server,
                key_path=("mcpServers", spec.target_name),
                format=spec.config_format,
                agent=spec.agent,
                sensitive=spec.contains_secret
                or spec.config_format in ("claude_json", "agy_json"),
                target_name=spec.target_name,
            )

            if is_absent:
                if not file_existed:
                    observed = MCPObservedEntry(
                        target=config_target,
                        exists=False,
                        fingerprint=None,
                        managed_fingerprint=None,
                        is_managed=False,
                        raw_entry=None,
                    )
                    op = MCPOperation(
                        target=config_target,
                        action="NOOP",
                        reason="Target file does not exist",
                        observed=observed,
                        desired=None,
                        spec=spec,
                        requires_force=False,
                        force_identity=target_key,
                        is_authorized=True,
                        state_transition=(spec.state_key, None),
                    )
                    group_ops.append(op)
                    all_operations.append(op)
                    continue

                try:
                    current = _read_entry(spec, current_text)
                except Exception as exc:
                    op = MCPOperation(
                        target=config_target,
                        action="ERROR",
                        reason=f"Failed to read entry: {exc}",
                        spec=spec,
                        is_authorized=False,
                    )
                    group_ops.append(op)
                    all_operations.append(op)
                    continue

                previous = entries.get(spec.state_key, {})
                managed_fp = previous.get("fingerprint")
                current_fp = _fingerprint(current) if current is not None else None
                observed = MCPObservedEntry(
                    target=config_target,
                    exists=current is not None,
                    fingerprint=current_fp,
                    managed_fingerprint=managed_fp,
                    is_managed=managed_fp is not None,
                    raw_entry=current,
                )

                if current is None:
                    op = MCPOperation(
                        target=config_target,
                        action="NOOP",
                        reason="Already absent",
                        observed=observed,
                        desired=None,
                        spec=spec,
                        requires_force=False,
                        force_identity=target_key,
                        is_authorized=True,
                        state_transition=(spec.state_key, None),
                    )
                else:
                    safe_to_remove = is_authorized or (
                        managed_fp is not None and current_fp == managed_fp
                    )
                    if not safe_to_remove:
                        op = MCPOperation(
                            target=config_target,
                            action="CONFLICT",
                            reason="Existing config was not last written by aikito; review it or rerun with --force",
                            observed=observed,
                            desired=None,
                            spec=spec,
                            requires_force=True,
                            force_identity=target_key,
                            is_authorized=False,
                        )
                    else:
                        op = MCPOperation(
                            target=config_target,
                            action="REMOVE",
                            reason="Removed from agent configuration",
                            observed=observed,
                            desired=None,
                            spec=spec,
                            requires_force=(
                                managed_fp is None or current_fp != managed_fp
                            ),
                            force_identity=target_key,
                            is_authorized=True,
                            state_transition=(spec.state_key, None),
                        )
                        try:
                            current_text = _remove_entry(spec, current_text)
                        except Exception as exc:
                            op = MCPOperation(
                                target=config_target,
                                action="ERROR",
                                reason=f"Failed to remove entry: {exc}",
                                spec=spec,
                                is_authorized=False,
                            )
                group_ops.append(op)
                all_operations.append(op)
                continue

            # Desired Present
            if not spec.enabled:
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=spec.reason or "Agent or server disabled",
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            if not _agent_detected(spec):
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=f"Agent '{spec.agent}' is not installed or detected",
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            try:
                current = _read_entry(spec, current_text) if file_existed else None
            except Exception as exc:
                op = MCPOperation(
                    target=config_target,
                    action="ERROR",
                    reason=f"Failed to read entry: {exc}",
                    spec=spec,
                    is_authorized=False,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            previous = entries.get(spec.state_key, {})
            managed_fp = previous.get("fingerprint")
            current_fp = _fingerprint(current) if current is not None else None
            desired_fp = _fingerprint(spec.desired)
            observed = MCPObservedEntry(
                target=config_target,
                exists=current is not None,
                fingerprint=current_fp,
                managed_fingerprint=managed_fp,
                is_managed=managed_fp is not None,
                raw_entry=current,
            )
            desired_entry = MCPDesiredEntry(
                target=config_target,
                fingerprint=desired_fp,
                contains_secret=spec.contains_secret,
                missing_credential_env=spec.missing_credential_env,
                live_command=spec.live_command,
                auth_command=spec.auth_command,
                raw_desired=spec.desired,
            )

            if spec.missing_credential_env:
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=f"Requires missing environment variable: {spec.missing_credential_env}",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            if _entry_matches_desired(spec, current):
                op = MCPOperation(
                    target=config_target,
                    action="NOOP",
                    reason="Already synchronized",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    requires_force=False,
                    force_identity=target_key,
                    is_authorized=True,
                    state_transition=(spec.state_key, desired_fp),
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            safe_to_update = (
                current is None
                or is_authorized
                or (managed_fp is not None and current_fp == managed_fp)
            )
            if not safe_to_update:
                op = MCPOperation(
                    target=config_target,
                    action="CONFLICT",
                    reason="Existing config was not last written by aikito; review it or rerun with --force",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    requires_force=True,
                    force_identity=target_key,
                    is_authorized=False,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            action = "CREATE" if current is None else "UPDATE"
            reason = "New server entry" if current is None else "Configuration updated"
            requires_force_flag = current is not None and (
                managed_fp is None or managed_fp != current_fp
            )
            op = MCPOperation(
                target=config_target,
                action=action,
                reason=reason,
                observed=observed,
                desired=desired_entry,
                spec=spec,
                requires_force=requires_force_flag,
                force_identity=target_key,
                is_authorized=True,
                state_transition=(spec.state_key, desired_fp),
            )
            group_ops.append(op)
            all_operations.append(op)

            try:
                current_text = _update_entry(spec, current_text)
            except Exception as exc:
                err_op = MCPOperation(
                    target=config_target,
                    action="ERROR",
                    reason=f"Failed to update entry: {exc}",
                    spec=spec,
                    is_authorized=False,
                )
                group_ops[-1] = err_op
                all_operations[-1] = err_op

        file_mutating = current_text != orig_text
        file_plan = MCPFilePlan(
            path=canonical_path,
            physical_identity=phys_id,
            format=resolved_format,
            sensitive=file_sensitive,
            pre_image=file_snapshot,
            operations=tuple(group_ops),
            orig_content=orig_text if file_existed else None,
            final_content=current_text if file_mutating else orig_text,
        )
        file_plans.append(file_plan)

    all_operations.extend(unsupported_operations)
    return MCPPlan(
        operations=tuple(all_operations),
        file_plans=tuple(file_plans),
        state_snapshot_hash=state_snapshot_hash,
        specs=tuple(raw_specs),
    )


def _map_operation_to_status(op: MCPOperation) -> str:
    """Map pure MCPOperation action to user-facing inspection status."""
    if op.action == "NOOP":
        return "OK"
    elif op.action == "CREATE":
        return "MISSING"
    elif op.action == "UPDATE":
        return "UPDATE"
    elif op.action == "CONFLICT":
        return "DRIFT"
    elif op.action == "SKIP":
        if op.spec and op.spec.missing_credential_env:
            if op.observed and op.observed.exists and op.observed.raw_entry is not None:
                if _entry_matches_desired(op.spec, op.observed.raw_entry):
                    return "OK"
                return "DRIFT"
        return "SKIP"
    elif op.action == "ERROR":
        return "ERROR"
    return op.action


def evaluate_spec_status(
    spec: AgentSpec,
    state: dict[str, Any] | None = None,
    home: Path | None = None,
    plan: MCPPlan | None = None,
) -> str:
    """Evaluates synchronization status for a single AgentSpec via pure MCP planning.

    Guarantees status, diff, and Doctor share the exact same decision engine as sync.
    Returns one of: 'OK', 'MISSING', 'UPDATE', 'DRIFT', 'ERROR', 'SKIP'.
    """
    if plan is not None:
        for op in plan.operations:
            if (
                op.target.agent == spec.agent
                and op.target.logical_identity == spec.server
            ):
                return _map_operation_to_status(op)

    effective_home = home or spec.home
    if effective_home is None:
        effective_home = Path.home()

    try:
        single_plan = build_mcp_plan(
            aikito_dir=effective_home,
            home=effective_home,
            specs=[spec],
        )
        if single_plan.operations:
            return _map_operation_to_status(single_plan.operations[0])
        return "SKIP"
    except Exception:
        return "ERROR"
