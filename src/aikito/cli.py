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
import tomllib
from collections import Counter
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
from .cli_show import (
    cmd_show_project as cmd_show_project,
    cmd_show_skill as cmd_show_skill,
    cmd_show_instructions as cmd_show_instructions,
    cmd_show_mcp as cmd_show_mcp,
    cmd_show_subagents as cmd_show_subagents,
    cmd_show_inbox as cmd_show_inbox,
    cmd_show_memory as cmd_show_memory,
)
from .diff import (
    collect_drift_diffs,
    filter_drift_diffs,
    render_drift_diffs,
    render_drift_index,
    render_project_drift_index,
)
from .doctor import run_doctor, run_doctor_fixes
from .global_skills import (
    execute_global_skills,
)
from .init import init_project, init_workspace, is_recognized_workspace
from .maintain import MemoryMaintenanceError, run_memory_maintenance
from .resolve import (
    ProjectContextConflictError,
    SkillTargetConflictError,
    detect_current_project,
    open_in_editor,
    resolve_instruction_target,
    resolve_mcp_target_for_command,
    resolve_skill_target_for_command,
    resolve_subagent_target_for_command,
)
from .project_sync import (
    sync_project,
)
from .workspace.sync import (
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
from .project_config import resolve_project_binding

from .config import get_inbox_path
from .inbox import (
    InboxTargetConflictError,
    remove_inbox_note,
    resolve_inbox_target_for_command,
)
from .render import (
    DoctorReport,
    render_doctor_report,
    render_status_report,
    render_workspace_sync_plan,
)
from .status import get_status_report_data
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
from .workspace.paths import (
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)
from .workspace.layout import (
    WorkspaceLayoutError,
    apply_migration,
    build_migration_plan,
    migration_path_policy,
    require_current_layout,
)
from .workspace.importing import (
    WorkspaceImportError,
    run_workspace_import,
)
from .workspace.resources import is_shared_resource


def get_aikito_dir() -> Path:
    return resolve_workspace(Path.home())


def get_agents_dir() -> Path:
    return Path.home() / ".agents"


def cmd_migrate_workspace_resources(args: argparse.Namespace) -> None:
    """Preview or apply the required workspace resource layout migration."""
    if not args.dry_run:
        from .skill_state import WorkspaceWriterLock
        from .workspace.transactions import recover

        with WorkspaceWriterLock(Path.home()):
            if recover((get_aikito_dir(),), policy=migration_path_policy()):
                print("[RECOVER] Interrupted workspace migration recovered")
    plan = build_migration_plan(get_aikito_dir(), home=Path.home())
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


def cmd_import_workspace(args: argparse.Namespace) -> None:
    """Preview or import canonical resources into the active workspace."""
    keep = set(getattr(args, "keep_target", ()))
    take = set(getattr(args, "take_source", ()))
    if keep & take:
        raise WorkspaceImportError(
            "Conflicting resolutions for: " + ", ".join(sorted(keep & take))
        )
    resolutions = {key: "target" for key in keep}
    resolutions.update((key, "source") for key in take)
    plan = run_workspace_import(
        args.source,
        get_aikito_dir(),
        Path.home(),
        dry_run=args.dry_run,
        resolutions=resolutions,
    )
    # Changes to one shared TOML file are shown as a single file-level line.
    grouped: dict[Path, list[str]] = {}
    for item in plan.changes:
        if is_shared_resource(item.resource.kind):
            grouped.setdefault(item.resource.relative_path, []).append(
                f"{item.resource.kind}:{item.resource.name}"
            )
    shown: set[Path] = set()
    counts: Counter[str] = Counter()
    for item in plan.items:
        if item.action == "NOOP":
            if args.verbose:
                print(
                    f"[NOOP] {item.resource.relative_path.as_posix()} "
                    f"({item.resource.kind}:{item.resource.name})"
                )
            continue
        path = item.resource.relative_path
        identity = f"{item.resource.kind}:{item.resource.name}"
        action = item.action
        names = [identity]
        if path in grouped and action in ("CREATE", "UPDATE"):
            if path in shown:
                continue
            shown.add(path)
            action = "CREATE" if path in plan.new_files else "UPDATE"
            names = grouped[path]
        detail = ""
        if action in ("CONFLICT", "BLOCKED"):
            detail = f" ({identity}): {item.reason}"
        elif args.verbose:
            detail = f" ({', '.join(names)})"
        counts[action] += 1
        print(f"[{action}] {path.as_posix()}{detail}")
    if args.verbose:
        for excluded in plan.excluded:
            print(f"[{excluded}]")
    for warning in plan.warnings:
        print(f"[WARNING] {warning.resource}: {warning.message}", file=sys.stderr)
    blocked_reasons = {item.reason for item in plan.items if item.action == "BLOCKED"}
    for finding in plan.findings:
        if finding not in blocked_reasons:
            print(f"[BLOCKED] {finding}", file=sys.stderr)
    summary = ", ".join(
        f"{action} {counts[action]}"
        for action in ("CREATE", "UPDATE", "CONFLICT", "BLOCKED")
    )
    print(f"[SUMMARY] {summary}, SKIPPED {len(plan.excluded)}")
    if plan.blocked:
        print(
            "[NEXT] Fix the blocking findings, then rerun the import command.",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.dry_run:
        print("[DRY RUN] No files changed")
    else:
        if plan.conflicts:
            message = (
                "Non-conflicting resources imported"
                if plan.changes
                else "No resources changed"
            )
            print(f"[PARTIAL] {message}; conflicts kept unchanged")
        else:
            print("[SUCCESS] Workspace resources imported")
        print("[NEXT] Run aikito sync --dry-run")
        for item in plan.creates:
            if item.resource.kind != "project":
                continue
            with (plan.target / item.resource.relative_path).open("rb") as stream:
                config = tomllib.load(stream)
            if not resolve_project_binding(config, Path.home()).active_entries:
                name = item.resource.name
                print(
                    f"[NEXT] Project {name} has no local code directory; after cloning, run aikito sync project {name} <path>"
                )
        print(
            "[NEXT] Review with aikito git status and aikito git diff, then commit with aikito git"
        )
    if plan.conflicts:
        print(
            "[NEXT] Resolve conflicts with --keep-target RESOURCE_ID or --take-source RESOURCE_ID, "
            "or edit the resources and retry. Source choices must pass reference and safety checks.",
            file=sys.stderr,
        )
        sys.exit(2)


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
    target = getattr(args, "diff_target", None)

    # 1. Full diff across all resources
    if getattr(args, "all", False):
        diffs = collect_drift_diffs(aikito_dir, home, project_filter=None)
        print(render_drift_diffs(diffs))
        return

    # 2. Targeted drill-down: project
    if target == "project":
        project_name = getattr(args, "project_name", None)
        if not project_name:
            try:
                detected = detect_current_project(aikito_dir, Path.cwd(), home)
            except ProjectContextConflictError as exc:
                names = ", ".join(exc.projects)
                print(
                    f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                    file=sys.stderr,
                )
                sys.exit(1)
            if not detected:
                print(
                    "[ERROR] No project specified and current directory is not inside any registered project.\n"
                    "Usage: aikito diff project <project> [skill] [file]",
                    file=sys.stderr,
                )
                sys.exit(1)
            print(f"[aikito] Target project: '{detected}' (detected from cwd)")
            project_name = detected

        skill_name = getattr(args, "skill_name", None)
        file_path = getattr(args, "file_path", None)

        diffs = collect_drift_diffs(
            aikito_dir, home, project_filter=project_name, kind="project_skill"
        )

        if file_path is not None:
            matching = filter_drift_diffs(
                diffs,
                kind="project_skill",
                project=project_name,
                name=skill_name,
                file=file_path,
            )
            print(
                render_drift_diffs(
                    matching, empty_message="No matching drift detected."
                )
            )
        elif skill_name is not None:
            matching = filter_drift_diffs(
                diffs,
                kind="project_skill",
                project=project_name,
                name=skill_name,
            )
            print(
                render_drift_diffs(
                    matching, empty_message="No matching drift detected."
                )
            )
        else:
            print(render_project_drift_index(project_name, diffs))
        return

    # 3. Targeted drill-down: mcp
    if target == "mcp":
        agent = getattr(args, "agent", None)
        server = getattr(args, "server", None)
        diffs = collect_drift_diffs(aikito_dir, home, kind="mcp")
        matching = filter_drift_diffs(
            diffs,
            kind="mcp",
            agent=agent,
            name=server,
        )
        print(render_drift_diffs(matching, empty_message="No matching drift detected."))
        return

    # 4. Targeted drill-down: subagent
    if target == "subagent":
        agent = getattr(args, "agent", None)
        name = getattr(args, "name", None)
        diffs = collect_drift_diffs(aikito_dir, home, kind="subagent")
        matching = filter_drift_diffs(
            diffs,
            kind="subagent",
            agent=agent,
            name=name,
        )
        print(render_drift_diffs(matching, empty_message="No matching drift detected."))
        return

    # 5. Default naked `aikito diff` -> global drift index
    diffs = collect_drift_diffs(aikito_dir, home, project_filter=None)
    print(render_drift_index(diffs))


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


def cmd_edit_mcp(args: argparse.Namespace) -> None:
    aikito_dir = get_aikito_dir()
    mcp_file = resolve_mcp_target_for_command(aikito_dir, args.target, operation="edit")
    open_in_editor(mcp_file)


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
        WorkspaceImportError,
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
