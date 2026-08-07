"""
Status aggregation module for aikito.
Gathers synchronization status data across agents, instructions, skills, MCP, subagents, and memory.
"""

import re
import sys
import tomllib
from pathlib import Path
from typing import List, Dict, Any, Tuple, Set

from aikito_link import classify_symlink, symlink_verdict_to_status, SymlinkVerdict


from aikito_mcp import (
    load_agents,
    load_agent_specs,
    evaluate_spec_status,
    AgentDefinition,
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


def collect_agent_status_rows(
    aikito_dir: Path, home: Path
) -> tuple[List[AgentStatusRow], int, int, int]:
    agents_dict = load_agents(aikito_dir, home)
    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    global_skills = _get_skills_list(aikito_dir)
    total_global_skills = len(global_skills)

    # Pre-fetch MCP specs and Subagent plan items
    mcp_specs = load_agent_specs(aikito_dir, home)
    subagent_plan, _ = build_plan(aikito_dir, home, allow_empty=True)

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
        if definition.mcp_config_format != "unsupported" and agent_mcp_specs:
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
            total_sub = len(active_items)
            ok_sub = sum(1 for i in active_items if i.action == "OK")
            if ok_sub == total_sub:
                subagent_status = f"OK ({total_sub})"
            else:
                subagent_status = f"CONFLICT ({ok_sub}/{total_sub})"
                agent_issues += 1

        rows.append(
            AgentStatusRow(
                agent_name=name,
                display_name=definition.display_name,
                instructions_status=instructions_status,
                skills_status=skills_status,
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

    # Global Memory: Global memory has no ~/.agents/memory symlink requirement.
    global_mem_dir = aikito_dir / "memory"
    global_index = global_mem_dir / "index.md"
    global_index_status = "OK" if global_index.is_file() else "MISSING"
    if global_index_status != "OK":
        mem_issues += 1

    notes_dir = global_mem_dir / "notes"
    global_notes_count = len(list(notes_dir.glob("*.md"))) if notes_dir.is_dir() else 0
    total_notes += global_notes_count

    try:
        global_mem_target = f"~/{global_mem_dir.relative_to(home)}"
    except ValueError:
        global_mem_target = str(global_mem_dir)

    rows.append(
        MemoryStatusRow(
            name="Global Memory",
            scope="Global",
            index_status=global_index_status,
            notes_count=global_notes_count,
            target_link=global_mem_target,
            link_status="N/A",
        )
    )

    # Project Memory (scan ~/aikito/projects/<project_name>)
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
                p_link_target = None

                if proj_path_val:
                    actual_proj_path = Path(proj_path_val).expanduser().resolve()
                    proj_agents_mem = actual_proj_path / ".agents" / "memory"

                    try:
                        rel_path = proj_agents_mem.relative_to(home)
                        p_link_target = f"~/{rel_path}"
                    except ValueError:
                        p_link_target = str(proj_agents_mem)

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
                    p_link_target = "(unbound)"
                    p_link_status = "N/A"

                rows.append(
                    MemoryStatusRow(
                        name=f"Project ({proj_folder.name})",
                        scope="Project",
                        index_status=proj_index_status,
                        notes_count=proj_notes_count,
                        target_link=p_link_target,
                        link_status=p_link_status,
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
                                project_skills[proj_folder.name] = set(str(s) for s in p_skills)
                    except (tomllib.TOMLDecodeError, OSError) as exc:
                        print(f"[WARN] Failed to read {agent_toml}: {exc}", file=sys.stderr)

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
            p_name for p_name, p_skills in sorted(project_skills.items()) if name in p_skills
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

