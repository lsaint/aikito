"""
Status aggregation module for aikito.
Gathers synchronization status data across agents, memory, instructions, skills, MCP, and subagents.
"""

import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from .agents import AgentRegistry, load_agent_definitions
from .global_skills import build_global_skill_batch, plan_global_skills
from .instructions import (
    build_global_instruction_batch,
    plan_instructions,
)
from .mcp import (
    build_mcp_plan,
    evaluate_spec_status,
    load_agent_specs,
    probe_mcp_tools_for_specs,
    read_all_entries,
    read_entry,
    redact_mcp_entry,
)
from .memory import extract_note_title
from .memory_runtime import build_project_memory_batch, plan_project_memory
from .project import (
    collect_project_summaries,
    get_instructions_line_count_display,
    resolve_project_binding,
)
from .render import (
    AgentStatusRow,
    GlobalSummary,
    MCPServerRow,
    MemoryNoteRow,
    MemoryStatusRow,
    OrphanSubagentFile,
    SkillRow,
    StatusReportData,
    SubagentRow,
    get_consumer_display_name,
)
from .subagent import SubagentConfigError, build_subagent_plan


@dataclass(frozen=True)
class MCPDetailRow:
    server_name: str
    target_name: str
    agent_name: str
    agent_display_name: str
    source: str
    status: str
    config_path: Path
    config_format: str
    entry: dict[str, Any] | None


@dataclass(frozen=True)
class MCPRuntimeRow:
    agent_name: str
    agent_display_name: str
    connect_status: str
    auth_method: str
    tool_names: tuple[str, ...]
    error: str = ""


@dataclass(frozen=True)
class SubagentDetailRow:
    subagent_name: str
    description: str
    agent_name: str
    agent_display_name: str
    status: str
    target_path: Path
    config_format: str
    platform_options: dict[str, Any]
    canonical_path: Path


def _resolve_name(target: str, names: list[str], resource: str) -> str:
    if target in names:
        return target
    matches = [name for name in names if name.startswith(target)]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise ValueError(
            f"Unknown {resource} '{target}'; available: {', '.join(names)}"
        )
    raise ValueError(f"Ambiguous {resource} '{target}'; matches: {', '.join(matches)}")


def collect_mcp_details(
    aikito_dir: Path,
    home: Path,
    server_target: str | None = None,
    agent_target: str | None = None,
) -> list[MCPDetailRow]:
    agents = load_agent_definitions(aikito_dir, home)
    try:
        specs = load_agent_specs(aikito_dir, home)
        mcp_plan = build_mcp_plan(aikito_dir, home=home, specs=specs)
    except Exception:
        specs = []
        mcp_plan = None
    server_names = sorted({spec.server for spec in specs if spec.enabled})
    server_name = (
        _resolve_name(server_target, server_names, "MCP server")
        if server_target
        else None
    )
    agent_name = (
        _resolve_name(agent_target, sorted(agents), "agent") if agent_target else None
    )

    rows: list[MCPDetailRow] = []
    managed_targets: dict[str, set[str]] = {}
    for spec in specs:
        if not spec.enabled or (server_name and spec.server != server_name):
            continue
        if agent_name and spec.agent != agent_name:
            continue
        definition = agents[spec.agent]
        current = None
        if spec.config_path.is_file():
            current = read_entry(spec, spec.config_path.read_text(encoding="utf-8"))
        rows.append(
            MCPDetailRow(
                server_name=spec.server,
                target_name=spec.target_name,
                agent_name=spec.agent,
                agent_display_name=definition.display_name,
                source="managed",
                status=evaluate_spec_status(spec, home=home, plan=mcp_plan),
                config_path=spec.config_path,
                config_format=spec.config_format,
                entry=redact_mcp_entry(current) if current is not None else None,
            )
        )
        managed_targets.setdefault(spec.agent, set()).add(spec.target_name)

    if agent_name and not server_name:
        definition = agents[agent_name]
        path = definition.mcp_config_path
        if path and path.is_file():
            entries = read_all_entries(
                definition.mcp_config_format, path.read_text(encoding="utf-8")
            )
            for target_name in sorted(
                entries.keys() - managed_targets.get(agent_name, set())
            ):
                rows.append(
                    MCPDetailRow(
                        server_name=target_name,
                        target_name=target_name,
                        agent_name=agent_name,
                        agent_display_name=definition.display_name,
                        source="unmanaged",
                        status="PRESENT",
                        config_path=path,
                        config_format=definition.mcp_config_format,
                        entry=None,
                    )
                )
    return rows


def collect_mcp_runtime(
    aikito_dir: Path,
    home: Path,
    server_target: str,
    agent_target: str | None = None,
) -> tuple[str, list[MCPRuntimeRow]]:
    """Probe one managed MCP server through each selected Agent-native config."""
    agents = load_agent_definitions(aikito_dir, home)
    specs = load_agent_specs(aikito_dir, home)
    server_names = sorted({spec.server for spec in specs if spec.enabled})
    server_name = _resolve_name(server_target, server_names, "MCP server")
    agent_name = (
        _resolve_name(agent_target, sorted(agents), "agent") if agent_target else None
    )
    selected_specs = [
        spec
        for spec in specs
        if spec.enabled
        and spec.server == server_name
        and (agent_name is None or spec.agent == agent_name)
    ]
    if not selected_specs:
        selection = f"{server_name}/{agent_name}" if agent_name else server_name
        raise ValueError(f"No MCP configuration found for '{selection}'")

    results = probe_mcp_tools_for_specs(selected_specs)
    rows = [
        MCPRuntimeRow(
            agent_name=result.agent,
            agent_display_name=agents[result.agent].display_name,
            connect_status=result.status,
            auth_method=result.auth_method,
            tool_names=result.tool_names,
            error=result.error,
        )
        for result in results
    ]
    return server_name, rows


def collect_subagent_details(
    aikito_dir: Path,
    home: Path,
    subagent_target: str | None = None,
    agent_target: str | None = None,
) -> list[SubagentDetailRow]:
    from .subagent import (
        FORMAT_EXTENSIONS,
        load_all_agents,
        load_subagent_definitions,
    )

    agent_configs, all_agent_names = load_all_agents(aikito_dir, home)
    subagent_defs = load_subagent_definitions(aikito_dir, allow_empty=True)
    try:
        subagent_plan = build_subagent_plan(aikito_dir, home, allow_empty=True)
        plan_ops = subagent_plan.operations
    except SubagentConfigError:
        plan_ops = ()

    subagent_names = sorted(subagent_defs.keys())
    subagent_name = (
        _resolve_name(subagent_target, subagent_names, "subagent")
        if subagent_target
        else None
    )
    agent_name = (
        _resolve_name(agent_target, sorted(agent_configs.keys()), "agent")
        if agent_target
        else None
    )

    plan_map: dict[tuple[str, str], Any] = {
        (op.target.logical_identity, op.target.agent): op for op in plan_ops
    }

    rows: list[SubagentDetailRow] = []

    for name in subagent_names:
        if subagent_name and name != subagent_name:
            continue
        sub_def = subagent_defs[name]

        for ag_key, ag_cfg in sorted(agent_configs.items()):
            if agent_name and ag_key != agent_name:
                continue

            if ag_key not in sub_def.agents and not (subagent_name and agent_name):
                continue

            plan_op = plan_map.get((name, ag_key))
            if ag_key not in sub_def.agents:
                status = "NOT_TARGETED"
                ext = FORMAT_EXTENSIONS.get(ag_cfg.config_format, ".md")
                target_path = ag_cfg.config_path / f"{name}{ext}"
            elif plan_op:
                if plan_op.action in ("OK", "NOOP"):
                    status = "OK"
                elif plan_op.action in ("UPDATE", "FORCE UPDATE"):
                    status = "DRIFT"
                elif plan_op.action == "CREATE":
                    status = "MISSING"
                elif plan_op.action == "CONFLICT":
                    status = "CONFLICT"
                elif plan_op.action == "SKIP":
                    status = "SKIP"
                else:
                    status = plan_op.action
                target_path = plan_op.target.path
            else:
                status = "MISSING"
                ext = FORMAT_EXTENSIONS.get(ag_cfg.config_format, ".md")
                target_path = ag_cfg.config_path / f"{name}{ext}"

            platform_opts = sub_def.platform_configs.get(ag_key, {})
            canonical_path = aikito_dir / "subagents" / f"{name}.md"

            rows.append(
                SubagentDetailRow(
                    subagent_name=name,
                    description=sub_def.description,
                    agent_name=ag_key,
                    agent_display_name=ag_cfg.display_name,
                    status=status,
                    target_path=target_path,
                    config_format=ag_cfg.config_format,
                    platform_options=platform_opts,
                    canonical_path=canonical_path,
                )
            )

    return rows


def _get_skills_list(aikito_dir: Path) -> list[str]:
    skills_toml_path = aikito_dir / "skills.toml"
    if not skills_toml_path.exists():
        return []
    try:
        with open(skills_toml_path, "rb") as f:
            data = tomllib.load(f)
        skills = data.get("skills", [])
        if isinstance(skills, list):
            return [str(s) for s in skills]
    except (tomllib.TOMLDecodeError, OSError) as exc:
        print(
            f"[WARN] Failed to read global skills configuration: {exc}",
            file=sys.stderr,
        )
    return []


def _summarize_subagent_status(actions: list[str]) -> str:
    total = len(actions)
    ok_count = actions.count("OK")
    if ok_count == total:
        return f"OK ({total})"
    if "ERROR" in actions:
        return f"ERROR ({ok_count}/{total})"
    if "CONFLICT" in actions:
        return f"CONFLICT ({ok_count}/{total})"
    if any(action in ("UPDATE", "FORCE UPDATE") for action in actions):
        return f"DRIFT ({ok_count}/{total})"
    if "CREATE" in actions:
        return f"MISSING ({ok_count}/{total})"
    return f"CONFLICT ({ok_count}/{total})"


def collect_agent_status_rows(
    aikito_dir: Path, home: Path
) -> tuple[list[AgentStatusRow], int, int, int]:
    agents_dict = load_agent_definitions(aikito_dir, home)
    instruction_batch = build_global_instruction_batch(
        aikito_dir, home, registry=AgentRegistry(agents_dict)
    )
    instruction_plan = plan_instructions(instruction_batch, home)

    instruction_target_status: dict[Path, str] = {}
    for op in instruction_plan.operations:
        if op.action in ("NOOP", "SHARED_PATH"):
            st = "OK"
        elif op.action == "CREATE":
            st = "MISSING"
        elif op.action == "CONFLICT":
            st = "CONFLICT"
        elif op.action == "SKIP":
            st = "SKIP"
        elif op.action == "UNLINK":
            st = "DRIFT"
        else:
            st = op.action
        instruction_target_status[op.target_path] = st

    global_skills = _get_skills_list(aikito_dir)
    total_global_skills = len(global_skills)

    global_skill_batch = build_global_skill_batch(
        aikito_dir,
        home,
        skills=global_skills,
        registry=AgentRegistry(agents_dict),
        container_path=home / ".agents" / "skills",
    )
    global_skill_plan = plan_global_skills(global_skill_batch, home, dry_run=True)

    agent_issues = 0

    # Pre-fetch MCP specs and Subagent plan items
    try:
        mcp_specs = load_agent_specs(aikito_dir, home)
        mcp_plan = build_mcp_plan(aikito_dir, home=home, specs=mcp_specs)
    except Exception:
        mcp_specs = []
        mcp_plan = None
        agent_issues += 1

    try:
        subagent_plan = build_subagent_plan(aikito_dir, home, allow_empty=True)
        subagent_ops = subagent_plan.operations
        subagent_configs = dict(subagent_plan.agent_configs)
    except SubagentConfigError:
        subagent_plan = None
        subagent_ops = ()
        subagent_configs = {}
        agent_issues += 1

    # Unique enabled MCP servers
    enabled_mcp_servers = set(spec.server for spec in mcp_specs if spec.enabled)
    total_mcp_count = len(enabled_mcp_servers)

    # Unique active subagents
    active_subagents = set(
        op.target.logical_identity for op in subagent_ops if op.action != "SKIP"
    )
    total_subagents_count = len(active_subagents)

    rows: list[AgentStatusRow] = []

    for name, definition in agents_dict.items():
        # 1. Instructions Status
        instructions_status = "SKIP"
        if definition.instruction_path is not None:
            instructions_status = instruction_target_status.get(
                definition.instruction_path, "SKIP"
            )
            if instructions_status not in ("OK", "SKIP"):
                agent_issues += 1

        # 2. Skills Status
        skills_status = "SKIP"
        if definition.skills_path is not None:
            consumer_op = next(
                (
                    op
                    for op in global_skill_plan.consumer_ops
                    if op.target_path == definition.skills_path
                ),
                None,
            )
            if consumer_op is None or consumer_op.action == "SKIP":
                skills_status = "SKIP"
            elif consumer_op.action == "CREATE":
                skills_status = "MISSING"
                agent_issues += 1
            elif consumer_op.action == "CONFLICT":
                skills_status = (
                    f"CONFLICT (0/{total_global_skills})"
                    if total_global_skills > 0
                    else "CONFLICT"
                )
                agent_issues += 1
            else:
                # Consumer link is OK (NOOP or SHARED_PATH). Now check container & managed entries.
                if global_skill_plan.container_op.action == "CONFLICT":
                    skills_status = (
                        f"CONFLICT (0/{total_global_skills})"
                        if total_global_skills > 0
                        else "CONFLICT"
                    )
                    agent_issues += 1
                elif global_skill_plan.container_op.action == "CREATE":
                    skills_status = "MISSING"
                    agent_issues += 1
                else:
                    ok_skills = sum(
                        1
                        for op in global_skill_plan.entry_ops
                        if op.desired_representation == "link" and op.action == "NOOP"
                    )
                    if ok_skills == total_global_skills and total_global_skills > 0:
                        skills_status = f"OK ({total_global_skills})"
                    elif total_global_skills > 0:
                        skills_status = f"CONFLICT ({ok_skills}/{total_global_skills})"
                        agent_issues += 1
                    else:
                        skills_status = "OK (0)"

        # 3. MCP Status
        mcp_status = "SKIP"
        agent_mcp_specs = [s for s in mcp_specs if s.agent == name]
        if definition.mcp_config_format != "unsupported":
            if not agent_mcp_specs:
                mcp_status = "OK (0)"
            else:
                total_mcp = len(agent_mcp_specs)
                ok_mcp = 0
                skip_mcp = 0
                has_drift = False
                has_missing = False
                has_error = False

                for spec in agent_mcp_specs:
                    st = evaluate_spec_status(spec, home=home, plan=mcp_plan)
                    if st == "OK":
                        ok_mcp += 1
                    elif st == "SKIP":
                        skip_mcp += 1
                    elif st in ("DRIFT", "UPDATE"):
                        has_drift = True
                    elif st == "MISSING":
                        has_missing = True
                    elif st == "ERROR":
                        has_error = True

                if skip_mcp == total_mcp:
                    mcp_status = "SKIP"
                elif ok_mcp + skip_mcp == total_mcp:
                    mcp_status = f"OK ({ok_mcp})"
                elif has_error:
                    mcp_status = f"ERROR ({ok_mcp}/{total_mcp})"
                    agent_issues += 1
                elif has_drift:
                    mcp_status = f"DRIFT ({ok_mcp}/{total_mcp})"
                    agent_issues += 1
                elif has_missing:
                    mcp_status = f"MISSING ({ok_mcp}/{total_mcp})"
                    agent_issues += 1
                else:
                    mcp_status = f"CONFLICT ({ok_mcp}/{total_mcp})"
                    agent_issues += 1

        # 4. Subagent Status
        subagent_status = "SKIP"
        agent_subagent_ops = [
            op
            for op in subagent_ops
            if op.target.agent in (name, definition.display_name)
        ]
        active_ops = [op for op in agent_subagent_ops if op.action != "SKIP"]
        if active_ops:
            subagent_status = _summarize_subagent_status(
                ["OK" if op.action == "NOOP" else op.action for op in active_ops]
            )
            if not subagent_status.startswith("OK"):
                agent_issues += 1
        elif name in subagent_configs:
            subagent_status = "OK (0)"

        shared_skills_path = home / ".agents" / "skills"
        if definition.skills_path is None:
            skills_link_depth = None
        elif definition.skills_path == shared_skills_path:
            skills_link_depth = 1
        else:
            skills_link_depth = 2

        rows.append(
            AgentStatusRow(
                agent_name=name,
                display_name=definition.display_name,
                instructions_status=instructions_status,
                skills_status=skills_status,
                skills_link_depth=skills_link_depth,
                mcp_status=mcp_status,
                subagent_status=subagent_status,
            )
        )

    return rows, agent_issues, total_subagents_count, total_mcp_count


def collect_memory_status_rows(
    aikito_dir: Path, home: Path
) -> tuple[list[MemoryStatusRow], int, int]:
    rows: list[MemoryStatusRow] = []
    total_notes = 0
    mem_issues = 0

    def latest_memory_update(memory_dir: Path) -> date | None:
        notes_dir = memory_dir / "notes"
        existing = list(notes_dir.glob("*.md")) if notes_dir.is_dir() else []
        if not existing:
            return None
        return date.fromtimestamp(max(path.stat().st_mtime for path in existing))

    # Global Memory: Global memory has no ~/.agents/memory symlink requirement.
    global_mem_dir = aikito_dir / "memory"
    notes_dir = global_mem_dir / "notes"
    global_status = "OK" if notes_dir.is_dir() else "MISSING"
    if global_status != "OK":
        mem_issues += 1

    global_notes_count = len(list(notes_dir.glob("*.md"))) if notes_dir.is_dir() else 0
    total_notes += global_notes_count

    rows.append(
        MemoryStatusRow(
            name="Global",
            scope="Global",
            status=global_status,
            notes_count=global_notes_count,
            updated_on=latest_memory_update(global_mem_dir),
        )
    )

    # Project Memory (scan <workspace>/projects/<project_name>)
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if proj_folder.is_dir():
                proj_mem = proj_folder / "memory"
                proj_notes = proj_mem / "notes"
                canonical_status = "OK" if proj_notes.is_dir() else "MISSING"
                if canonical_status != "OK":
                    mem_issues += 1

                proj_notes_count = (
                    len(list(proj_notes.glob("*.md"))) if proj_notes.is_dir() else 0
                )
                total_notes += proj_notes_count

                # Read local codebase path from projects/<name>/agent.toml
                agent_toml = proj_folder / "agent.toml"
                p_link_status = "N/A"
                if agent_toml.is_file():
                    try:
                        with open(agent_toml, "rb") as f:
                            toml_data = tomllib.load(f)
                        binding = resolve_project_binding(toml_data, Path.home())
                    except (tomllib.TOMLDecodeError, OSError) as exc:
                        print(
                            f"[WARN] Failed to read configuration for project '{proj_folder.name}': {exc}",
                            file=sys.stderr,
                        )
                        binding = None

                    if binding and binding.active_entries:
                        active_statuses = []
                        for entry in binding.active_entries:
                            mem_batch = build_project_memory_batch(
                                aikito_dir,
                                proj_folder.name,
                                toml_data,
                                active_checkouts=[entry.resolved_path],
                            )
                            mem_plan = plan_project_memory(mem_batch)
                            entry_statuses = []
                            for op in mem_plan.operations:
                                if op.action in ("NOOP", "SHARED_PATH"):
                                    entry_statuses.append("OK")
                                elif op.action == "CREATE":
                                    entry_statuses.append("MISSING")
                                elif op.action in ("UNLINK", "CONFLICT"):
                                    entry_statuses.append("CONFLICT")
                            if "CONFLICT" in entry_statuses:
                                active_statuses.append("CONFLICT")
                            elif "MISSING" in entry_statuses:
                                active_statuses.append("MISSING")
                            else:
                                active_statuses.append("OK")
                        if "CONFLICT" in active_statuses:
                            p_link_status = "CONFLICT"
                            mem_issues += 1
                        elif "MISSING" in active_statuses:
                            p_link_status = "MISSING"
                            mem_issues += 1
                        else:
                            p_link_status = "OK"
                    elif binding and binding.offline_entries:
                        p_link_status = "OFFLINE"
                else:
                    p_link_status = "N/A"

                rows.append(
                    MemoryStatusRow(
                        name=proj_folder.name,
                        scope="Project",
                        status=(
                            canonical_status
                            if canonical_status != "OK"
                            else p_link_status
                        ),
                        notes_count=proj_notes_count,
                        updated_on=latest_memory_update(proj_mem),
                    )
                )

    return rows, total_notes, mem_issues


def get_status_report_data(aikito_dir: Path, home: Path) -> StatusReportData:
    agent_rows, agent_issues, total_subagents, total_mcp = collect_agent_status_rows(
        aikito_dir, home
    )
    memory_rows, total_memory_notes, mem_issues = collect_memory_status_rows(
        aikito_dir, home
    )
    skills_list = _get_skills_list(aikito_dir)
    total_skills_count = len(skills_list)
    total_issues = agent_issues + mem_issues

    # Global scope row
    global_instr = get_instructions_line_count_display(aikito_dir / "AGENTS.md")
    global_notes_count = (
        memory_rows[0].notes_count
        if memory_rows and memory_rows[0].scope.lower() == "global"
        else 0
    )
    global_mem_status = (
        memory_rows[0].status
        if memory_rows and memory_rows[0].scope.lower() == "global"
        else "OK"
    )
    global_mem_issues = (
        1 if global_mem_status not in ("OK", "EMPTY", "N/A", "SKIP") else 0
    )
    global_issues = agent_issues + global_mem_issues
    if global_issues == 0:
        global_status = "OK"
    else:
        issue_label = "issue" if global_issues == 1 else "issues"
        global_status = f"! {global_issues} {issue_label}"

    global_mcp_str = str(total_mcp) if total_mcp > 0 else "-"
    global_sub_str = str(total_subagents) if total_subagents > 0 else "-"

    global_summary = GlobalSummary(
        status=global_status,
        instr=global_instr,
        skills_count=total_skills_count,
        memory_notes_count=global_notes_count,
        mcp_count=global_mcp_str,
        subagent_count=global_sub_str,
    )

    projects = collect_project_summaries(aikito_dir, home)

    consumer_names = sorted(
        dict.fromkeys(get_consumer_display_name(a.agent_name) for a in agent_rows)
    )

    return StatusReportData(
        agents=agent_rows,
        memories=memory_rows,
        total_subagents_count=total_subagents,
        total_mcp_count=total_mcp,
        total_skills_count=total_skills_count,
        total_memory_notes=total_memory_notes,
        issues_count=total_issues,
        consumers=consumer_names,
        projects=projects,
        global_summary=global_summary,
    )


def collect_mcp_matrix(
    aikito_dir: Path, home: Path, live: bool = False
) -> tuple[list[MCPServerRow], list[str]]:
    agents_dict = load_agent_definitions(aikito_dir, home)
    try:
        specs = load_agent_specs(aikito_dir, home)
        mcp_plan = build_mcp_plan(aikito_dir, home=home, specs=specs)
    except Exception:
        specs = []
        mcp_plan = None
    agent_names = [a.display_name for a in agents_dict.values()]
    agent_key_to_display = {k: v.display_name for k, v in agents_dict.items()}

    servers: dict[str, dict[str, str]] = {}
    for spec in specs:
        srv_name = spec.server
        ag_display = agent_key_to_display.get(spec.agent, spec.agent)

        if srv_name not in servers:
            servers[srv_name] = {}

        if spec.agent in agents_dict:
            st = evaluate_spec_status(spec, home=home, plan=mcp_plan)
        else:
            st = "SKIP"

        servers[srv_name][ag_display] = st

    if live:
        live_specs = [
            spec
            for spec in specs
            if spec.enabled
            and spec.agent in agents_dict
            and servers.get(spec.server, {}).get(
                agent_key_to_display.get(spec.agent, spec.agent)
            )
            == "OK"
        ]
        probe_results = probe_mcp_tools_for_specs(live_specs)
        for spec, result in zip(live_specs, probe_results):
            display_name = agent_key_to_display.get(spec.agent, spec.agent)
            if result.status == "OK":
                servers[spec.server][display_name] = f"OK ({len(result.tool_names)})"
            elif result.status == "ERROR":
                servers[spec.server][display_name] = "ERROR"

    for srv_name, st_dict in servers.items():
        for ag_display in agent_names:
            if ag_display not in st_dict:
                st_dict[ag_display] = "SKIP"

    rows = []
    for srv_name in sorted(servers.keys()):
        rows.append(
            MCPServerRow(server_name=srv_name, agent_statuses=servers[srv_name])
        )

    return rows, agent_names


def collect_subagents_matrix(
    aikito_dir: Path, home: Path
) -> tuple[list[SubagentRow], list[OrphanSubagentFile], list[str]]:
    try:
        subagent_plan = build_subagent_plan(
            aikito_dir=aikito_dir, home=home, allow_empty=True
        )
        plan_ops = subagent_plan.operations
    except SubagentConfigError:
        plan_ops = ()
    agents_dict = load_agent_definitions(aikito_dir, home)
    agent_names = [a.display_name for a in agents_dict.values()]

    subagents_map: dict[str, dict[str, str]] = {}
    orphan_files: list[OrphanSubagentFile] = []

    for op in plan_ops:
        ag_def = agents_dict.get(op.target.agent)
        ag_display = ag_def.display_name if ag_def else op.target.agent

        if op.action in ("ORPHAN", "REMOVE"):
            rel_path = str(op.target.path)
            try:
                rel_path = f"~/{op.target.path.relative_to(home)}"
            except ValueError:
                pass
            orphan_files.append(
                OrphanSubagentFile(agent_display_name=ag_display, file_path=rel_path)
            )
            continue

        if op.target.logical_identity == "*":
            continue

        sub_name = op.target.logical_identity
        if sub_name not in subagents_map:
            subagents_map[sub_name] = {}

        if op.action in ("CREATE", "UPDATE"):
            subagents_map[sub_name][ag_display] = "MISSING"
        elif op.action == "CONFLICT":
            subagents_map[sub_name][ag_display] = "CONFLICT"
        elif op.action in ("OK", "NOOP"):
            subagents_map[sub_name][ag_display] = "OK"
        elif op.action == "SKIP":
            subagents_map[sub_name][ag_display] = "SKIP"

    for sub_name, st_dict in subagents_map.items():
        for ag_def in agents_dict.values():
            if ag_def.display_name not in st_dict:
                st_dict[ag_def.display_name] = "SKIP"

    subagent_rows = []
    for sub_name in sorted(subagents_map.keys()):
        subagent_rows.append(
            SubagentRow(subagent_name=sub_name, agent_statuses=subagents_map[sub_name])
        )

    return subagent_rows, orphan_files, agent_names


def collect_memory_notes_rows(
    aikito_dir: Path,
    home: Path,
    project: str | None = None,
    *,
    include_global: bool = False,
) -> list[MemoryNoteRow]:
    rows = []

    # 1. Global Memory Notes
    if project is None or project.lower() == "global" or include_global:
        global_notes_dir = aikito_dir / "memory" / "notes"
        if global_notes_dir.is_dir():
            for note_file in sorted(global_notes_dir.glob("*.md")):
                try:
                    updated_on = date.fromtimestamp(note_file.stat().st_mtime)
                except OSError:
                    updated_on = None
                rows.append(
                    MemoryNoteRow(
                        scope_name="Global",
                        note_name=note_file.stem,
                        title=extract_note_title(note_file),
                        link_status="SKIP",
                        updated_on=updated_on,
                    )
                )

    # 2. Project Memory Notes
    if project is None or project.lower() != "global":
        projects_dir = aikito_dir / "projects"
        if projects_dir.is_dir():
            for proj_folder in sorted(projects_dir.iterdir()):
                if proj_folder.is_dir():
                    if project is not None and proj_folder.name != project:
                        continue
                    proj_mem = proj_folder / "memory"
                    proj_notes = proj_mem / "notes"

                    agent_toml = proj_folder / "agent.toml"
                    link_st = "MISSING"
                    if agent_toml.is_file():
                        try:
                            with open(agent_toml, "rb") as f:
                                data = tomllib.load(f)
                            binding = resolve_project_binding(data, home)
                            if binding.active_entries:
                                if all(
                                    (e.resolved_path / ".agents" / "memory").exists()
                                    for e in binding.active_entries
                                ):
                                    link_st = "OK"
                                else:
                                    link_st = "MISSING"
                            elif binding.entries:
                                link_st = "OFFLINE"
                        except Exception:
                            pass

                    if proj_notes.is_dir():
                        for p_note in sorted(proj_notes.glob("*.md")):
                            try:
                                updated_on = date.fromtimestamp(p_note.stat().st_mtime)
                            except OSError:
                                updated_on = None
                            rows.append(
                                MemoryNoteRow(
                                    scope_name=proj_folder.name,
                                    note_name=p_note.stem,
                                    title=extract_note_title(p_note),
                                    link_status=link_st,
                                    updated_on=updated_on,
                                )
                            )

    return rows


def _parse_skill_description(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return "-"
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
        lines = content.lstrip("\ufeff").splitlines()
        if (
            lines
            and lines[0].rstrip() == "---"
            and not lines[0].startswith((" ", "\t"))
        ):
            for line in lines[1:]:
                line_stripped = line.rstrip()
                if line_stripped == "---" and not line.startswith((" ", "\t")):
                    break
                if line.strip().startswith("description:"):
                    desc = line.strip().split("description:", 1)[1].strip().strip("\"'")
                    return desc
    except Exception:
        pass
    return "-"


def collect_skills_rows(aikito_dir: Path) -> list[SkillRow]:
    global_skills = set(_get_skills_list(aikito_dir))

    project_skills: dict[str, set[str]] = {}
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if proj_folder.is_dir():
                agent_toml = proj_folder / "agent.toml"
                if agent_toml.is_file():
                    try:
                        with open(agent_toml, "rb") as f:
                            data = tomllib.load(f)
                            p_skills = data.get("skills", [])
                            if isinstance(p_skills, list):
                                project_skills[proj_folder.name] = set(
                                    str(s) for s in p_skills
                                )
                    except (tomllib.TOMLDecodeError, OSError) as exc:
                        print(
                            f"[WARN] Failed to read configuration for project '{proj_folder.name}': {exc}",
                            file=sys.stderr,
                        )

    skills_dir = aikito_dir / "skills"
    disk_skills = set()
    if skills_dir.is_dir():
        for item in skills_dir.iterdir():
            if item.is_dir():
                disk_skills.add(item.name)

    all_skill_names = set(global_skills) | disk_skills
    for p_skills in project_skills.values():
        all_skill_names |= p_skills

    rows: list[SkillRow] = []

    for name in sorted(all_skill_names):
        is_global = name in global_skills

        proj_matches = [
            p_name
            for p_name, p_skills in sorted(project_skills.items())
            if name in p_skills
        ]

        if is_global:
            scope = "Global"
        elif proj_matches:
            scope = ", ".join(proj_matches)
        else:
            scope = "Orphan"

        s_dir = skills_dir / name
        source_status = "OK" if s_dir.is_dir() else "MISSING"

        desc = _parse_skill_description(s_dir) if source_status == "OK" else "-"

        rows.append(
            SkillRow(
                skill_name=name,
                scope=scope,
                source_status=source_status,
                description=desc,
            )
        )

    # Sort rows by scope priority (Global -> Projects -> Orphan), then by skill_name
    def scope_sort_key(r: SkillRow):
        if r.scope == "Global":
            return (0, "", r.skill_name)
        elif r.scope == "Orphan":
            return (2, "", r.skill_name)
        else:
            return (1, r.scope, r.skill_name)

    rows.sort(key=scope_sort_key)
    return rows
