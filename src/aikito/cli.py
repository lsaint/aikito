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
from .agents import AgentRegistryError, load_agent_definitions
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
from .cli_parser import (
    build_parser,
    resolve_color_flags,
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
    GlobalSyncExecutionResult,
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
    sync_mcp_configs,
)
from .templating import TemplateError, detect_existing_agents
from .project import collect_project_summaries

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
    render_workspace_sync_plan,
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
from .update_notifier import check_and_notify_update, cmd_version as cmd_version
from .workspace import (
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)
from .workspace_layout import (
    WorkspaceLayoutError,
    apply_migration,
    build_migration_plan,
    migration_path_policy,
    require_current_layout,
)


def get_aikito_dir() -> Path:
    return resolve_workspace(Path.home())


def get_agents_dir() -> Path:
    return Path.home() / ".agents"


def cmd_migrate_workspace_resources(args: argparse.Namespace) -> None:
    """Preview or apply the required workspace resource layout migration."""
    if not args.dry_run:
        from .skill_state import WorkspaceWriterLock
        from .workspace_core import recover

        with WorkspaceWriterLock(Path.home()):
            if recover((get_aikito_dir(),), policy=migration_path_policy()):
                print("[RECOVER] Interrupted workspace migration recovered")
    plan = build_migration_plan(get_aikito_dir())
    for path, _content in plan.creates:
        print(f"[CREATE] {path}")
    for path, _content in plan.updates:
        print(f"[UPDATE] {path}")
    for path in plan.removes:
        print(f"[REMOVE] {path}")
    for finding in plan.findings:
        print(f"[BLOCKED] {finding}", file=sys.stderr)
    for note in plan.notes:
        print(f"[NOTE] {note}")
    if plan.blocked:
        sys.exit(1)
    if args.dry_run:
        print("[DRY RUN] No files changed")
    else:
        apply_migration(plan, Path.home())
        print("[SUCCESS] Workspace resource layout migrated")


def sync_global_resources(
    aikito_dir: Path,
    home: Path,
    *,
    dry_run: bool = False,
) -> GlobalSyncExecutionResult:
    """Synchronize global resources (skills and instructions) via workspace_sync."""
    container_path = get_agents_dir() / "skills"
    plan = build_global_sync_plan(
        aikito_dir,
        home,
        dry_run=dry_run,
        container_path=container_path,
        outdated_bundled_skills_fn=outdated_bundled_skills,
        load_agent_definitions_fn=load_agent_definitions,
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
            return GlobalSyncExecutionResult(
                success=False, error_message=plan.error_message
            )

        if plan.error_message in (
            "Global skills configuration not found.",
            "Global skills configuration is malformed.",
        ):
            return GlobalSyncExecutionResult(
                success=False, error_message=plan.error_message
            )

        if any(
            f.code in ("TOML_DECODE_ERROR", "MCP_CONFIG_ERROR") for f in plan.findings
        ):
            return GlobalSyncExecutionResult(
                success=False, error_message=plan.error_message
            )

        if plan.skill_plan:
            all_conflicts = plan.skill_plan.conflicts
            if all_conflicts:
                for op in all_conflicts:
                    prefix = (
                        "[ERROR]"
                        if op.rule_id in ("INV-TR-02", "INV-TR-04")
                        else "[CONFLICT]"
                    )
                    print(f"{prefix} {op.reason}", file=sys.stderr)
                print("[ERROR] Global synchronization aborted.", file=sys.stderr)
                return GlobalSyncExecutionResult(
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
    print(render_workspace_sync_plan(plan, verbose=verbose))
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
        home=home,
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
        root = get_aikito_dir()
        if is_recognized_workspace(root) or any(
            (root / name).exists()
            for name in ("agents.toml", "subagents.toml", "layout.toml")
        ):
            try:
                require_current_layout(root)
            except WorkspaceLayoutError:
                return
        try:
            items = get_candidates(args.category, root, getattr(args, "query", None))
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


def main() -> None:
    init_console_encoding()
    parser = build_parser()
    args = parser.parse_args()
    debug_mode = getattr(args, "debug", False) or os.environ.get("AIKITO_DEBUG") == "1"

    try:
        if args.command not in ("version", "path", "migrate", "completion") and not (
            args.command == "init" and getattr(args, "init_target", None) == "workspace"
        ):
            root = get_aikito_dir()
            if is_recognized_workspace(root) or any(
                (root / name).exists()
                for name in ("agents.toml", "subagents.toml", "layout.toml")
            ):
                require_current_layout(root)
        args.func(args)
    except (
        AgentRegistryError,
        MCPConfigError,
        SubagentConfigError,
        TemplateError,
        MemoryMaintenanceError,
        SkillTargetConflictError,
        InboxTargetConflictError,
        MemoryTargetConflictError,
        WorkspaceLayoutError,
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
