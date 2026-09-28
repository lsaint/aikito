"""Read-only resource queries and presentation for the show commands."""

import argparse
import sys
from pathlib import Path

from .bundled_skills import print_bundled_skill_notice
from .cli_parser import resolve_color_flags
from .resolve import (
    ProjectContextConflictError,
    collect_instruction_agent_status,
    collect_project_instruction_status,
    detect_current_project,
    resolve_instruction_target,
    resolve_mcp_target_for_command,
    resolve_project_filter,
    resolve_skill_target_for_command,
    resolve_subagent_target_for_command,
)
from .memory import resolve_memory_target_for_command
from .mcp import MCPConfigError
from .project import collect_project_summaries
from .config import get_inbox_path
from .inbox import collect_inbox_rows, resolve_inbox_target_for_command
from .render import (
    render_agent_mcp_table,
    render_agent_subagent_table,
    render_mcp_details,
    render_mcp_runtime_table,
    render_subagent_details,
    render_instruction_agent_status,
    render_key_value_fields,
    render_inbox_table,
    render_mcp_status_table,
    render_memory_notes_table,
    render_project_detail,
    render_projects_table,
    render_skills_table,
    render_subagents_status_table,
)
from .status import (
    collect_mcp_details,
    collect_mcp_matrix,
    collect_mcp_runtime,
    collect_subagent_details,
    collect_memory_notes_rows,
    collect_memory_status_rows,
    collect_skills_rows,
    collect_subagents_matrix,
)
from .subagent import SubagentConfigError
from .workspace.paths import resolve_workspace


def cmd_show_project(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    home = Path.home()
    projects = collect_project_summaries(aikito_dir, home)
    target = getattr(args, "target", None)
    show_target = getattr(args, "show_target", "project")
    use_unicode, use_color = resolve_color_flags(args)

    if target == ".":
        try:
            detected = detect_current_project(aikito_dir, Path.cwd(), home)
        except ProjectContextConflictError as exc:
            names = ", ".join(exc.projects)
            print(
                f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                file=sys.stderr,
            )
            sys.exit(1)
        if detected is None:
            print(
                f"[ERROR] Current directory is not inside a registered project: {Path.cwd().resolve()}",
                file=sys.stderr,
            )
            sys.exit(1)
        target = detected

    if not target:
        if show_target != "projects":
            try:
                detected = detect_current_project(aikito_dir, Path.cwd(), home)
            except ProjectContextConflictError as exc:
                names = ", ".join(exc.projects)
                print(
                    f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if detected:
                print(f"[aikito] Target project: '{detected}' (detected from cwd)")
                target = detected

    if not target:
        memory_rows, _, _ = collect_memory_status_rows(aikito_dir, home)
        print(
            render_projects_table(
                projects, use_unicode, use_color, memory_rows=memory_rows
            )
        )
        return

    exact = [project for project in projects if project.name == target]
    matches = exact or [
        project for project in projects if project.name.startswith(target)
    ]
    if len(matches) == 1:
        print(render_project_detail(matches[0], use_unicode, use_color))
        return
    if not matches:
        print(f"[ERROR] Project '{target}' not found.", file=sys.stderr)
    else:
        names = ", ".join(project.name for project in matches)
        print(
            f"[CONFLICT] Multiple projects match '{target}': {names}", file=sys.stderr
        )
    sys.exit(1)


def cmd_show_skill(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    target = getattr(args, "target", None)

    if not target:
        use_unicode, use_color = resolve_color_flags(args)
        skill_rows = collect_skills_rows(aikito_dir=aikito_dir)
        table_str = render_skills_table(skill_rows, use_unicode, use_color)
        print(table_str)
        print_bundled_skill_notice(aikito_dir)
        return

    skill_file = resolve_skill_target_for_command(aikito_dir, target, operation="show")

    try:
        print(skill_file.read_text(encoding="utf-8"), end="")
    except Exception as e:
        print(
            f"[ERROR] Failed to read skill file {skill_file}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)

    print_bundled_skill_notice(aikito_dir, names=(skill_file.parent.name,))


def cmd_show_instructions(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    target = getattr(args, "target", None)
    if not target:
        use_unicode, use_color = resolve_color_flags(args)
        rows = collect_instruction_agent_status(aikito_dir, Path.home())
        project_rows = collect_project_instruction_status(aikito_dir, Path.home())
        print(
            render_instruction_agent_status(rows, project_rows, use_unicode, use_color)
        )
        return

    name, instructions_path = resolve_instruction_target(
        aikito_dir, Path.home(), target, Path.cwd()
    )
    if not instructions_path.is_file():
        print(
            f"[ERROR] Instructions file not found: {instructions_path}",
            file=sys.stderr,
        )
        sys.exit(1)
    print(instructions_path.read_text(encoding="utf-8"), end="")
    if target == "." and name != "global":
        print("\nAlso active: global instructions", file=sys.stderr)
        print("View with: aikito show instructions global", file=sys.stderr)


def cmd_show_mcp(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    home = Path.home()
    use_unicode, use_color = resolve_color_flags(args)

    try:
        target = getattr(args, "target", None)
        raw_agent = getattr(args, "agent", None)
        agent_flag_passed = raw_agent is not None
        agent_target = None if raw_agent is True else raw_agent

        if getattr(args, "live", False) and not target and agent_flag_passed:
            print(
                "[ERROR] --agent with --live requires an MCP server target",
                file=sys.stderr,
            )
            sys.exit(2)

        if getattr(args, "live", False) and target:
            try:
                server_name, rows = collect_mcp_runtime(
                    aikito_dir=aikito_dir,
                    home=home,
                    server_target=target,
                    agent_target=agent_target,
                )
            except ValueError as exc:
                raise MCPConfigError(str(exc)) from exc
            print(
                render_key_value_fields(
                    [
                        ("MCP Server:", server_name),
                        (
                            "Canonical source:",
                            str(aikito_dir / "mcps" / f"{server_name}.toml"),
                        ),
                    ]
                )
            )
            print()
            print(render_mcp_runtime_table(rows, use_unicode, use_color))

            errors = [row for row in rows if row.error]
            if errors:
                print()
                for row in errors:
                    print(
                        f"[{row.connect_status}] {row.agent_display_name}: {row.error}"
                    )

            if agent_target is not None and len(rows) == 1 and rows[0].tool_names:
                print()
                print(f"Tools ({len(rows[0].tool_names)}):")
                for tool_name in rows[0].tool_names:
                    print(f"- {tool_name}")
            return

        # 1. Detail / Agent view across agents when --agent is passed
        # - show mcp <target> --agent: detail view of <target> across all agents
        # - show mcp <target> --agent <agent>: detail view of <target> for specific agent
        # - show mcp --agent <agent>: list of MCP servers on specific agent
        if (agent_target is not None) or (target and agent_flag_passed):
            try:
                rows = collect_mcp_details(
                    aikito_dir=aikito_dir,
                    home=home,
                    server_target=target,
                    agent_target=agent_target,
                )
            except ValueError as exc:
                raise MCPConfigError(str(exc)) from exc
            if not rows:
                selection = "/".join(value for value in (agent_target, target) if value)
                raise MCPConfigError(f"No MCP configuration found for '{selection}'")
            if agent_target and not target:
                first = rows[0]
                managed = [row for row in rows if row.source == "managed"]
                synced = sum(row.status == "OK" for row in managed)
                drifted = sum(row.status == "DRIFT" for row in managed)
                unmanaged = sum(row.source == "unmanaged" for row in rows)
                print(
                    render_key_value_fields(
                        [
                            ("Agent:", first.agent_display_name),
                            ("Agent key:", first.agent_name),
                            ("Config:", str(first.config_path)),
                            ("Format:", first.config_format),
                            (
                                "MCP Servers:",
                                f"{len(managed)} managed, {synced} synced, "
                                f"{drifted} drifted, {unmanaged} unmanaged",
                            ),
                        ]
                    )
                )
                print()
                print(render_agent_mcp_table(rows, use_unicode, use_color))
            else:
                print(
                    render_mcp_details(
                        rows,
                        str(aikito_dir / "mcps" / f"{rows[0].server_name}.toml"),
                        use_unicode,
                        use_color,
                    )
                )
            return

        # 2. Raw file view when target is specified without --agent
        if target:
            mcp_file = resolve_mcp_target_for_command(
                aikito_dir, target, operation="show"
            )
            try:
                print(mcp_file.read_text(encoding="utf-8"), end="")
            except Exception as e:
                print(
                    f"[ERROR] Failed to read MCP config file {mcp_file}: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            return

        # 3. Default matrix view
        server_rows, agent_names = collect_mcp_matrix(
            aikito_dir=aikito_dir,
            home=home,
            live=getattr(args, "live", False),
        )
        table_str = render_mcp_status_table(
            server_rows, agent_names, use_unicode, use_color
        )
        print(table_str)
    except MCPConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_show_subagents(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    home = Path.home()
    target = getattr(args, "target", None)
    agent_flag_passed = getattr(args, "agent", None) is not None
    agent_target = args.agent if isinstance(getattr(args, "agent", None), str) else None
    use_unicode, use_color = resolve_color_flags(args)

    try:
        # 1. Detail / Agent view across agents when --agent is passed
        if (agent_target is not None) or (target and agent_flag_passed):
            try:
                rows = collect_subagent_details(
                    aikito_dir=aikito_dir,
                    home=home,
                    subagent_target=target,
                    agent_target=agent_target,
                )
            except ValueError as exc:
                raise SubagentConfigError(str(exc)) from exc

            if not rows:
                selection = "/".join(value for value in (agent_target, target) if value)
                raise SubagentConfigError(
                    f"No subagent configuration found for '{selection}'"
                )

            if agent_target and not target:
                first = rows[0]
                synced = sum(row.status == "OK" for row in rows)
                drifted = sum(row.status == "DRIFT" for row in rows)
                missing = sum(row.status == "MISSING" for row in rows)
                print(
                    render_key_value_fields(
                        [
                            ("Agent:", first.agent_display_name),
                            ("Agent key:", first.agent_name),
                            ("Target dir:", str(first.target_path.parent)),
                            ("Format:", first.config_format),
                            (
                                "Subagents:",
                                f"{len(rows)} managed, {synced} synced, "
                                f"{drifted} drifted, {missing} missing",
                            ),
                        ]
                    )
                )
                print()
                print(render_agent_subagent_table(rows, use_unicode, use_color))
            else:
                print(
                    render_subagent_details(
                        rows,
                        str(aikito_dir / "subagents" / f"{rows[0].subagent_name}.md"),
                        use_unicode,
                        use_color,
                    )
                )
            return

        # 2. Raw file view when target is specified without --agent
        if target:
            subagent_file = resolve_subagent_target_for_command(
                aikito_dir, target, operation="show"
            )
            try:
                print(subagent_file.read_text(encoding="utf-8"), end="")
            except Exception as e:
                print(
                    f"[ERROR] Failed to read subagent file {subagent_file}: {e}",
                    file=sys.stderr,
                )
                sys.exit(1)
            return

        # 3. Default matrix view
        subagent_rows, orphan_files, agent_names = collect_subagents_matrix(
            aikito_dir=aikito_dir,
            home=home,
        )
        table_str = render_subagents_status_table(
            subagent_rows, orphan_files, agent_names, use_unicode, use_color
        )
        print(table_str)
    except SubagentConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)


def cmd_show_inbox(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    inbox_dir = get_inbox_path(aikito_dir)
    target = getattr(args, "target", None)

    if not target:
        if not inbox_dir.is_dir():
            print(f"Inbox directory does not exist: {inbox_dir}")
            return
        use_unicode, use_color = resolve_color_flags(args)
        rows = collect_inbox_rows(inbox_dir)
        if not rows:
            print(f"Inbox is empty ({inbox_dir}).")
            return
        table_str = render_inbox_table(rows, use_unicode, use_color)
        print(table_str)
        return

    target_file = resolve_inbox_target_for_command(inbox_dir, target, operation="show")

    try:
        print(target_file.read_text(encoding="utf-8"), end="")
    except Exception as e:
        print(
            f"[ERROR] Failed to read inbox note {target_file}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_show_memory(args: argparse.Namespace) -> None:
    aikito_dir = resolve_workspace(Path.home())
    home = Path.home()
    target = getattr(args, "target", None)
    project_arg = getattr(args, "project", None)
    show_all = getattr(args, "all", False)

    if project_arg is not None and show_all:
        print("[ERROR] Cannot specify both --project and --all.", file=sys.stderr)
        sys.exit(1)

    resolved_project = None
    if project_arg is not None:
        resolved_project = resolve_project_filter(
            aikito_dir=aikito_dir,
            project_target=project_arg,
            cwd=Path.cwd(),
            home=home,
        )

    if not target:
        use_unicode, use_color = resolve_color_flags(args)
        include_global = False
        scoped_project = resolved_project
        if resolved_project is None and not show_all:
            try:
                detected = detect_current_project(aikito_dir, Path.cwd(), home)
            except ProjectContextConflictError as exc:
                names = ", ".join(exc.projects)
                print(
                    f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if detected:
                print(f"[aikito] Target project: '{detected}' (detected from cwd)")
                scoped_project = detected
                include_global = True

        note_rows = collect_memory_notes_rows(
            aikito_dir=aikito_dir,
            home=home,
            project=scoped_project,
            include_global=include_global,
        )
        table_str = render_memory_notes_table(note_rows, use_unicode, use_color)
        print(table_str)
        return

    target_file = resolve_memory_target_for_command(
        aikito_dir, target, operation="show", project=resolved_project
    )

    try:
        print(target_file.read_text(encoding="utf-8"), end="")
    except Exception as e:
        print(
            f"[ERROR] Failed to read memory note {target_file}: {e}",
            file=sys.stderr,
        )
        sys.exit(1)
