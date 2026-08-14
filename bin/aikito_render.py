"""
Rendering module for aikito status reports.
Formats StatusReportData into TTY Unicode Box tables or ASCII fallback tables with status symbols.
"""

import re
import json
import shutil
import sys
import unicodedata
from dataclasses import dataclass

from typing import Any, Dict, List, Optional, Tuple

from aikito_project import ProjectSummary


@dataclass
class AgentStatusRow:
    agent_name: str
    display_name: str
    instructions_status: str
    skills_status: str
    skills_link_depth: Optional[int] = None
    mcp_status: str = "SKIP"
    subagent_status: str = "SKIP"


@dataclass
class MemoryStatusRow:
    name: str
    scope: str
    index_status: str
    notes_count: int
    target_link: Optional[str]
    link_status: str


@dataclass
class StatusReportData:
    agents: List[AgentStatusRow]
    memories: List[MemoryStatusRow]
    total_subagents_count: int = 0
    total_mcp_count: int = 0
    total_skills_count: int = 0
    total_memory_notes: int = 0
    issues_count: int = 0


@dataclass
class MCPServerRow:
    server_name: str
    agent_statuses: Dict[
        str, str
    ]  # agent_display_name -> status ("OK", "OK_LIVE", "DRIFT", "MISSING", "SKIP")


@dataclass
class SubagentRow:
    subagent_name: str
    agent_statuses: Dict[
        str, str
    ]  # agent_display_name -> status ("OK", "CONFLICT", "SKIP")


@dataclass
class OrphanSubagentFile:
    agent_display_name: str
    file_path: str


@dataclass
class SkillRow:
    skill_name: str
    scope: str
    source_status: str
    description: str


@dataclass
class MemoryNoteRow:
    scope_name: str
    note_name: str
    title: str
    is_indexed: bool
    link_status: str  # "OK", "DANGLING", "N/A"


@dataclass
class DoctorFinding:
    status: str  # "OK" | "FAIL" | "WARN"
    message: str  # Human-readable description
    fix_hint: str = ""  # Suggested fix command (empty if none)


@dataclass
class DoctorSection:
    name: str
    findings: List["DoctorFinding"]


@dataclass
class DoctorReport:
    sections: List["DoctorSection"]

    @property
    def fail_count(self) -> int:
        return sum(1 for s in self.sections for f in s.findings if f.status == "FAIL")

    @property
    def warn_count(self) -> int:
        return sum(1 for s in self.sections for f in s.findings if f.status == "WARN")


# ANSI Colors
COLOR_RESET = "\033[0m"
COLOR_GREEN = "\033[32m"
COLOR_YELLOW = "\033[33m"
COLOR_RED = "\033[31m"
COLOR_CYAN = "\033[36m"
COLOR_DIM = "\033[2m"
COLOR_BOLD = "\033[1m"


def _colorize(text: str, color_code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"{color_code}{text}{COLOR_RESET}"


def _format_badge_text(status_str: str, use_unicode: bool) -> Tuple[str, str]:
    """
    Returns (display_text, status_type) where status_type in ("ok", "issue", "skip").
    """
    ok_sym = "✓" if use_unicode else "v"
    skip_sym = "–" if use_unicode else "-"
    warn_sym = "⚠" if use_unicode else "!"

    if status_str == "OK":
        return f"{ok_sym}", "ok"
    if status_str == "OK_LIVE":
        return f"{ok_sym} (live)", "ok"
    if status_str.startswith("OK ("):
        count_part = status_str[4:-1]
        return f"{ok_sym} {count_part}", "ok"
    if status_str in ("SKIP", "N/A"):
        return f"{skip_sym}", "skip"
    if status_str == "PRESENT":
        return "P", "skip"

    prefix_map = {
        "CONFLICT": "C",
        "MISSING": "M",
        "DRIFT": "D",
        "ERROR": "E",
    }

    for key, code in prefix_map.items():
        if status_str == key:
            return f"{warn_sym} {code}", "issue"
        if status_str.startswith(f"{key} ("):
            parts = status_str.split(" (", 1)
            count_part = parts[1][:-1]
            return f"{warn_sym} {code} {count_part}", "issue"

    return status_str, "ok"


def _format_status_badge(status_str: str, use_unicode: bool, use_color: bool) -> str:
    text, status_type = _format_badge_text(status_str, use_unicode)
    if status_type == "ok":
        return _colorize(text, COLOR_GREEN, use_color)
    elif status_type == "issue":
        return _colorize(text, COLOR_RED, use_color)
    elif status_type == "skip":
        return _colorize(text, COLOR_DIM, use_color)
    return text


def _get_terminal_width() -> Optional[int]:
    if not sys.stdout.isatty():
        return None
    try:
        cols = shutil.get_terminal_size().columns
        return cols if cols >= 40 else 40
    except Exception:
        return None


def _get_display_width(text: str) -> int:
    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    clean_text = ansi_escape.sub("", text)
    width = 0
    for char in clean_text:
        w = unicodedata.east_asian_width(char)
        if w in ("F", "W"):
            width += 2
        else:
            width += 1
    return width


def _truncate_display_text(text: str, max_width: int) -> str:
    if max_width <= 0:
        return ""
    if _get_display_width(text) <= max_width:
        return text

    ansi_escape = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
    plain_text = ansi_escape.sub("", text)

    current_w = 0
    truncated_chars = []
    target_w = max_width - 1  # Save 1 column for '…'

    for ch in plain_text:
        ch_w = 2 if unicodedata.east_asian_width(ch) in ("F", "W") else 1
        if current_w + ch_w > target_w:
            break
        current_w += ch_w
        truncated_chars.append(ch)

    res = "".join(truncated_chars) + "…"
    # Re-apply color styling if original text had ANSI codes
    if "\x1b" in text:
        if "\x1b[32m" in text:
            return f"\033[32m{res}\033[0m"
        elif "\x1b[31m" in text:
            return f"\033[31m{res}\033[0m"
        elif "\x1b[33m" in text:
            return f"\033[33m{res}\033[0m"
        elif "\x1b[2m" in text:
            return f"\033[2m{res}\033[0m"
    return res


def _build_generic_table(
    headers: List[str],
    formatted_rows: List[Any],
    use_unicode: bool,
    use_color: bool,
    truncatable_cols: Optional[List[int]] = None,
) -> str:
    col_widths = [_get_display_width(h) for h in headers]
    for row in formatted_rows:
        if isinstance(row, list):
            for i, val in enumerate(row):
                col_widths[i] = max(col_widths[i], _get_display_width(val))

    num_cols = len(headers)
    term_width = _get_terminal_width()

    if term_width is not None:
        border_overhead = 3 * num_cols + 1
        max_content_width = term_width - border_overhead
        current_content_width = sum(col_widths)

        if current_content_width > max_content_width and max_content_width > 0:
            excess = current_content_width - max_content_width

            if truncatable_cols is None:
                # Default truncatable columns: from right to left (excluding col 0)
                truncatable_cols = list(range(num_cols - 1, 0, -1))

            min_widths = {
                c_idx: max(_get_display_width(headers[c_idx]), 6)
                for c_idx in truncatable_cols
            }
            while excess > 0:
                reducible_cols = [
                    c_idx
                    for c_idx in truncatable_cols
                    if col_widths[c_idx] > min_widths[c_idx]
                ]
                if not reducible_cols:
                    break

                widest_col = max(reducible_cols, key=lambda c_idx: col_widths[c_idx])
                col_widths[widest_col] -= 1
                excess -= 1

    if use_unicode:
        top_l, top_m, top_r = "┌", "┬", "┐"
        mid_l, mid_m, mid_r = "├", "┼", "┤"
        bot_l, bot_m, bot_r = "└", "┴", "┘"
        horiz, vert = "─", "│"
    else:
        top_l = top_m = top_r = "+"
        mid_l = mid_m = mid_r = "+"
        bot_l = bot_m = bot_r = "+"
        horiz = "-"
        vert = "|"

    lines = []

    # Top border
    top_parts = [horiz * (w + 2) for w in col_widths]
    lines.append(f"{top_l}{top_m.join(top_parts)}{top_r}")

    # Header row
    header_cells = []
    for i, h in enumerate(headers):
        visible_len = _get_display_width(h)
        if visible_len > col_widths[i]:
            h = _truncate_display_text(h, col_widths[i])
            visible_len = _get_display_width(h)
        padding = " " * (col_widths[i] - visible_len)
        header_cells.append(f" {h}{padding} ")
    header_line = f"{vert}{vert.join(header_cells)}{vert}"
    lines.append(_colorize(header_line, COLOR_BOLD, use_color))

    # Header separator
    mid_parts = [horiz * (w + 2) for w in col_widths]
    mid_sep_line = f"{mid_l}{mid_m.join(mid_parts)}{mid_r}"
    lines.append(mid_sep_line)

    # Data rows
    for row in formatted_rows:
        if row == "---SEPARATOR---":
            lines.append(mid_sep_line)
            continue

        cells = []
        for i, val in enumerate(row):
            visible_len = _get_display_width(val)
            if visible_len > col_widths[i]:
                val = _truncate_display_text(val, col_widths[i])
                visible_len = _get_display_width(val)
            padding = " " * (col_widths[i] - visible_len)
            cells.append(f" {val}{padding} ")
        lines.append(f"{vert}{vert.join(cells)}{vert}")

    # Bottom border
    bot_parts = [horiz * (w + 2) for w in col_widths]
    lines.append(f"{bot_l}{bot_m.join(bot_parts)}{bot_r}")

    return "\n".join(lines)


def _format_skills_badge(
    status_str: str,
    link_depth: Optional[int],
    use_unicode: bool,
    use_color: bool,
) -> str:
    if link_depth is None or status_str in ("SKIP", "N/A"):
        return _format_status_badge("SKIP", use_unicode, use_color)

    if status_str.startswith("OK"):
        m = re.search(r"\((\d+)\)", status_str)
        count_str = m.group(1) if m else "0"
        if link_depth == 1:
            sym = "›" if use_unicode else ">"
        elif link_depth == 2:
            sym = "»" if use_unicode else ">>"
        else:
            sym = ""
        text = f"{count_str} {sym}".strip() if sym else count_str
        return _colorize(text, COLOR_GREEN, use_color)

    return _format_status_badge(status_str, use_unicode, use_color)


def render_agents_table(
    rows: List[AgentStatusRow], use_unicode: bool, use_color: bool
) -> str:
    headers = ["Agent", "Instructions", "Skills", "MCP Config", "Subagents"]

    formatted_rows = []
    for r in rows:
        formatted_rows.append(
            [
                r.display_name,
                _format_status_badge(r.instructions_status, use_unicode, use_color),
                _format_skills_badge(
                    r.skills_status, r.skills_link_depth, use_unicode, use_color
                ),
                _format_status_badge(r.mcp_status, use_unicode, use_color),
                _format_status_badge(r.subagent_status, use_unicode, use_color),
            ]
        )
    return _build_generic_table(headers, formatted_rows, use_unicode, use_color)


def render_memory_table(
    rows: List[MemoryStatusRow], use_unicode: bool, use_color: bool
) -> str:
    headers = ["Memory Scope", "Index", "Notes", "Link Target", "Link Status"]

    formatted_rows = []
    for r in rows:
        target_display = r.target_link if r.target_link else "-"
        formatted_rows.append(
            [
                r.name,
                _format_status_badge(r.index_status, use_unicode, use_color),
                str(r.notes_count),
                target_display,
                _format_status_badge(r.link_status, use_unicode, use_color),
            ]
        )
    return _build_generic_table(headers, formatted_rows, use_unicode, use_color)


def render_mcp_status_table(
    server_rows: List[MCPServerRow],
    agent_names: List[str],
    use_unicode: bool,
    use_color: bool,
) -> str:
    headers = ["MCP Server"] + agent_names
    formatted_rows = []
    for s in server_rows:
        row_data = [s.server_name]
        for ag in agent_names:
            st = s.agent_statuses.get(ag, "SKIP")
            row_data.append(_format_status_badge(st, use_unicode, use_color))
        formatted_rows.append(row_data)
    return _build_generic_table(headers, formatted_rows, use_unicode, use_color)


def render_agent_mcp_table(rows: List[Any], use_unicode: bool, use_color: bool) -> str:
    formatted_rows = [
        [
            row.server_name,
            row.source.capitalize(),
            _format_status_badge(row.status, use_unicode, use_color),
        ]
        for row in rows
    ]
    return _build_generic_table(
        ["MCP Server", "Source", "Status"],
        formatted_rows,
        use_unicode,
        use_color,
    )


def _render_title_box(
    title: str, *, use_unicode: bool, use_color: bool, inner_width: int | None = None
) -> str:
    if use_unicode:
        tl, tr, bl, br, horiz, vert = "╭", "╮", "╰", "╯", "─", "│"
    else:
        tl = tr = bl = br = "+"
        horiz, vert = "-", "|"
    title_text = f" {title} "
    width = inner_width or _get_display_width(title_text) + 3
    padding = " " * max(0, width - _get_display_width(title_text))
    return "\n".join(
        [
            f"{tl}{horiz * width}{tr}",
            f"{vert}{_colorize(title_text, COLOR_BOLD, use_color)}{padding}{vert}",
            f"{bl}{horiz * width}{br}",
        ]
    )


def render_mcp_details(
    rows: List[Any], canonical_path: str, use_unicode: bool, use_color: bool
) -> str:
    blocks = [
        f"MCP Server: {rows[0].server_name}",
        f"Canonical source: {canonical_path}",
    ]
    for row in rows:
        entry = (
            json.dumps(row.entry, ensure_ascii=False, indent=2)
            if row.entry is not None
            else "<missing>"
        )
        blocks.append(
            "\n".join(
                [
                    _render_title_box(
                        row.agent_display_name,
                        use_unicode=use_unicode,
                        use_color=use_color,
                    ),
                    f"Agent key: {row.agent_name}",
                    f"Status: {'synced' if row.status == 'OK' else row.status.lower()}",
                    f"Config: {row.config_path}",
                    f"Format: {row.config_format}",
                    "",
                    "Managed entry:",
                    entry,
                ]
            )
        )
    return "\n".join(blocks[:2]) + "\n\n" + "\n\n".join(blocks[2:])


def render_instruction_agent_status(
    rows: List[tuple[str, str, str]],
    project_rows: List[tuple[str, str]],
    use_unicode: bool,
    use_color: bool,
) -> str:
    blocks = ["Instructions"]
    for display_name, target, status in rows:
        blocks.append(
            "\n".join(
                [
                    _render_title_box(
                        display_name,
                        use_unicode=use_unicode,
                        use_color=use_color,
                    ),
                    f"Status: {status}",
                    f"Target: {target}",
                ]
            )
        )
    if project_rows:
        blocks.append(
            "\n".join(
                [
                    _render_title_box(
                        "Projects", use_unicode=use_unicode, use_color=use_color
                    ),
                    *(f"{name}: {status}" for name, status in project_rows),
                ]
            )
        )
    return "\n\n".join(blocks)


def render_subagents_status_table(
    subagent_rows: List[SubagentRow],
    orphan_files: List[OrphanSubagentFile],
    agent_names: List[str],
    use_unicode: bool,
    use_color: bool,
) -> str:
    headers = ["Subagent"] + agent_names
    formatted_rows = []
    for sub in subagent_rows:
        row_data = [sub.subagent_name]
        for ag in agent_names:
            st = sub.agent_statuses.get(ag, "SKIP")
            row_data.append(_format_status_badge(st, use_unicode, use_color))
        formatted_rows.append(row_data)

    output = [_build_generic_table(headers, formatted_rows, use_unicode, use_color)]

    if orphan_files:
        output.append("")
        orphan_title = "Orphan Subagent Files (Managed files no longer defined):"
        if use_color:
            orphan_title = _colorize(orphan_title, COLOR_YELLOW, True)
        output.append(orphan_title)

        o_headers = ["Agent", "Orphan File Path"]
        o_rows = [[o.agent_display_name, o.file_path] for o in orphan_files]
        output.append(_build_generic_table(o_headers, o_rows, use_unicode, use_color))

    return "\n".join(output)


def render_memory_notes_table(
    notes: List[MemoryNoteRow],
    use_unicode: bool,
    use_color: bool,
) -> str:
    headers = ["Scope", "Note File", "Title", "Index", "Link"]
    ok_sym = "✓" if use_unicode else "v"
    warn_sym = "⚠" if use_unicode else "!"

    formatted_rows: List[Any] = []
    last_scope = None

    for n in notes:
        if last_scope is not None and n.scope_name != last_scope:
            formatted_rows.append("---SEPARATOR---")
        last_scope = n.scope_name

        idx_str = ok_sym if n.is_indexed else f"{warn_sym} M"
        idx_badge = _colorize(
            idx_str, COLOR_GREEN if n.is_indexed else COLOR_RED, use_color
        )

        link_badge = _format_status_badge(n.link_status, use_unicode, use_color)

        formatted_rows.append(
            [
                n.scope_name,
                n.note_name,
                n.title if n.title else "-",
                idx_badge,
                link_badge,
            ]
        )

    return _build_generic_table(
        headers, formatted_rows, use_unicode, use_color, truncatable_cols=[2, 1]
    )


def render_skills_table(
    skills: List[SkillRow],
    use_unicode: bool,
    use_color: bool,
) -> str:
    headers = ["Skill", "Scope", "Source", "Description"]

    formatted_rows: List[Any] = []
    last_scope = None

    for s in skills:
        if last_scope is not None and s.scope != last_scope:
            formatted_rows.append("---SEPARATOR---")
        last_scope = s.scope

        source_badge = _format_status_badge(s.source_status, use_unicode, use_color)

        formatted_rows.append(
            [
                s.skill_name,
                s.scope,
                source_badge,
                s.description if s.description else "-",
            ]
        )

    return _build_generic_table(
        headers, formatted_rows, use_unicode, use_color, truncatable_cols=[3]
    )


def render_projects_table(
    projects: List[ProjectSummary], use_unicode: bool, use_color: bool
) -> str:
    rows = [
        [
            project.name,
            project.path,
            project.sync_mode,
            _format_status_badge(project.instructions_status, use_unicode, use_color),
            str(project.skills_count),
            str(project.memory_notes_count),
            _format_status_badge(project.runtime_status, use_unicode, use_color),
        ]
        for project in projects
    ]
    return _build_generic_table(
        [
            "Project",
            "Path",
            "Mode",
            "Instructions",
            "Skills",
            "Memory",
            "Sync",
        ],
        rows,
        use_unicode,
        use_color,
        truncatable_cols=[1],
    )


def render_project_detail(
    project: ProjectSummary, use_unicode: bool, use_color: bool
) -> str:
    separator = " · " if use_unicode else " | "

    skills = ", ".join(project.skill_names) if project.skill_names else "0 selected"
    memory = f"{project.memory_notes_count} notes"
    if project.memory_refs:
        memory += f"{separator}{len(project.memory_refs)} references"
    else:
        memory += f"{separator}0 references"

    lines = [
        f"Project: {project.name}",
        f"Canonical directory: {project.config_path.parent}",
        f"Project directory: {project.path}",
        f"Sync mode: {project.sync_mode}",
        "Instructions: "
        + ("configured" if project.instructions_status == "OK" else "not configured"),
        f"Selected skills: {skills}",
        f"Memory: {memory}",
        f"Sync: {project.runtime_status}",
    ]
    if project.error:
        lines.append(f"Error: {project.error}")
    issues = [detail for detail in project.details if detail.status != "OK"]
    if issues:
        lines.append("Issues:")
        lines.extend(
            f"  {detail.resource} [{detail.status}]: {detail.detail}"
            for detail in issues
        )
    elif project.runtime_status == "PATH MISSING":
        lines.extend(
            ["Issues:", f"  Project: directory does not exist: {project.path}"]
        )
    elif project.runtime_status == "UNBOUND":
        lines.extend(["Issues:", "  Project: no directory is registered"])
    return "\n".join(lines)


def render_status_report(
    data: StatusReportData,
    *,
    is_tty: bool = True,
    no_color: bool = False,
) -> str:
    use_unicode = is_tty
    use_color = is_tty and not no_color

    dot = "·" if use_unicode else "*"
    ok_sym = "✓" if use_unicode else "v"
    skip_sym = "–" if use_unicode else "-"
    warn_sym = "⚠" if use_unicode else "!"

    output_sections = []

    # 1. Agents section
    if data.agents:
        output_sections.append(render_agents_table(data.agents, use_unicode, use_color))

    # 2. Memories section
    if data.memories:
        output_sections.append("")
        mem_title = "Memory Resources"
        if use_color:
            mem_title = _colorize(mem_title, COLOR_BOLD, True)
        output_sections.append(mem_title)
        output_sections.append(
            render_memory_table(data.memories, use_unicode, use_color)
        )

    agents_count = len(data.agents)
    skills_count = data.total_skills_count
    scopes_count = len(data.memories)
    notes_count = data.total_memory_notes
    indent = "  "

    # 3. Summary & Legend
    if data.issues_count > 0:
        output_sections.append("")
        legend_text = (
            f"Legend: {ok_sym} synced {dot} {skip_sym} skipped {dot} "
            f"{warn_sym} M missing {dot} {warn_sym} C conflict {dot} {warn_sym} D drift"
        )
        if use_color:
            legend_text = _colorize(legend_text, COLOR_DIM, True)
        output_sections.append(legend_text)

        output_sections.append("")
        issue_label = "issue" if data.issues_count == 1 else "issues"
        summary_line = (
            f"{warn_sym} {data.issues_count} {issue_label} {dot} run: aikito doctor"
        )
        if use_color:
            summary_line = _colorize(summary_line, COLOR_YELLOW, True)
        output_sections.append(summary_line)
    else:
        output_sections.append("")
        summary_line1 = f"{ok_sym} all synced {dot} {agents_count} agents {dot} {skills_count} skills"
        if use_color:
            summary_line1 = _colorize(summary_line1, COLOR_GREEN, True)
        output_sections.append(summary_line1)

        summary_line_notes = f"{indent}{notes_count} notes across {scopes_count} scopes"
        if use_color:
            summary_line_notes = _colorize(summary_line_notes, COLOR_GREEN, True)
        output_sections.append(summary_line_notes)

    return "\n".join(output_sections)


def render_doctor_report(
    report: "DoctorReport",
    *,
    is_tty: bool = True,
    no_color: bool = False,
    use_unicode: bool = True,
) -> str:
    """Render a DoctorReport as grouped sections with box-drawing borders.

    Each section is wrapped in a titled box. Findings are rendered as
    individual lines with status symbols and right borders.
    """
    use_color = is_tty and not no_color

    ok_sym = "✓" if use_unicode else "[OK]"
    fail_sym = "✗" if use_unicode else "[FAIL]"
    warn_sym = "⚠" if use_unicode else "[WARN]"

    output_blocks: List[str] = []
    max_title_w = max((_get_display_width(s.name) for s in report.sections), default=10)
    box_inner_width = (
        max_title_w + 3
    )  # Dynamically fitted to longest section title + 3 chars

    for section in report.sections:
        lines: List[str] = []

        # 1. Title box (uniform length across all section titles)
        lines.append(
            _render_title_box(
                section.name,
                use_unicode=use_unicode,
                use_color=use_color,
                inner_width=box_inner_width,
            )
        )

        # 2. Findings lines underneath title box
        for finding in section.findings:
            if finding.status == "OK":
                sym_colored = _colorize(ok_sym, COLOR_GREEN, use_color)
            elif finding.status == "FAIL":
                sym_colored = _colorize(fail_sym, COLOR_RED, use_color)
            else:
                sym_colored = _colorize(warn_sym, COLOR_YELLOW, use_color)

            lines.append(f"  {sym_colored} {finding.message}")

            if finding.fix_hint:
                hint_prefix = "    → " if use_unicode else "    -> "
                hint_colored = _colorize(finding.fix_hint, COLOR_DIM, use_color)
                lines.append(f"{hint_prefix}{hint_colored}")

        output_blocks.append("\n".join(lines))

    output_blocks.append("")

    fail_count = report.fail_count
    warn_count = report.warn_count

    if fail_count == 0 and warn_count == 0:
        summary = _colorize(f"{ok_sym} All checks passed.", COLOR_GREEN, use_color)
    else:
        parts = []
        if fail_count:
            issue_word = "issue" if fail_count == 1 else "issues"
            parts.append(_colorize(f"{fail_count} {issue_word}", COLOR_RED, use_color))
        if warn_count:
            warn_word = "warning" if warn_count == 1 else "warnings"
            parts.append(
                _colorize(f"{warn_count} {warn_word}", COLOR_YELLOW, use_color)
            )
        summary = "Found " + ", ".join(parts) + "."

    output_blocks.append(summary)
    return "\n".join(output_blocks)
