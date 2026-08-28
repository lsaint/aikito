"""
Status aggregation module for aikito.
Gathers synchronization status data across agents, memory, instructions, skills, MCP, and subagents.
"""

import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from aikito_link import SymlinkVerdict, classify_symlink, symlink_verdict_to_status
from aikito_mcp import (
    load_agents,
    load_agent_specs,
    evaluate_spec_status,
    read_all_entries,
    read_entry,
    redact_mcp_entry,
    run_live_mcp_commands,
)
from aikito_subagent import build_plan
from aikito_render import (
    AgentStatusRow,
    MCPServerRow,
    MemoryNoteRow,
    MemoryStatusRow,
    OrphanSubagentFile,
    SkillRow,
    StatusReportData,
    SubagentRow,
)


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
    agents = load_agents(aikito_dir, home)
    specs = load_agent_specs(aikito_dir, home)
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
                status=evaluate_spec_status(spec),
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


def collect_subagent_details(
    aikito_dir: Path,
    home: Path,
    subagent_target: str | None = None,
    agent_target: str | None = None,
) -> list[SubagentDetailRow]:
    from aikito_subagent import (
        FORMAT_EXTENSIONS,
        load_all_agents,
        load_subagent_definitions,
    )

    agent_configs, all_agent_names = load_all_agents(aikito_dir, home)
    subagent_defs = load_subagent_definitions(aikito_dir, allow_empty=True)
    plan_items, _ = build_plan(aikito_dir, home, allow_empty=True)

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
        (item.subagent_name, item.agent_name): item for item in plan_items
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

            plan_item = plan_map.get((name, ag_key))
            if ag_key not in sub_def.agents:
                status = "NOT_TARGETED"
                ext = FORMAT_EXTENSIONS.get(ag_cfg.config_format, ".md")
                target_path = ag_cfg.config_path / f"{name}{ext}"
            elif plan_item:
                if plan_item.action == "OK":
                    status = "OK"
                elif plan_item.action in ("UPDATE", "FORCE UPDATE"):
                    status = "DRIFT"
                elif plan_item.action == "CREATE":
                    status = "MISSING"
                elif plan_item.action == "CONFLICT":
                    status = "CONFLICT"
                elif plan_item.action == "SKIP":
                    status = "SKIP"
                else:
                    status = plan_item.action
                target_path = plan_item.target_path
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


def _get_skills_list(aikito_dir: Path) -> List[str]:
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
        print(f"[WARN] Failed to read {skills_toml_path}: {exc}", file=sys.stderr)
    return []


def _summarize_subagent_status(actions: List[str]) -> str:
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
) -> tuple[List[AgentStatusRow], int, int, int]:
    agents_dict = load_agents(aikito_dir, home)
    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    global_skills = _get_skills_list(aikito_dir)
    total_global_skills = len(global_skills)

    # Pre-fetch MCP specs and Subagent plan items
    mcp_specs = load_agent_specs(aikito_dir, home)
    subagent_plan, subagent_configs = build_plan(aikito_dir, home, allow_empty=True)

    # Unique enabled MCP servers
    enabled_mcp_servers = set(spec.server for spec in mcp_specs if spec.enabled)
    total_mcp_count = len(enabled_mcp_servers)

    # Unique active subagents
    active_subagents = set(
        item.subagent_name for item in subagent_plan if item.action != "SKIP"
    )
    total_subagents_count = len(active_subagents)

    rows: List[AgentStatusRow] = []
    agent_issues = 0

    for name, definition in agents_dict.items():
        # 1. Instructions Status
        instructions_status = "SKIP"
        if definition.instruction_path is not None:
            target = definition.instruction_path
            if not target.parent.exists():
                instructions_status = "SKIP"
            else:
                verdict = classify_symlink(target, global_instruction_source)
                instructions_status = symlink_verdict_to_status(verdict)
                if instructions_status != "OK":
                    agent_issues += 1

        # 2. Skills Status
        skills_status = "SKIP"
        if definition.skills_path is not None:
            skills_dir = definition.skills_path
            if not skills_dir.parent.exists():
                skills_status = "SKIP"
            elif not skills_dir.exists() and not skills_dir.is_symlink():
                skills_status = "MISSING"
                agent_issues += 1
            else:
                ok_skills = 0
                for skill_name in global_skills:
                    skill_target = skills_dir / skill_name
                    expected_source = aikito_dir / "skills" / skill_name
                    verdict = classify_symlink(skill_target, expected_source)
                    if verdict == SymlinkVerdict.OK:
                        ok_skills += 1
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
                has_drift = False
                has_missing = False
                has_error = False

                for spec in agent_mcp_specs:
                    st = evaluate_spec_status(spec)
                    if st == "OK":
                        ok_mcp += 1
                    elif st == "DRIFT":
                        has_drift = True
                    elif st == "MISSING":
                        has_missing = True
                    elif st == "ERROR":
                        has_error = True

                if ok_mcp == total_mcp:
                    mcp_status = f"OK ({total_mcp})"
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
        agent_subagent_items = [
            i for i in subagent_plan if i.agent_name in (name, definition.display_name)
        ]
        active_items = [i for i in agent_subagent_items if i.action != "SKIP"]
        if active_items:
            subagent_status = _summarize_subagent_status(
                [item.action for item in active_items]
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
) -> tuple[List[MemoryStatusRow], int, int]:
    rows: List[MemoryStatusRow] = []
    total_notes = 0
    mem_issues = 0

    def latest_memory_update(memory_dir: Path) -> date | None:
        candidates = [memory_dir / "index.md"]
        notes_dir = memory_dir / "notes"
        if notes_dir.is_dir():
            candidates.extend(notes_dir.glob("*.md"))
        existing = [path for path in candidates if path.is_file()]
        if not existing:
            return None
        return date.fromtimestamp(max(path.stat().st_mtime for path in existing))

    # Global Memory: Global memory has no ~/.agents/memory symlink requirement.
    global_mem_dir = aikito_dir / "memory"
    global_index = global_mem_dir / "index.md"
    global_index_status = "OK" if global_index.is_file() else "MISSING"
    if global_index_status != "OK":
        mem_issues += 1

    notes_dir = global_mem_dir / "notes"
    global_notes_count = len(list(notes_dir.glob("*.md"))) if notes_dir.is_dir() else 0
    total_notes += global_notes_count

    rows.append(
        MemoryStatusRow(
            name="Global Memory",
            scope="Global",
            status=global_index_status,
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
                proj_index = proj_mem / "index.md"
                proj_index_status = "OK" if proj_index.is_file() else "MISSING"
                if proj_index_status != "OK":
                    mem_issues += 1

                proj_notes = proj_mem / "notes"
                proj_notes_count = (
                    len(list(proj_notes.glob("*.md"))) if proj_notes.is_dir() else 0
                )
                total_notes += proj_notes_count

                # Read local codebase path from projects/<name>/agent.toml
                agent_toml = proj_folder / "agent.toml"
                proj_path_val = None
                if agent_toml.is_file():
                    try:
                        with open(agent_toml, "rb") as f:
                            toml_data = tomllib.load(f)
                            proj_path_val = toml_data.get("path")
                    except (tomllib.TOMLDecodeError, OSError) as exc:
                        print(
                            f"[WARN] Failed to read {agent_toml}: {exc}",
                            file=sys.stderr,
                        )

                p_link_status = "N/A"
                if proj_path_val:
                    actual_proj_path = Path(proj_path_val).expanduser().resolve()
                    proj_agents_mem = actual_proj_path / ".agents" / "memory"

                    if proj_agents_mem.is_symlink():
                        p_link_status = "OK"
                    elif proj_agents_mem.is_dir():
                        idx_link = proj_agents_mem / "index.md"
                        if idx_link.is_symlink():
                            p_link_status = "OK"
                        else:
                            p_link_status = "CONFLICT"
                            mem_issues += 1
                    else:
                        p_link_status = "MISSING"
                        mem_issues += 1
                else:
                    p_link_status = "N/A"

                rows.append(
                    MemoryStatusRow(
                        name=proj_folder.name,
                        scope="Project",
                        status=(
                            proj_index_status
                            if proj_index_status != "OK"
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

    return StatusReportData(
        agents=agent_rows,
        memories=memory_rows,
        total_subagents_count=total_subagents,
        total_mcp_count=total_mcp,
        total_skills_count=total_skills_count,
        total_memory_notes=total_memory_notes,
        issues_count=total_issues,
    )


def collect_mcp_matrix(
    aikito_dir: Path, home: Path, live: bool = False
) -> tuple[List[MCPServerRow], List[str]]:
    agents_dict = load_agents(aikito_dir, home)
    specs = load_agent_specs(aikito_dir, home)
    agent_names = [a.display_name for a in agents_dict.values()]
    agent_key_to_display = {k: v.display_name for k, v in agents_dict.items()}

    servers: Dict[str, Dict[str, str]] = {}
    for spec in specs:
        srv_name = spec.server
        ag_display = agent_key_to_display.get(spec.agent, spec.agent)

        if srv_name not in servers:
            servers[srv_name] = {}

        if spec.agent in agents_dict:
            st = evaluate_spec_status(spec)
        else:
            st = "SKIP"

        servers[srv_name][ag_display] = st

    if live:
        live_commands = {}
        for spec in specs:
            agent_status = servers[spec.server].get(
                agent_key_to_display.get(spec.agent, spec.agent)
            )
            if agent_status == "OK" and spec.live_command:
                live_commands.setdefault(spec.agent, spec.live_command)

        for result in run_live_mcp_commands(live_commands):
            display_name = agent_key_to_display[result.agent]
            for server_statuses in servers.values():
                if server_statuses.get(display_name) != "OK":
                    continue
                if result.status == "OK":
                    server_statuses[display_name] = "OK_LIVE"
                elif result.status in ("ERROR", "TIMEOUT"):
                    server_statuses[display_name] = "ERROR"

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
) -> tuple[List[SubagentRow], List[OrphanSubagentFile], List[str]]:
    plan_items, _ = build_plan(aikito_dir=aikito_dir, home=home, allow_empty=True)
    agents_dict = load_agents(aikito_dir, home)
    agent_names = [a.display_name for a in agents_dict.values()]

    subagents_map: Dict[str, Dict[str, str]] = {}
    orphan_files: List[OrphanSubagentFile] = []

    for item in plan_items:
        ag_def = agents_dict.get(item.agent_name)
        ag_display = ag_def.display_name if ag_def else item.agent_name

        if item.action == "ORPHAN":
            rel_path = str(item.target_path)
            try:
                rel_path = f"~/{item.target_path.relative_to(home)}"
            except ValueError:
                pass
            orphan_files.append(
                OrphanSubagentFile(agent_display_name=ag_display, file_path=rel_path)
            )
            continue

        if item.subagent_name == "*":
            continue

        sub_name = item.subagent_name
        if sub_name not in subagents_map:
            subagents_map[sub_name] = {}

        if item.action in ("CREATE", "UPDATE", "FORCE UPDATE"):
            subagents_map[sub_name][ag_display] = "MISSING"
        elif item.action == "CONFLICT":
            subagents_map[sub_name][ag_display] = "CONFLICT"
        elif item.action == "OK":
            subagents_map[sub_name][ag_display] = "OK"
        elif item.action == "SKIP":
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


def _parse_index_titles(index_file: Path) -> Tuple[Set[str], Dict[str, str]]:
    indexed_stems: Set[str] = set()
    titles: Dict[str, str] = {}
    if not index_file.exists():
        return indexed_stems, titles

    content = index_file.read_text(encoding="utf-8", errors="ignore")
    pattern = re.compile(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]")
    for match in pattern.finditer(content):
        stem = match.group(1).strip()
        title = match.group(2).strip() if match.group(2) else ""
        indexed_stems.add(stem)
        if title:
            titles[stem] = title

    return indexed_stems, titles


def collect_memory_notes_rows(aikito_dir: Path, home: Path) -> List[MemoryNoteRow]:
    rows = []

    # 1. Global Memory Notes
    global_mem = aikito_dir / "memory"
    global_index_file = global_mem / "index.md"
    g_indexed_stems, g_titles = _parse_index_titles(global_index_file)

    global_notes_dir = global_mem / "notes"
    if global_notes_dir.is_dir():
        for note_file in sorted(global_notes_dir.glob("*.md")):
            stem = note_file.stem
            is_indexed = stem in g_indexed_stems
            title = g_titles.get(stem, "-")
            rows.append(
                MemoryNoteRow(
                    scope_name="Global",
                    note_name=stem,
                    title=title,
                    is_indexed=is_indexed,
                    link_status="SKIP",
                )
            )

    # 2. Project Memory Notes
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if proj_folder.is_dir():
                proj_mem = proj_folder / "memory"
                proj_index = proj_mem / "index.md"
                proj_notes = proj_mem / "notes"

                p_indexed_stems, p_titles = _parse_index_titles(proj_index)

                agent_toml = proj_folder / "agent.toml"
                proj_path_val = None
                if agent_toml.is_file():
                    try:
                        with open(agent_toml, "rb") as f:
                            data = tomllib.load(f)
                            proj_path_val = data.get("path")
                    except Exception:
                        pass

                link_st = "MISSING"
                if proj_path_val:
                    actual_proj_path = Path(proj_path_val).expanduser().resolve()
                    proj_agents_mem = actual_proj_path / ".agents" / "memory"
                    if proj_agents_mem.exists():
                        link_st = "OK"

                if proj_notes.is_dir():
                    for p_note in sorted(proj_notes.glob("*.md")):
                        p_stem = p_note.stem
                        p_indexed = p_stem in p_indexed_stems
                        p_title = p_titles.get(p_stem, "-")
                        rows.append(
                            MemoryNoteRow(
                                scope_name=proj_folder.name,
                                note_name=p_stem,
                                title=p_title,
                                is_indexed=p_indexed,
                                link_status=link_st,
                            )
                        )

    return rows


def _parse_skill_description(skill_dir: Path) -> str:
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        return "-"
    try:
        content = skill_md.read_text(encoding="utf-8", errors="ignore")
        if content.startswith("---"):
            parts = content.split("---", 2)
            if len(parts) >= 3:
                frontmatter = parts[1]
                for line in frontmatter.splitlines():
                    if line.startswith("description:"):
                        desc = line.split("description:", 1)[1].strip()
                        return desc
    except Exception:
        pass
    return "-"


def collect_skills_rows(aikito_dir: Path) -> List[SkillRow]:
    global_skills = set(_get_skills_list(aikito_dir))

    project_skills: Dict[str, Set[str]] = {}
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
                            f"[WARN] Failed to read {agent_toml}: {exc}",
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

    rows: List[SkillRow] = []

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
