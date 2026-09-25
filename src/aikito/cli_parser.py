"""Command line interface argument parsing and grammar definitions for Aikito."""

import argparse
import os
import sys
from typing import Any

from . import __version__


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


def _get_handlers(handlers: dict[str, Any] | None = None) -> dict[str, Any]:
    if handlers is not None:
        return handlers
    from . import cli

    return {
        name: getattr(cli, name)
        for name in (
            "cmd_add_mcp",
            "cmd_add_skill",
            "cmd_add_subagent",
            "cmd_adopt",
            "cmd_completion",
            "cmd_diff",
            "cmd_doctor",
            "cmd_edit_inbox",
            "cmd_edit_instructions",
            "cmd_edit_mcp",
            "cmd_edit_memory",
            "cmd_edit_skill",
            "cmd_edit_subagent",
            "cmd_git",
            "cmd_global_sync",
            "cmd_init",
            "cmd_init_project",
            "cmd_maintain_memory",
            "cmd_migrate_workspace_resources",
            "cmd_mcp_auth",
            "cmd_mcp_sync",
            "cmd_path_workspace",
            "cmd_project_sync",
            "cmd_rename_memory",
            "cmd_rm_inbox",
            "cmd_rm_mcp",
            "cmd_rm_memory",
            "cmd_rm_skill",
            "cmd_rm_subagent",
            "cmd_show_inbox",
            "cmd_show_instructions",
            "cmd_show_mcp",
            "cmd_show_memory",
            "cmd_show_project",
            "cmd_show_skill",
            "cmd_show_subagents",
            "cmd_status",
            "cmd_subagent_sync",
            "cmd_sync_all",
            "cmd_version",
            "cmd_web",
        )
    }


def build_parser(handlers: dict[str, Any] | None = None) -> argparse.ArgumentParser:
    h = _get_handlers(handlers)
    cmd_add_mcp = h["cmd_add_mcp"]
    cmd_add_skill = h["cmd_add_skill"]
    cmd_add_subagent = h["cmd_add_subagent"]
    cmd_adopt = h["cmd_adopt"]
    cmd_completion = h["cmd_completion"]
    cmd_diff = h["cmd_diff"]
    cmd_doctor = h["cmd_doctor"]
    cmd_edit_inbox = h["cmd_edit_inbox"]
    cmd_edit_instructions = h["cmd_edit_instructions"]
    cmd_edit_mcp = h["cmd_edit_mcp"]
    cmd_edit_memory = h["cmd_edit_memory"]
    cmd_edit_skill = h["cmd_edit_skill"]
    cmd_edit_subagent = h["cmd_edit_subagent"]
    cmd_git = h["cmd_git"]
    cmd_global_sync = h["cmd_global_sync"]
    cmd_init = h["cmd_init"]
    cmd_init_project = h["cmd_init_project"]
    cmd_maintain_memory = h["cmd_maintain_memory"]
    cmd_migrate_workspace_resources = h["cmd_migrate_workspace_resources"]
    cmd_mcp_auth = h["cmd_mcp_auth"]
    cmd_mcp_sync = h["cmd_mcp_sync"]
    cmd_path_workspace = h["cmd_path_workspace"]
    cmd_project_sync = h["cmd_project_sync"]
    cmd_rename_memory = h["cmd_rename_memory"]
    cmd_rm_inbox = h["cmd_rm_inbox"]
    cmd_rm_mcp = h["cmd_rm_mcp"]
    cmd_rm_memory = h["cmd_rm_memory"]
    cmd_rm_skill = h["cmd_rm_skill"]
    cmd_rm_subagent = h["cmd_rm_subagent"]
    cmd_show_inbox = h["cmd_show_inbox"]
    cmd_show_instructions = h["cmd_show_instructions"]
    cmd_show_mcp = h["cmd_show_mcp"]
    cmd_show_memory = h["cmd_show_memory"]
    cmd_show_project = h["cmd_show_project"]
    cmd_show_skill = h["cmd_show_skill"]
    cmd_show_subagents = h["cmd_show_subagents"]
    cmd_status = h["cmd_status"]
    cmd_subagent_sync = h["cmd_subagent_sync"]
    cmd_sync_all = h["cmd_sync_all"]
    cmd_version = h["cmd_version"]
    cmd_web = h["cmd_web"]
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

    p_migrate = subparsers.add_parser("migrate", help="Migrate workspace resources")
    migrate_subparsers = p_migrate.add_subparsers(dest="migrate_target", required=True)
    p_migrate_resources = migrate_subparsers.add_parser(
        "workspace-resources",
        help="Move Agent and subagent definitions to per-resource files",
    )
    p_migrate_resources.add_argument(
        "--dry-run", action="store_true", help="Preview migration without writing"
    )
    p_migrate_resources.set_defaults(func=cmd_migrate_workspace_resources)

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
