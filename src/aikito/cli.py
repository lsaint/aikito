#!/usr/bin/env python3
"""
Aikito - Agent Workspace Resource Management CLI Tool
Handles memory, skills, MCP configs, and .agents runtime directory synchronization.
Supports sync_mode: 'link' (symlinks) or 'copy' (file/directory copy).
"""

import argparse
import errno
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, List, Optional

from . import __version__
from .add import add_mcp, add_skill, add_subagent
from .remove import remove_mcp, remove_skill, remove_subagent
from .adopt import (
    apply_adopt_skips,
    build_adopt_plan,
    execute_adoption,
    summarize_adopt_plan,
)
from .bundled_skills import (
    outdated_bundled_skills,
    print_bundled_skill_notice,
)
from .diff import collect_drift_diffs, render_drift_diffs
from .doctor import run_doctor, run_doctor_fixes
from .global_skills import (
    execute_global_skills,
)
from .init import init_project, init_workspace, is_recognized_workspace
from .maintain import MemoryMaintenanceError, run_memory_maintenance
from .resolve import (
    ProjectContextConflictError,
    SkillTargetConflictError,
    collect_instruction_agent_status,
    collect_project_instruction_status,
    detect_current_project,
    open_in_editor,
    resolve_instruction_target,
    resolve_mcp_target_for_command,
    resolve_project_filter,
    resolve_skill_target_for_command,
    resolve_subagent_target_for_command,
)
from .project_sync import (
    sync_project,
)
from .workspace_sync import (
    GlobalSyncResult,
    build_global_sync_plan,
    build_workspace_sync_plan,
    execute_global_sync_plan,
    execute_workspace_sync_plan,
)
from .memory import (
    MemoryTargetConflictError,
    remove_memory_note,
    rename_memory_note,
    resolve_memory_target_for_command,
    validate_memory_name,
)
from .instructions import (
    execute_instruction_plan,
)
from .mcp import (
    MCPConfigError,
    authenticate_mcp,
    build_mcp_plan,
    load_agents,  # noqa: F401
    sync_mcp_configs,
)
from .templating import TemplateError, detect_existing_agents
from .project import (
    append_candidate_path_to_config,
    collect_project_summaries,
)

from .config import get_inbox_path
from .inbox import (
    InboxTargetConflictError,
    collect_inbox_rows,
    remove_inbox_note,
    resolve_inbox_target_for_command,
)
from .render import (
    DoctorReport,
    render_doctor_report,
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
    render_status_report,
    render_subagents_status_table,
)
from .status import (
    collect_mcp_details,
    collect_mcp_matrix,
    collect_mcp_runtime,
    collect_subagent_details,
    collect_memory_notes_rows,
    collect_skills_rows,
    collect_subagents_matrix,
    get_status_report_data,
)
from .subagent import (
    SubagentConfigError,
    build_subagent_plan,
    sync_subagent_configs,
)
from .compat import (
    init_console_encoding,
    require_symlink_support,
    resolve_executable,
)

from .web_console import serve_console

from .completion import (
    generate_bash,
    generate_fish,
    generate_powershell,
    generate_zsh,
    get_candidates,
)
from .update_notifier import check_and_notify_update, cmd_version
from .workspace import (
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)


def get_aikito_dir() -> Path:
    return resolve_workspace(Path.home())


def get_agents_dir() -> Path:
    return Path.home() / ".agents"


def add_color_args(p: argparse.ArgumentParser) -> None:
    """Add --color and --no-color flags to a subcommand parser."""
    p.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default="auto",
        help="Controls color and table formatting mode (default: auto)",
    )
    p.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colorized output (equivalent to NO_COLOR=1)",
    )


def resolve_color_flags(args: argparse.Namespace) -> tuple[bool, bool]:
    """Resolve (use_unicode, use_color) flags from CLI arguments and environment."""
    color_opt = getattr(args, "color", "auto")
    is_tty = sys.stdout.isatty() if color_opt == "auto" else (color_opt == "always")
    no_color = getattr(args, "no_color", False) or (
        os.environ.get("NO_COLOR") is not None and os.environ.get("NO_COLOR") != ""
    )
    use_unicode = is_tty
    use_color = is_tty and not no_color
    return use_unicode, use_color


def sync_global_resources(
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
) -> GlobalSyncResult:
    """Synchronize global resources (skills and instructions) via workspace_sync."""
    container_path = get_agents_dir() / "skills"
    plan = build_global_sync_plan(
        aikito_dir,
        home,
        dry_run=dry_run,
        container_path=container_path,
        outdated_bundled_skills_fn=outdated_bundled_skills,
        load_agents_fn=load_agents,
    )

    if not plan.can_apply:
        for finding in plan.findings:
            if finding.code in (
                "GLOBAL_SKILLS_MISSING",
                "TOML_DECODE_ERROR",
                "INVALID_CONFIG",
                "CONFLICT_MARKER",
                "MCP_CONFIG_ERROR",
            ):
                print(f"[ERROR] {finding.message}", file=sys.stderr)

        if plan.error_message in (
            "Conflict markers detected in skills.toml.",
            "Conflict markers detected in global resources.",
        ):
            print("[ERROR] Global synchronization aborted.", file=sys.stderr)
            return GlobalSyncResult(success=False, error_message=plan.error_message)

        if plan.error_message in (
            "Global skills configuration not found.",
            "Global skills configuration is malformed.",
        ):
            return GlobalSyncResult(success=False, error_message=plan.error_message)

        if any(
            f.code in ("TOML_DECODE_ERROR", "MCP_CONFIG_ERROR") for f in plan.findings
        ):
            return GlobalSyncResult(success=False, error_message=plan.error_message)

        if plan.skill_plan and plan.skill_plan.all_operations:
            all_conflicts = [
                op for op in plan.skill_plan.all_operations if op.action == "CONFLICT"
            ]
            if all_conflicts:
                for op in all_conflicts:
                    prefix = (
                        "[ERROR]"
                        if op.rule_id in ("INV-TR-02", "INV-TR-04")
                        else "[CONFLICT]"
                    )
                    print(f"{prefix} {op.reason}", file=sys.stderr)
                print("[ERROR] Global synchronization aborted.", file=sys.stderr)
                return GlobalSyncResult(
                    success=False,
                    error_message="Conflicts detected in global skill plan.",
                )

    res = execute_global_sync_plan(
        plan,
        aikito_dir,
        home,
        dry_run=dry_run,
        execute_global_skills_fn=execute_global_skills,
        execute_instruction_plan_fn=execute_instruction_plan,
    )

    global_instruction_source = aikito_dir / "global" / "AGENTS.md"
    if not global_instruction_source.is_file():
        print(
            f"[ERROR] Global instruction file not found: {global_instruction_source}",
            file=sys.stderr,
        )
        return res

    if plan.instruction_plan and plan.instruction_plan.conflicts:
        for op in plan.instruction_plan.conflicts:
            if op.rule_id == "INV-TR-02":
                print(
                    f"[ERROR] Global instruction file not found: {global_instruction_source}",
                    file=sys.stderr,
                )
            else:
                prefix = (
                    f"[CONFLICT] {op.resource_name} instructions:"
                    if op.resource_name
                    else "[CONFLICT]"
                )
                print(f"{prefix} {op.reason}", file=sys.stderr)
        print(
            "[ERROR] Global skills were synced successfully, but one or more Agent "
            "instruction runtime targets have conflicts.",
            file=sys.stderr,
        )
        return res

    if not res.success:
        if res.error_message:
            print(f"[ERROR] {res.error_message}", file=sys.stderr)
        if res.instruction_result and not res.instruction_result.success:
            print(
                "[ERROR] Global skills were synced successfully, but one or more "
                "Agent instruction runtime targets have conflicts.",
                file=sys.stderr,
            )
        elif res.skill_result and not res.skill_result.success:
            print(
                f"[ERROR] Global skill synchronization aborted: {res.skill_result.error_message or 'execution failed'}",
                file=sys.stderr,
            )
        return res

    if plan.skill_plan:
        valid_targets = tuple(
            str(s.path.name) for s in plan.skill_plan.batch.selected_entries
        )
        skill_consumer_count = sum(
            len(t.consumers) for t in plan.skill_plan.batch.consumers
        )
        consumer_count = (
            res.skill_result.consumer_target_count if res.skill_result else 0
        )
        print(
            f"[SUCCESS] Global resources synced successfully "
            f"({len(valid_targets)} skills, 1 instruction source, "
            f"{consumer_count} Agent skill entries across {skill_consumer_count} consumers)."
        )

    return res


def cmd_global_sync(args: argparse.Namespace) -> None:
    require_symlink_support()
    dry_run = getattr(args, "dry_run", False)
    if not sync_global_resources(get_aikito_dir(), Path.home(), dry_run=dry_run):
        sys.exit(1)


def sync_project_by_name(
    aikito_dir: Path,
    home: Path,
    project_name: str,
    project_path: Optional[str] = None,
    dry_run: bool = False,
    force: bool = False,
) -> bool:
    from .compat import require_symlink_support

    require_symlink_support()
    # Path registration is handled atomically by the CAS mechanism inside sync_project.
    # Do not write agent.toml here before preflight; doing so would bypass conflict
    # checks and cause the CAS to see the path as already registered (NOOP).
    return sync_project(
        aikito_dir,
        home,
        project_name,
        project_path=project_path,
        dry_run=dry_run,
        force=force,
        append_fn=append_candidate_path_to_config,
    )


def cmd_project_sync(args: argparse.Namespace) -> None:
    dry_run = getattr(args, "dry_run", False)
    force = getattr(args, "force", False)
    aikito_dir = get_aikito_dir()
    home = Path.home()

    raw_names = args.project_name
    if not raw_names or raw_names == ".":
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
            if not raw_names:
                print(
                    "Please specify a project name, e.g. 'aikito sync project <name>'",
                    file=sys.stderr,
                )
            sys.exit(1)
        if not raw_names:
            print(f"[aikito] Target project: '{detected}' (detected from cwd)")
        project_names = [detected]
    else:
        project_names = [p.strip() for p in raw_names.split(",") if p.strip()]

    if len(project_names) > 1 and args.project_path:
        print(
            "[ERROR] Cannot specify explicit project_path when syncing multiple projects.",
            file=sys.stderr,
        )
        sys.exit(1)

    for p in project_names:
        if not sync_project_by_name(
            aikito_dir,
            home,
            p,
            project_path=args.project_path if len(project_names) == 1 else None,
            dry_run=dry_run,
            force=force,
        ):
            sys.exit(1)


def _sync_project_active_entries(
    aikito_dir: Path,
    project_name: str,
    binding: Any,
    data: dict,
    home: Path,
    *,
    dry_run: bool,
    force: bool,
) -> bool:
    from .compat import require_symlink_support

    require_symlink_support()
    return sync_project(
        aikito_dir,
        home,
        project_name,
        dry_run=dry_run,
        force=force,
    )


def cmd_mcp_sync(args: argparse.Namespace) -> None:
    try:
        success = sync_mcp_configs(
            aikito_dir=get_aikito_dir(),
            home=Path.home(),
            dry_run=args.dry_run,
            force=args.force,
        )
    except MCPConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    if not success:
        sys.exit(1)


def cmd_mcp_auth(args: argparse.Namespace) -> None:
    try:
        success = authenticate_mcp(
            aikito_dir=get_aikito_dir(),
            home=Path.home(),
            agent=args.agent,
            server=args.server,
        )
    except MCPConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    if not success:
        sys.exit(1)


def cmd_subagent_sync(args: argparse.Namespace) -> None:
    try:
        success = sync_subagent_configs(
            aikito_dir=get_aikito_dir(),
            home=Path.home(),
            dry_run=args.dry_run,
            force_targets=args.force,
            prune=args.prune,
        )
    except SubagentConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    if not success:
        sys.exit(1)


def cmd_sync_all(args: argparse.Namespace) -> None:
    require_symlink_support()
    aikito_dir = get_aikito_dir()
    home = Path.home()
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)

    plan = build_workspace_sync_plan(
        aikito_dir,
        home=home,
        build_subagent_plan_fn=build_subagent_plan,
        build_mcp_plan_fn=build_mcp_plan,
    )
    print(plan.render(verbose=verbose))
    if not plan.can_apply:
        sys.exit(1)
    if dry_run:
        return

    res = execute_workspace_sync_plan(plan, aikito_dir, home=home, dry_run=False)
    if not res.success:
        if res.error_message:
            print(f"[ERROR] {res.error_message}", file=sys.stderr)
        sys.exit(1)
    print("\n[SUCCESS] Full workspace sync completed successfully.")


def cmd_status(args: argparse.Namespace) -> None:
    aikito_dir, workspace_source = resolve_workspace_with_source(Path.home())
    home = Path.home()
    use_unicode, use_color = resolve_color_flags(args)

    # Top-level Dashboard report
    report_data = get_status_report_data(aikito_dir, home)
    rendered = render_status_report(
        report_data,
        is_tty=use_unicode,
        no_color=not use_color,
        workspace=str(aikito_dir),
        workspace_source=workspace_source,
    )
    print(rendered)
    print_bundled_skill_notice(aikito_dir)


def cmd_web(args: argparse.Namespace) -> None:
    try:
        serve_console(
            get_aikito_dir(),
            Path.home(),
            __version__,
            port=args.port,
            open_browser=not args.no_open,
        )
    except OSError as exc:
        if exc.errno != errno.EADDRINUSE:
            raise
        print(f"[ERROR] Port {args.port} is already in use.", file=sys.stderr)
        print(
            f"Open the existing console at http://127.0.0.1:{args.port}, "
            "or run 'aikito web --port 0' to use a free port.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_diff(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    home = Path.home()
    project_filter = None
    if not getattr(args, "all", False):
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
            project_filter = detected

    diffs = collect_drift_diffs(aikito_dir, home, project_filter=project_filter)
    print(render_drift_diffs(diffs))


def cmd_init(args: argparse.Namespace) -> None:
    require_symlink_support()
    target = Path(args.workspace_path) if args.workspace_path else get_aikito_dir()
    existing_workspace = is_recognized_workspace(target.expanduser().resolve())
    home = Path.home()
    success = init_workspace(target, home, force=args.force)
    if not success:
        sys.exit(1)
    if args.workspace_path:
        pointer_path = persist_workspace(target, home)
        print(f"[CONFIG] Default workspace: {target.expanduser().resolve()}")
        print(f"[CONFIG] Workspace pointer: {pointer_path}")

    resolved_target = target.expanduser().resolve()
    adoption = summarize_adopt_plan(build_adopt_plan(resolved_target, home))
    if adoption.total_changes or adoption.conflicts or adoption.errors:
        print(
            "\nNext step: Run 'aikito adopt'. It checks the complete import "
            "plan before changing the workspace."
        )
    elif existing_workspace:
        print(
            "\nNext step: Run 'aikito doctor' to check this workspace against "
            "the Agents and paths available on this host."
        )
    elif detect_existing_agents(home):
        print(
            "\nNext step: Run 'aikito sync'. It checks the complete plan for "
            "conflicts before changing managed configuration on this host."
        )
    else:
        print(
            "\n[INFO] No supported Agents detected on this host. "
            "The workspace is ready; synchronization can wait until an Agent "
            "is installed."
        )


def cmd_path_workspace(args: argparse.Namespace) -> None:
    print(get_aikito_dir())


def cmd_git(args: argparse.Namespace) -> None:
    workspace = get_aikito_dir()
    if not workspace.exists():
        print(
            f"[ERROR] Workspace does not exist: {workspace}. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)
    if not (workspace / ".git").exists():
        print(
            f"[ERROR] Workspace '{workspace}' is not a Git repository. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    git_bin = shutil.which("git")
    if not git_bin:
        print("[ERROR] 'git' executable not found in PATH.", file=sys.stderr)
        sys.exit(1)

    git_args = list(getattr(args, "git_args", []) or [])
    if git_args and git_args[0] == "--":
        git_args = git_args[1:]

    command = resolve_executable([git_bin, "-C", str(workspace), *git_args])
    try:
        proc = subprocess.run(command)
        if proc.returncode != 0:
            sys.exit(proc.returncode)
    except KeyboardInterrupt:
        sys.exit(130)


def cmd_init_project(args: argparse.Namespace) -> None:
    require_symlink_support()
    project_path = Path(args.project_path) if args.project_path else Path.cwd()

    project_name = init_project(
        get_aikito_dir(),
        project_path,
        args.project_name,
        description=getattr(args, "description", None),
    )
    if not project_name:
        sys.exit(1)

    cmd_project_sync(
        argparse.Namespace(
            project_name=project_name,
            project_path=str(project_path),
        )
    )


def cmd_add_skill(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    home = Path.home()
    project_arg = getattr(args, "project", None)
    is_global = getattr(args, "is_global", False)

    if project_arg and is_global:
        print("[ERROR] Cannot specify both --project and --global.", file=sys.stderr)
        sys.exit(1)

    projects = None
    if project_arg:
        projects = [p.strip() for p in project_arg.split(",") if p.strip()]
    elif not is_global:
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
            projects = [detected]

    success = add_skill(
        aikito_dir=aikito_dir,
        home=home,
        name=args.name,
        description=getattr(args, "description", None),
        projects=projects,
        from_source=getattr(args, "from_source", None),
        sync=getattr(args, "sync", False),
        force=getattr(args, "force", False),
    )
    if not success:
        sys.exit(1)


def cmd_add_subagent(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    agents_list = None
    if getattr(args, "agents", None):
        agents_list = [a.strip() for a in args.agents.split(",") if a.strip()]
    success = add_subagent(
        aikito_dir=aikito_dir,
        home=Path.home(),
        name=args.name,
        description=getattr(args, "description", None),
        agents=agents_list,
        from_source=getattr(args, "from_source", None),
        sync=getattr(args, "sync", False),
        force=getattr(args, "force", False),
    )
    if not success:
        sys.exit(1)


def cmd_add_mcp(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    agents_list = None
    if getattr(args, "agents", None):
        agents_list = [a.strip() for a in args.agents.split(",") if a.strip()]
    success = add_mcp(
        aikito_dir=aikito_dir,
        home=Path.home(),
        name=getattr(args, "name", None),
        transport=getattr(args, "transport", None),
        command=getattr(args, "command", None),
        url=getattr(args, "url", None),
        agents=agents_list,
        from_source=getattr(args, "from_source", None),
        sync=getattr(args, "sync", False),
        force=getattr(args, "force", False),
    )
    if not success:
        sys.exit(1)


def cmd_show_project(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
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
        print(render_projects_table(projects, use_unicode, use_color))
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
    aikito_dir = get_aikito_dir()
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
    aikito_dir = get_aikito_dir()
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


def cmd_edit_instructions(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    home = Path.home()
    target = getattr(args, "target", None)
    if target is None:
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
        else:
            target = "global"

    _, instructions_path = resolve_instruction_target(
        aikito_dir, home, target, Path.cwd()
    )
    open_in_editor(instructions_path)


def cmd_edit_skill(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    skill_file = resolve_skill_target_for_command(
        aikito_dir, args.target, operation="edit"
    )
    open_in_editor(skill_file)


def cmd_edit_subagent(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    subagent_file = resolve_subagent_target_for_command(
        aikito_dir, args.target, operation="edit"
    )
    open_in_editor(subagent_file)


def cmd_show_mcp(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
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


def cmd_edit_mcp(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    mcp_file = resolve_mcp_target_for_command(aikito_dir, args.target, operation="edit")
    open_in_editor(mcp_file)


def cmd_show_subagents(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
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
    aikito_dir = get_aikito_dir()
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


def cmd_edit_inbox(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    inbox_dir = get_inbox_path(aikito_dir)
    target_file = resolve_inbox_target_for_command(
        inbox_dir, args.target, operation="edit"
    )
    open_in_editor(target_file)


def cmd_rm_inbox(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    inbox_dir = get_inbox_path(aikito_dir)
    op = "remove" if getattr(args, "remove_target", None) else "rm"
    target_file = resolve_inbox_target_for_command(inbox_dir, args.target, operation=op)

    try:
        rel = target_file.relative_to(inbox_dir)
        ident = rel.with_suffix("").as_posix()
    except ValueError:
        ident = target_file.stem

    try:
        remove_inbox_note(inbox_dir, target_file)
    except Exception as e:
        print(f"[ERROR] Failed to remove inbox note '{ident}': {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Removed inbox note '{ident}' ({target_file.name})")


def cmd_show_memory(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
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


def cmd_edit_memory(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    target_file = resolve_memory_target_for_command(
        aikito_dir, args.target, operation="edit"
    )
    open_in_editor(target_file)


def cmd_rename_memory(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    new_name = args.new_name

    err = validate_memory_name(new_name)
    if err:
        print(f"[ERROR] {err}", file=sys.stderr)
        sys.exit(1)

    target_file = resolve_memory_target_for_command(
        aikito_dir, args.target, operation="rename"
    )

    try:
        result = rename_memory_note(aikito_dir, target_file, new_name)
    except (ValueError, FileExistsError, UnicodeError, OSError) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Renamed memory note '{result.old_stem}' → '{result.new_stem}'")
    try:
        rel_new = result.new_path.relative_to(aikito_dir)
    except ValueError:
        rel_new = result.new_path
    print(f"  - File: {rel_new}")
    if result.refactored_notes:
        print(
            f"  - Updated inbound wikilinks in {len(result.refactored_notes)} file(s):"
        )
        for p in result.refactored_notes:
            try:
                rel = p.relative_to(aikito_dir)
            except ValueError:
                rel = p
            print(f"    * {rel}")
    else:
        print("  - No inbound wikilinks found in other notes.")


def cmd_rm_memory(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    target_file = resolve_memory_target_for_command(
        aikito_dir, args.target, operation="rm"
    )

    try:
        result = remove_memory_note(aikito_dir, target_file)
    except (
        ValueError,
        FileNotFoundError,
        UnicodeError,
        OSError,
    ) as e:
        print(f"[ERROR] {e}", file=sys.stderr)
        sys.exit(1)

    print(f"[OK] Removed memory note '{result.stem}' ({result.deleted_path.name})")
    if result.inbound_references:
        print(
            f"  - [WARN] {len(result.inbound_references)} inbound reference(s) still exist:"
        )
        for ref in result.inbound_references:
            try:
                rel = ref.note_path.relative_to(aikito_dir)
            except ValueError:
                rel = ref.note_path
            print(f"    * {rel}:{ref.line_number}: {ref.line_content}")
        print("    Please review and update the referencing notes if necessary.")
    else:
        print("  - No inbound references found.")


def cmd_rm_skill(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    project_arg = getattr(args, "project", None)
    projects = None
    if project_arg:
        projects = [p.strip() for p in project_arg.split(",") if p.strip()]
    success = remove_skill(
        aikito_dir=aikito_dir,
        home=Path.home(),
        name=args.name,
        projects=projects,
        force=getattr(args, "force", False),
        sync=getattr(args, "sync", False),
    )
    if not success:
        sys.exit(1)


def cmd_rm_subagent(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    success = remove_subagent(
        aikito_dir=aikito_dir,
        home=Path.home(),
        name=args.name,
        sync=getattr(args, "sync", False),
    )
    if not success:
        sys.exit(1)


def cmd_rm_mcp(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    success = remove_mcp(
        aikito_dir=aikito_dir,
        home=Path.home(),
        name=args.name,
        sync=getattr(args, "sync", False),
        force=getattr(args, "force", False),
    )
    if not success:
        sys.exit(1)


def cmd_adopt(args: argparse.Namespace) -> None:
    target = Path(args.target) if args.target else get_aikito_dir()
    home = Path.home()

    try:
        plan = apply_adopt_skips(build_adopt_plan(target, home), args.skip)
        success = execute_adoption(
            plan,
            dry_run=args.dry_run,
            verbose=args.verbose,
        )
        if not success:
            sys.exit(1)
    except Exception as exc:
        print(
            f"[ERROR] Failed to adopt local agent configurations: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_doctor(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    home = Path.home()

    use_unicode, use_color = resolve_color_flags(args)

    fixes: List[str] = []
    if getattr(args, "fix", False):
        fix_actions = run_doctor_fixes(aikito_dir, home)
        fixes.extend(fix_actions)
        if fix_actions and not getattr(args, "json", False):
            for fix_msg in fix_actions:
                print(f"[FIX] {fix_msg}")
            print()

    stale_days = getattr(args, "stale_days", None)

    progress_cb = None
    if not getattr(args, "json", False) and sys.stdout.isatty():

        def _progress(stage: Optional[str]) -> None:
            if stage is not None:
                if use_color:
                    line = f"Checking \033[36m{stage}...\033[0m"
                else:
                    line = f"Checking {stage}..."
                sys.stdout.write(f"\r{line}\033[K")
                sys.stdout.flush()
            else:
                sys.stdout.write("\r\033[K")
                sys.stdout.flush()

        progress_cb = _progress

    report = run_doctor(
        aikito_dir, home, stale_days=stale_days, on_progress=progress_cb
    )

    if getattr(args, "json", False):

        def _report_to_dict(r: DoctorReport) -> dict:
            return {
                "sections": [
                    {
                        "name": s.name,
                        "findings": [
                            {
                                "status": f.status,
                                "message": f.message,
                                "fix_hint": f.fix_hint,
                                "code": f.code,
                                "resource": f.resource,
                                "source": f.source,
                                "reason": f.reason,
                                "actions": [
                                    {"label": action.label, "command": action.command}
                                    for action in f.actions
                                ],
                            }
                            for f in s.findings
                        ],
                    }
                    for s in r.sections
                ],
                "fail_count": r.fail_count,
                "warn_count": r.warn_count,
                "fixes": fixes,
            }

        print(json.dumps(_report_to_dict(report), ensure_ascii=False, indent=2))
    else:
        print(render_doctor_report(report, is_tty=use_unicode, no_color=not use_color))
        print_bundled_skill_notice(aikito_dir)

    if report.fail_count > 0:
        sys.exit(1)


def cmd_completion(args: argparse.Namespace) -> None:
    """Handle 'aikito completion <shell>' and 'aikito completion candidates <category>'."""
    target = args.shell_or_sub
    if target == "candidates":
        try:
            items = get_candidates(
                args.category, get_aikito_dir(), getattr(args, "query", None)
            )
            for item in items:
                print(item)
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            sys.exit(1)
    else:
        parser = build_parser()
        if target == "zsh":
            print(generate_zsh(parser), end="")
        elif target == "bash":
            print(generate_bash(parser), end="")
        elif target == "fish":
            print(generate_fish(parser), end="")
        elif target == "powershell":
            print(generate_powershell(parser), end="")


def cmd_maintain_memory(args: argparse.Namespace) -> None:
    try:
        returncode = run_memory_maintenance(
            get_aikito_dir(), args.target, args.agent, Path.cwd()
        )
    except MemoryMaintenanceError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    if returncode != 0:
        sys.exit(returncode)


class AikitoSubParsersAction(argparse._SubParsersAction):
    """Subparsers action that falls back to 'help' for subparser description."""

    def add_parser(self, name: str, **kwargs: Any) -> argparse.ArgumentParser:
        if "description" not in kwargs and "help" in kwargs:
            kwargs["description"] = kwargs["help"]
        return super().add_parser(name, **kwargs)


class AikitoArgumentParser(argparse.ArgumentParser):
    """ArgumentParser registering AikitoSubParsersAction by default and formatting description first."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.register("action", "parsers", AikitoSubParsersAction)

    def format_help(self) -> str:
        formatter = self._get_formatter()
        if self.description:
            formatter.add_text(self.description)
        formatter.add_usage(self.usage, self._actions, self._mutually_exclusive_groups)
        for action_group in self._action_groups:
            formatter.start_section(action_group.title)
            formatter.add_text(action_group.description)
            formatter.add_arguments(action_group._group_actions)
            formatter.end_section()
        formatter.add_text(self.epilog)
        return formatter.format_help()


def build_parser() -> argparse.ArgumentParser:
    parser = AikitoArgumentParser(
        prog="aikito",
        description="Agent Workspace Resource Management & Synchronization CLI Tool",
    )
    parser.add_argument(
        "-v",
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
        help="Show program's version number and exit",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Print full Python traceback on unexpected errors",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # version
    p_version = subparsers.add_parser(
        "version",
        help="Print Aikito CLI version and check for updates",
    )
    p_version.add_argument(
        "-c",
        "--check",
        action="store_true",
        help="Check remote repository for latest available release",
    )
    p_version.add_argument(
        "--force",
        action="store_true",
        help="Bypass cache and force check remote release",
    )
    p_version.add_argument(
        "--json",
        action="store_true",
        help="Output version and update status in JSON format",
    )
    p_version.set_defaults(func=cmd_version)

    # path
    p_path = subparsers.add_parser(
        "path", help="Print resolved paths for scripts and Agent workflows"
    )
    path_subparsers = p_path.add_subparsers(dest="path_target", required=True)
    p_path_workspace = path_subparsers.add_parser(
        "workspace", help="Print the active workspace directory"
    )
    p_path_workspace.set_defaults(func=cmd_path_workspace)

    # git
    p_git = subparsers.add_parser(
        "git",
        help="Run git commands directly in the active Aikito workspace",
        description="Forward git commands and arguments directly to the active Aikito workspace.",
    )
    p_git.add_argument(
        "git_args",
        nargs=argparse.REMAINDER,
        metavar="[args...]",
        help="Arguments forwarded directly to git",
    )
    p_git.set_defaults(func=cmd_git)

    # init
    p_init = subparsers.add_parser("init", help="Initialize a workspace or project")
    init_subparsers = p_init.add_subparsers(dest="init_target", required=True)

    p_init_workspace = init_subparsers.add_parser(
        "workspace",
        help="Create an Aikito workspace skeleton and initialize Git",
    )
    p_init_workspace.add_argument(
        "workspace_path",
        nargs="?",
        default=None,
        help="Target directory (default: resolved active workspace)",
    )
    p_init_workspace.add_argument(
        "--force",
        action="store_true",
        help="Overwrite template files if they already exist",
    )
    p_init_workspace.set_defaults(func=cmd_init)

    p_init_project = init_subparsers.add_parser(
        "project",
        help="Register a code project and synchronize its .agents runtime",
    )
    p_init_project.add_argument(
        "project_name",
        nargs="?",
        default=None,
        help="Project name (default: project directory name)",
    )
    p_init_project.add_argument(
        "project_path",
        nargs="?",
        default=None,
        help="Code project directory (default: current directory)",
    )
    p_init_project.add_argument(
        "--description",
        help="Initial human-readable project description",
    )
    p_init_project.set_defaults(func=cmd_init_project)

    # add
    p_add = subparsers.add_parser(
        "add",
        help="Add a new managed canonical resource to Aikito",
    )
    add_subparsers = p_add.add_subparsers(dest="add_target", required=True)

    # add skill
    p_add_skill = add_subparsers.add_parser(
        "skill",
        help="Add a new canonical skill skeleton or import from external source, and register it",
    )
    p_add_skill.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name of the skill in kebab-case (inferred from --from if omitted)",
    )
    p_add_skill.add_argument(
        "--from",
        dest="from_source",
        default=None,
        help="Path to an external skill directory or markdown file to import",
    )
    p_add_skill.add_argument(
        "--description",
        default=None,
        help="Initial description for the skill",
    )
    p_add_skill.add_argument(
        "--project",
        default=None,
        help="Register skill under specific project(s) instead of globally (comma-separated for multiple projects)",
    )
    p_add_skill.add_argument(
        "--global",
        dest="is_global",
        action="store_true",
        default=False,
        help="Register skill globally even if invoked from inside a project directory",
    )
    p_add_skill.add_argument(
        "--sync",
        action="store_true",
        help="Automatically synchronize affected project(s) or global runtime after adding",
    )
    p_add_skill.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing canonical skill with the complete --from snapshot",
    )
    p_add_skill.set_defaults(func=cmd_add_skill)

    # add subagent
    p_add_subagent = add_subparsers.add_parser(
        "subagent",
        aliases=["subagents"],
        help="Add a new canonical subagent skeleton or import from external source, and register it",
    )
    p_add_subagent.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name of the subagent in kebab-case (inferred from --from if omitted)",
    )
    p_add_subagent.add_argument(
        "--from",
        dest="from_source",
        default=None,
        help="Path to an external markdown prompt file or directory to import",
    )
    p_add_subagent.add_argument(
        "--description",
        default=None,
        help="Description for the subagent (inferred from --from if omitted)",
    )
    p_add_subagent.add_argument(
        "--agents",
        default=None,
        help="Comma-separated list of target agent platforms (default: all configured)",
    )
    p_add_subagent.add_argument(
        "--sync",
        action="store_true",
        help="Immediately synchronize the subagent to configured agent runtimes",
    )
    p_add_subagent.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing subagent definition with the --from source",
    )
    p_add_subagent.set_defaults(func=cmd_add_subagent)

    # add mcp
    p_add_mcp = add_subparsers.add_parser(
        "mcp",
        help="Add a new canonical MCP server configuration or import from external source",
    )
    p_add_mcp.add_argument(
        "name",
        nargs="?",
        default=None,
        help="Name of the MCP server in kebab-case (inferred from --from if omitted)",
    )
    p_add_mcp.add_argument(
        "--from",
        dest="from_source",
        default=None,
        help="Path to an external configuration file (.json, .toml) or remote URL to import",
    )
    p_add_mcp.add_argument(
        "--transport",
        choices=["stdio", "remote"],
        default=None,
        help="MCP transport type (stdio or remote)",
    )
    p_add_mcp.add_argument(
        "--command",
        default=None,
        help="Command for stdio transport (default: npx)",
    )
    p_add_mcp.add_argument(
        "--url",
        default=None,
        help="URL for remote transport",
    )
    p_add_mcp.add_argument(
        "--agents",
        default=None,
        help="Comma-separated list of target agent platforms (default: all configured)",
    )
    p_add_mcp.add_argument(
        "--sync",
        action="store_true",
        help="Immediately synchronize the MCP server to configured agent runtimes",
    )
    p_add_mcp.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing canonical MCP server configuration (does not bypass agent conflict protections)",
    )
    p_add_mcp.set_defaults(func=cmd_add_mcp)

    # adopt
    p_adopt = subparsers.add_parser(
        "adopt",
        help="Adopt existing local agent configurations into the Aikito workspace",
    )
    p_adopt.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Target directory (default: resolved active workspace)",
    )
    p_adopt.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview adoption changes without modifying workspace files",
    )
    p_adopt.add_argument(
        "--verbose",
        action="store_true",
        help="Show every source and target in the adoption plan",
    )
    p_adopt.add_argument(
        "--skip",
        action="append",
        default=[],
        metavar="RESOURCE",
        help="Skip one explicit resource (instructions, mcp/<name>, or subagent/<name>); repeatable",
    )
    p_adopt.set_defaults(func=cmd_adopt)

    # status
    p_status = subparsers.add_parser(
        "status",
        help="Show top-level synchronization status dashboard",
    )

    add_color_args(p_status)
    p_status.set_defaults(func=cmd_status)

    p_web = subparsers.add_parser(
        "web",
        help="Start the read-only local Web Console",
    )
    p_web.add_argument(
        "--port",
        type=int,
        default=8765,
        help="Local port (default: 8765; use 0 to pick a free port)",
    )
    p_web.add_argument(
        "--no-open",
        action="store_true",
        help="Do not open the default browser",
    )
    p_web.set_defaults(func=cmd_web)

    # diff
    p_diff = subparsers.add_parser(
        "diff",
        help="Show unified diffs for all drifted managed resources",
    )
    p_diff.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Show diffs across all registered projects instead of scoping to the current project",
    )
    p_diff.set_defaults(func=cmd_diff)

    # maintain memory
    p_maintain = subparsers.add_parser(
        "maintain",
        help="Run proactive Agent-assisted maintenance workflows",
    )
    maintain_subparsers = p_maintain.add_subparsers(
        dest="maintain_target", required=True
    )
    p_maintain_memory = maintain_subparsers.add_parser(
        "memory",
        help="Review one complete memory scope and propose curated changes",
    )
    p_maintain_memory.add_argument(
        "target",
        nargs="?",
        default=".",
        help="global, a project name, or . for the current project (default: .)",
    )
    p_maintain_memory.add_argument(
        "--agent",
        default="codex",
        help="Configured Agent runner to launch (default: codex)",
    )
    p_maintain_memory.set_defaults(func=cmd_maintain_memory)

    # auth & drill-down subparsers
    p_auth = subparsers.add_parser(
        "auth",
        help="Authenticate with external services or platforms",
    )
    auth_subparsers = p_auth.add_subparsers(dest="auth_target", required=True)

    # auth mcp <agent> <server>
    p_auth_mcp = auth_subparsers.add_parser(
        "mcp",
        help="Authenticate an MCP server and always print its authorization URL",
    )
    p_auth_mcp.add_argument("agent", help="Agent name, for example codex or opencode")
    p_auth_mcp.add_argument("server", help="Canonical server name")
    p_auth_mcp.set_defaults(func=cmd_mcp_auth)

    # sync & drill-down subparsers
    p_sync = subparsers.add_parser(
        "sync",
        help="Synchronize global resources, project, MCP servers, or subagents",
    )
    p_sync.add_argument(
        "--dry-run",
        action="store_true",
        help="Report changes without modifying files or configurations",
    )
    p_sync.add_argument(
        "--verbose",
        action="store_true",
        help="Show every item and path in the workspace synchronization plan",
    )
    p_sync.set_defaults(func=cmd_sync_all)
    sync_subparsers = p_sync.add_subparsers(dest="sync_target", required=False)

    # sync global
    p_sync_global = sync_subparsers.add_parser(
        "global",
        help="Sync global skills and agent instruction links",
    )
    p_sync_global.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Report changes without modifying global runtime links",
    )
    p_sync_global.set_defaults(func=cmd_global_sync)

    # sync project <project_name> [project_path]
    p_sync_project = sync_subparsers.add_parser(
        "project",
        help="Sync project skills and memory to <project-path>/.agents/",
    )
    p_sync_project.add_argument(
        "project_name",
        nargs="?",
        default=None,
        help="Name of the project under <workspace>/projects/ (detected from cwd if omitted)",
    )
    p_sync_project.add_argument(
        "project_path",
        nargs="?",
        default=None,
        help="Path to the actual codebase directory (optional if already configured for the project)",
    )
    p_sync_project.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Report changes without modifying project runtime or workspace config",
    )
    p_sync_project.add_argument(
        "--force",
        action="store_true",
        help="Replace drifted copied skills after reviewing aikito diff",
    )
    p_sync_project.set_defaults(func=cmd_project_sync)

    # sync mcp
    p_sync_mcp = sync_subparsers.add_parser(
        "mcp",
        help="Safely synchronize managed MCP entries into agent configs",
    )
    p_sync_mcp.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Report changes without writing config or state files",
    )
    p_sync_mcp.add_argument(
        "--force",
        action="store_true",
        help="Replace conflicting managed entries after manual review",
    )
    p_sync_mcp.set_defaults(func=cmd_mcp_sync)

    # sync subagents (and alias subagent)
    p_sync_subagent = sync_subparsers.add_parser(
        "subagents",
        aliases=["subagent"],
        help="Synchronize canonical subagent definitions into target agent configs",
    )
    p_sync_subagent.add_argument(
        "--dry-run",
        action="store_true",
        default=argparse.SUPPRESS,
        help="Report changes without modifying files",
    )
    p_sync_subagent.add_argument(
        "--force",
        nargs="*",
        help="Force overwrite of specific conflict target(s), e.g. --force claude-code/verifier",
    )
    p_sync_subagent.add_argument(
        "--prune",
        action="store_true",
        help="Remove managed subagent files that are no longer defined",
    )
    p_sync_subagent.set_defaults(func=cmd_subagent_sync)

    # show & drill-down subparsers
    p_show = subparsers.add_parser(
        "show",
        help="Display managed resources, matrices, and note/skill content",
    )
    show_subparsers = p_show.add_subparsers(dest="show_target", required=True)

    # show mcp
    p_show_mcp = show_subparsers.add_parser(
        "mcp",
        aliases=["mcps"],
        help="Inspect managed MCP server configs across agents, or print one MCP server's config",
    )
    p_show_mcp.add_argument(
        "target",
        nargs="?",
        help="MCP server name or unique prefix to view its config file",
    )
    p_show_mcp.add_argument(
        "--agent",
        nargs="?",
        const=True,
        default=None,
        help="Show MCP details across all agents, or narrow to a specific agent",
    )
    p_show_mcp.add_argument(
        "--live",
        action="store_true",
        help="Run live checks, or discover tools for a targeted MCP server",
    )
    add_color_args(p_show_mcp)
    p_show_mcp.set_defaults(func=cmd_show_mcp)

    # show subagents (and alias subagent)
    p_show_subagents = show_subparsers.add_parser(
        "subagents",
        aliases=["subagent"],
        help="Inspect subagent definitions across agents, or print one subagent's instructions",
    )
    p_show_subagents.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Exact subagent name or unique prefix (e.g. verifier, jira)",
    )
    p_show_subagents.add_argument(
        "--agent",
        nargs="?",
        const=True,
        default=None,
        help="Show subagent details across all agents, or narrow to a specific agent",
    )
    add_color_args(p_show_subagents)
    p_show_subagents.set_defaults(func=cmd_show_subagents)

    # show instructions [target]
    p_show_instructions = show_subparsers.add_parser(
        "instructions",
        help="List global and project instructions, or print one target",
    )
    p_show_instructions.add_argument(
        "target",
        nargs="?",
        default=None,
        help="global, a project name, or . for the current project",
    )
    add_color_args(p_show_instructions)
    p_show_instructions.set_defaults(func=cmd_show_instructions)

    # show inbox [target]
    p_show_inbox = show_subparsers.add_parser(
        "inbox",
        help="Print raw markdown content of an inbox note, or list all inbox notes if target is omitted",
    )
    p_show_inbox.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Exact name or unique prefix of the inbox note",
    )
    add_color_args(p_show_inbox)
    p_show_inbox.set_defaults(func=cmd_show_inbox)

    # show memory [target]
    p_show_memory = show_subparsers.add_parser(
        "memory",
        help="Print raw markdown content of a memory note, or list all memory notes if target is omitted",
    )
    p_show_memory.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Exact name, unique prefix, or path of the memory note (e.g. simplified-clean, global/example)",
    )
    g_memory_scope = p_show_memory.add_mutually_exclusive_group()
    g_memory_scope.add_argument(
        "--project",
        nargs="?",
        const=".",
        default=None,
        help="Filter memory notes to a specific project (by name, prefix, or '.' for current directory)",
    )
    g_memory_scope.add_argument(
        "--all",
        action="store_true",
        default=False,
        help="Show memory notes across all projects and global scope even inside a project directory",
    )
    add_color_args(p_show_memory)
    p_show_memory.set_defaults(func=cmd_show_memory)

    # show project [target]
    p_show_project = show_subparsers.add_parser(
        "project",
        aliases=["projects"],
        help="List registered projects or inspect one project's resources",
    )
    p_show_project.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Exact project name or unique prefix",
    )
    add_color_args(p_show_project)
    p_show_project.set_defaults(func=cmd_show_project)

    # show skill [target]
    p_show_skill = show_subparsers.add_parser(
        "skill",
        aliases=["skills"],
        help="Print raw SKILL.md content of a registered or project skill, or list all skills if target is omitted",
    )
    p_show_skill.add_argument(
        "target",
        nargs="?",
        default=None,
        help="Exact name or unique prefix of the skill (e.g. durable-memory, dur)",
    )
    add_color_args(p_show_skill)
    p_show_skill.set_defaults(func=cmd_show_skill)

    # edit & drill-down subparsers
    p_edit = subparsers.add_parser(
        "edit",
        help="Edit managed resources with external editor",
    )
    edit_subparsers = p_edit.add_subparsers(dest="edit_target", required=True)

    # edit memory <target>
    p_edit_memory = edit_subparsers.add_parser(
        "memory",
        help="Open a global or project memory file in your configured editor",
    )
    p_edit_memory.add_argument(
        "target",
        help="Exact name, unique prefix, or path of the memory note (e.g. simplified-clean, global/example)",
    )
    p_edit_memory.set_defaults(func=cmd_edit_memory)

    # edit inbox <target>
    p_edit_inbox = edit_subparsers.add_parser(
        "inbox",
        help="Open an inbox note in your configured editor",
    )
    p_edit_inbox.add_argument(
        "target",
        help="Exact name or unique prefix of the inbox note",
    )
    p_edit_inbox.set_defaults(func=cmd_edit_inbox)

    # edit instructions <target>
    p_edit_instructions = edit_subparsers.add_parser(
        "instructions",
        help="Open global or project instructions in your configured editor",
    )
    p_edit_instructions.add_argument(
        "target",
        nargs="?",
        default=None,
        help="global, a project name, or . for the current project (detected from cwd if omitted)",
    )
    p_edit_instructions.set_defaults(func=cmd_edit_instructions)

    # edit skill <target>
    p_edit_skill = edit_subparsers.add_parser(
        "skill",
        aliases=["skills"],
        help="Open a registered or project skill's SKILL.md file in your configured editor",
    )
    p_edit_skill.add_argument(
        "target",
        help="Exact name or unique prefix of the skill (e.g. durable-memory, dur)",
    )
    p_edit_skill.set_defaults(func=cmd_edit_skill)

    # edit subagent <target>
    p_edit_subagent = edit_subparsers.add_parser(
        "subagent",
        aliases=["subagents"],
        help="Open a subagent's instruction markdown file in your configured editor",
    )
    p_edit_subagent.add_argument(
        "target",
        help="Exact name or unique prefix of the subagent (e.g. verifier, jira)",
    )
    p_edit_subagent.set_defaults(func=cmd_edit_subagent)

    # edit mcp <target>
    p_edit_mcp = edit_subparsers.add_parser(
        "mcp",
        aliases=["mcps"],
        help="Open an MCP server configuration file in your configured editor",
    )
    p_edit_mcp.add_argument(
        "target",
        help="Exact name or unique prefix of the MCP server (e.g. atlassian-rovo, atl)",
    )
    p_edit_mcp.set_defaults(func=cmd_edit_mcp)

    # rename
    p_rename = subparsers.add_parser(
        "rename",
        help="Rename a managed canonical resource",
    )
    rename_subparsers = p_rename.add_subparsers(dest="rename_target", required=True)

    p_rename_memory = rename_subparsers.add_parser(
        "memory",
        help="Atomically rename a memory note and refactor inbound wikilinks",
    )
    p_rename_memory.add_argument(
        "target",
        help="Exact name, unique prefix, or path of the memory note to rename",
    )
    p_rename_memory.add_argument(
        "new_name",
        help="New kebab-case name for the note (at most 50 characters)",
    )
    p_rename_memory.set_defaults(func=cmd_rename_memory)

    # rm & remove
    for cmd_name in ("rm", "remove"):
        p_rm = subparsers.add_parser(
            cmd_name,
            help=f"{'Remove' if cmd_name == 'remove' else 'Delete'} a managed canonical resource",
        )
        rm_subparsers = p_rm.add_subparsers(dest=f"{cmd_name}_target", required=True)
        p_rm_skill = rm_subparsers.add_parser(
            "skill",
            aliases=["skills"],
            help="Remove a skill globally or unregister it from specific project(s)",
        )
        p_rm_skill.add_argument(
            "name",
            help="Name of the skill to remove or unregister",
        )
        p_rm_skill.add_argument(
            "--project",
            default=None,
            help="Unregister skill from specific project(s) instead of globally (comma-separated)",
        )
        p_rm_skill.add_argument(
            "--sync",
            action="store_true",
            help="Automatically synchronize affected project(s) or global runtime after removing",
        )
        p_rm_skill.add_argument(
            "--force",
            action="store_true",
            help="Force global removal even if referenced by projects (unregisters from all referencing projects)",
        )
        p_rm_skill.set_defaults(func=cmd_rm_skill)

        p_rm_subagent = rm_subparsers.add_parser(
            "subagent",
            aliases=["subagents"],
            help="Remove a subagent and unregister it from workspace",
        )
        p_rm_subagent.add_argument(
            "name",
            help="Name of the subagent to remove",
        )
        p_rm_subagent.add_argument(
            "--sync",
            action="store_true",
            help="Automatically synchronize and prune subagent from target agent platforms after removing",
        )
        p_rm_subagent.set_defaults(func=cmd_rm_subagent)

        p_rm_mcp = rm_subparsers.add_parser(
            "mcp",
            aliases=["mcps"],
            help="Remove a canonical MCP server configuration from workspace",
        )
        p_rm_mcp.add_argument(
            "name",
            help="Name of the MCP server configuration to remove",
        )
        p_rm_mcp.add_argument(
            "--sync",
            action="store_true",
            help="Automatically synchronize and remove MCP server from target agent platforms after removing",
        )
        p_rm_mcp.add_argument(
            "--force",
            action="store_true",
            help="Force removal from target agents even if config was modified outside aikito",
        )
        p_rm_mcp.set_defaults(func=cmd_rm_mcp)

        p_rm_memory = rm_subparsers.add_parser(
            "memory",
            help="Remove a memory note and check for inbound wikilinks",
        )
        p_rm_memory.add_argument(
            "target",
            help="Exact name, unique prefix, or path of the memory note to remove",
        )
        p_rm_memory.set_defaults(func=cmd_rm_memory)

        p_rm_inbox = rm_subparsers.add_parser(
            "inbox",
            help="Remove an inbox note file",
        )
        p_rm_inbox.add_argument(
            "target",
            help="Exact name or unique prefix of the inbox note to remove",
        )
        p_rm_inbox.set_defaults(func=cmd_rm_inbox)

    # doctor
    p_doctor = subparsers.add_parser(
        "doctor",
        help="Run deep workspace diagnostics (broken links, orphans, drift, permissions)",
    )
    add_color_args(p_doctor)
    p_doctor.add_argument(
        "--json",
        action="store_true",
        help="Output structured JSON report instead of human-readable text",
    )
    p_doctor.add_argument(
        "--fix",
        action="store_true",
        help="Automatically repair fixable issues",
    )
    p_doctor.add_argument(
        "--stale-days",
        "--memory-stale-days",
        type=int,
        default=None,
        dest="stale_days",
        help="Override memory freshness stale threshold in days (default: read from config or 30)",
    )
    p_doctor.set_defaults(func=cmd_doctor)

    # completion
    p_completion = subparsers.add_parser(
        "completion",
        help="Output shell completion script or list dynamic candidates",
    )
    completion_subparsers = p_completion.add_subparsers(
        dest="shell_or_sub", required=True
    )

    for _shell in ("zsh", "bash", "fish", "powershell"):
        _p = completion_subparsers.add_parser(
            _shell,
            help=f"Output {_shell.capitalize()} completion script",
        )
        _p.set_defaults(func=cmd_completion, shell_or_sub=_shell)

    p_completion_candidates = completion_subparsers.add_parser(
        "candidates",
        help="List dynamic completion candidates for a given category",
    )
    p_completion_candidates.add_argument(
        "category",
        choices=[
            "projects",
            "skills",
            "subagents",
            "mcps",
            "mcp",
            "memories",
            "memory-completions",
            "inbox",
            "inbox-completions",
            "paths",
        ],
        help="Category of candidates to list",
    )
    p_completion_candidates.add_argument(
        "query",
        nargs="?",
        default=None,
        help="Optional basename prefix used by path completion",
    )
    p_completion_candidates.set_defaults(func=cmd_completion, shell_or_sub="candidates")

    return parser


def main() -> None:
    init_console_encoding()
    parser = build_parser()
    args = parser.parse_args()
    debug_mode = getattr(args, "debug", False) or os.environ.get("AIKITO_DEBUG") == "1"

    try:
        args.func(args)
    except (
        MCPConfigError,
        SubagentConfigError,
        TemplateError,
        MemoryMaintenanceError,
        SkillTargetConflictError,
        InboxTargetConflictError,
        MemoryTargetConflictError,
    ) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INFO] Operation aborted by user.", file=sys.stderr)
        sys.exit(130)
    except BrokenPipeError:
        try:
            devnull = os.open(os.devnull, os.O_WRONLY)
            os.dup2(devnull, sys.stdout.fileno())
        except Exception:
            pass
        sys.exit(0)
    except Exception as exc:
        if debug_mode:
            raise
        print(f"[ERROR] An unexpected error occurred: {exc}", file=sys.stderr)
        print(
            "Hint: Run with AIKITO_DEBUG=1 to see the full traceback.", file=sys.stderr
        )
        sys.exit(1)

    try:
        check_and_notify_update(
            aikito_dir=get_aikito_dir(),
            command=getattr(args, "command", None),
            args=args,
        )
    except Exception:
        pass


if __name__ == "__main__":
    main()
