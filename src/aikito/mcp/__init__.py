"""Synchronize canonical Aikito MCP definitions into supported agent configs."""

from pathlib import Path
from typing import Callable

from .adapters import (
    get_agy_json_server,
    get_claude_json_server,
    get_copilot_json_server,
    get_dsh_cordis_server,
    get_jsonc_server,
    get_toml_server,
    parse_jsonc,
    read_all_entries,
    read_entry,
    remove_claude_json_server,
    remove_dsh_cordis_server,
    remove_jsonc_server,
    remove_toml_server,
    update_agy_json_server,
    update_claude_json_server,
    update_copilot_json_server,
    update_dsh_cordis_server,
    update_jsonc_server,
    update_toml_server,
)
from .auth import (
    authenticate_mcp,
    describe_mcp_auth,
)
from .executor import (
    _load_state,
    execute_mcp_plan,
)
from .loader import (
    load_agent_specs,
)
from .model import (
    BACKUP_DIR,
    BROWSER_HELPER,
    DEFAULT_AGENTS_CONFIG,
    DEFAULT_MCPS_DIR,
    LEGACY_PLACEHOLDER_TOKEN,
    STATE_FILE,
    STATE_VERSION,
    AgentSpec,
    BasicTokenAuth,
    LiveMCPResult,
    MCPConfigError,
    MCPConfigTarget,
    MCPDesiredEntry,
    MCPExecutionResult,
    MCPFilePlan,
    MCPObservedEntry,
    MCPOperation,
    MCPPlan,
    MCPToolProbeResult,
    Token,
)
from .planner import (
    build_mcp_plan,
    evaluate_spec_status,
    mcp_operation_effect,
    mcp_operation_finding,
    observe_mcp_operation,
)
from .probe import (
    probe_mcp_tools,
    probe_mcp_tools_for_specs,
    run_live_mcp_commands,
)
from .redact import (
    SENSITIVE_URL_PARAMETERS,
    URL_PATTERN,
    environment_reference,
    is_credential_header,
    is_sensitive_url_parameter,
    redact_mcp_entry,
)

__all__ = [
    # Models & Exceptions
    "AgentSpec",
    "BasicTokenAuth",
    "LiveMCPResult",
    "MCPConfigError",
    "MCPConfigTarget",
    "MCPDesiredEntry",
    "MCPExecutionResult",
    "MCPFilePlan",
    "MCPObservedEntry",
    "MCPOperation",
    "MCPPlan",
    "MCPToolProbeResult",
    "Token",
    # Constants
    "BACKUP_DIR",
    "BROWSER_HELPER",
    "DEFAULT_AGENTS_CONFIG",
    "DEFAULT_MCPS_DIR",
    "LEGACY_PLACEHOLDER_TOKEN",
    "SENSITIVE_URL_PARAMETERS",
    "STATE_FILE",
    "STATE_VERSION",
    "URL_PATTERN",
    # Loader
    "load_agent_specs",
    # Adapters (formats)
    "get_agy_json_server",
    "get_claude_json_server",
    "get_copilot_json_server",
    "get_dsh_cordis_server",
    "get_jsonc_server",
    "get_toml_server",
    "parse_jsonc",
    "read_all_entries",
    "read_entry",
    "remove_claude_json_server",
    "remove_dsh_cordis_server",
    "remove_jsonc_server",
    "remove_toml_server",
    "update_agy_json_server",
    "update_claude_json_server",
    "update_copilot_json_server",
    "update_dsh_cordis_server",
    "update_jsonc_server",
    "update_toml_server",
    # Redaction
    "environment_reference",
    "is_credential_header",
    "is_sensitive_url_parameter",
    "redact_mcp_entry",
    # Planner
    "build_mcp_plan",
    "evaluate_spec_status",
    "mcp_operation_effect",
    "mcp_operation_finding",
    "observe_mcp_operation",
    # Executor & State
    "execute_mcp_plan",
    "sync_mcp_configs",
    "sync_remove_mcp_from_agents",
    # Auth & Probe
    "authenticate_mcp",
    "describe_mcp_auth",
    "probe_mcp_tools",
    "probe_mcp_tools_for_specs",
    "run_live_mcp_commands",
]


def sync_mcp_configs(
    *,
    aikito_dir: Path,
    home: Path,
    dry_run: bool = False,
    force: bool = False,
    plan: MCPPlan | None = None,
    output: Callable[[str], None] = print,
) -> bool:
    if plan is None:
        plan = build_mcp_plan(aikito_dir, home, force=force)

    # Output inspection results
    for op in plan.operations:
        target_key = f"{op.target.agent}/{op.target.logical_identity}"
        if op.action == "SKIP":
            if op.spec and op.spec.missing_credential_env:
                output(
                    f"[WARN] {target_key}: skipped due to missing credential "
                    f"environment variable: {op.spec.missing_credential_env}"
                )
            elif op.spec and not op.spec.enabled:
                output(f"[SKIP] {target_key}: {op.reason}")
            else:
                output(
                    f"[SKIP] {op.target.agent} not detected: {op.target.path.parent}"
                )
        elif op.action == "NOOP":
            output(f"[OK] {target_key}: already synchronized")
        elif op.action == "CONFLICT":
            output(
                f"[CONFLICT] {target_key}: existing config was not "
                "last written by aikito; review it or rerun with --force"
            )

    if dry_run:
        for op in plan.operations:
            if op.action in ("CREATE", "UPDATE") and op.is_authorized:
                action_name = "create" if op.action == "CREATE" else "update"
                output(
                    f"[DRY-RUN] {op.target.agent}/{op.target.logical_identity}: would {action_name} entry"
                )
        if not plan.can_apply:
            return False
        # In dry run, converge state for already OK entries just like legacy sync
        state = _load_state(home)
        entries = state.get("entries", {})
        for op in plan.operations:
            if op.action == "NOOP" and op.state_transition:
                state_key, fp = op.state_transition
                if entries.get(state_key, {}).get("fingerprint") != fp:
                    entries[state_key] = {
                        "fingerprint": fp,
                        "config_path": str(op.target.path),
                        "target_name": op.target.target_name,
                    }
        return plan.can_apply

    if not plan.can_apply:
        return False

    result = execute_mcp_plan(plan, home, output=output)
    return result.success


def sync_remove_mcp_from_agents(
    *,
    specs: list[AgentSpec],
    home: Path,
    output: Callable[[str], None] = print,
    force: bool = False,
) -> bool:
    server_names = {s.server for s in specs}
    plan = build_mcp_plan(
        aikito_dir=home,
        home=home,
        specs=specs,
        desired_absent_servers=server_names,
        force=force,
    )
    if not plan.can_apply:
        for op in plan.operations:
            if op.action == "CONFLICT" and not op.is_authorized:
                output(
                    f"[CONFLICT] {op.target.agent}/{op.target.logical_identity}: existing config was not "
                    "last written by aikito; review it or rerun with --force"
                )
        return False
    result = execute_mcp_plan(plan, home, output=output)
    return result.success
