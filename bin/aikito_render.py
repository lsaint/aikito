"""
Rendering module for aikito status reports.
Formats StatusReportData into TTY Unicode Box tables or ASCII fallback tables with status symbols.
"""

import re
import shutil
import sys
import unicodedata
from dataclasses import dataclass

from typing import Any, Dict, List, Optional, Tuple


@dataclass
class AgentStatusRow:
    agent_name: str
    display_name: str
    instructions_status: str
    skills_status: str
    mcp_status: str
    subagent_status: str


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
class MemoryNoteRow:
    scope_name: str
    note_name: str
    title: str
    is_indexed: bool
    link_status: str  # "OK", "DANGLING", "N/A"


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
    if status_str == "SKIP" or status_str == "N/A":
        return f"{skip_sym}", "skip"

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
                _format_status_badge(r.skills_status, use_unicode, use_color),
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
