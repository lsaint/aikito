"""
Shell completion support for the Aikito CLI.

Provides:
- Reflection-based completion script generators for Zsh, Bash, and Fish shells.
- Lightweight dynamic candidate listing (projects, skills, memories).

Design principle: stdlib-only, no third-party dependencies.
"""

from __future__ import annotations

import argparse
import os
import tomllib
from pathlib import Path
from typing import List


from aikito_config import get_inbox_path
from aikito_inbox import find_inbox_files
from aikito_memory import find_memory_files
from aikito_status import collect_skills_rows


# ---------------------------------------------------------------------------
# Dynamic candidate helpers
# ---------------------------------------------------------------------------


def list_projects(aikito_dir: Path) -> List[str]:
    """Return sorted list of project names found in <aikito_dir>/projects/."""
    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        return []
    return sorted(
        p.name
        for p in projects_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def list_skills(aikito_dir: Path) -> List[str]:
    """Return sorted list of skill names collected from workspace and project configs."""
    rows = collect_skills_rows(aikito_dir)
    return sorted(set(r.skill_name for r in rows))


def list_subagents(aikito_dir: Path) -> List[str]:
    """Return sorted list of subagent names defined in subagents.toml or subagents/."""
    subagents_toml = aikito_dir / "subagents.toml"
    names: set[str] = set()
    if subagents_toml.is_file():
        try:
            data = tomllib.loads(subagents_toml.read_text(encoding="utf-8"))
            sub_table = data.get("subagents", {})
            if isinstance(sub_table, dict):
                names.update(sub_table.keys())
        except (OSError, tomllib.TOMLDecodeError):
            pass
    subagents_dir = aikito_dir / "subagents"
    if subagents_dir.is_dir():
        for p in subagents_dir.glob("*.md"):
            if p.is_file() and not p.name.startswith("."):
                names.add(p.stem)
    return sorted(names)


def list_mcps(aikito_dir: Path) -> List[str]:
    """Return sorted list of MCP server names defined in mcps/*.toml."""
    mcps_dir = aikito_dir / "mcps"
    names: set[str] = set()
    if mcps_dir.is_dir():
        for p in mcps_dir.glob("*.toml"):
            if p.is_file() and not p.name.startswith("."):
                names.add(p.stem)
    return sorted(names)


def list_memories(aikito_dir: Path) -> List[str]:
    """Return sorted list of memory identifiers (stem, short_identifier, full_identifier)."""
    items = find_memory_files(aikito_dir)
    cands: set[str] = set()
    for item in items:
        if item.stem != "index":
            cands.add(item.stem)
        cands.add(item.short_identifier)
        cands.add(item.full_identifier)
    return sorted(cands)


def list_memory_completions(aikito_dir: Path) -> List[str]:
    """Return one executable candidate and display label per memory note."""
    items = find_memory_files(aikito_dir)
    stem_counts: dict[str, int] = {}
    short_counts: dict[str, int] = {}
    for item in items:
        stem_counts[item.stem] = stem_counts.get(item.stem, 0) + 1
        short_counts[item.short_identifier] = (
            short_counts.get(item.short_identifier, 0) + 1
        )

    completions = []
    for item in items:
        if item.stem != "index" and stem_counts[item.stem] == 1:
            candidate = item.stem
        elif short_counts[item.short_identifier] == 1:
            candidate = item.short_identifier
        else:
            candidate = item.full_identifier
        completions.append(f"{candidate}\t({item.scope})")
    return sorted(set(completions))


def list_inbox_completions(aikito_dir: Path) -> List[str]:
    """Return candidates for inbox notes."""
    inbox_dir = get_inbox_path(aikito_dir)
    files = find_inbox_files(inbox_dir)
    completions = []
    for f in files:
        try:
            rel = f.relative_to(inbox_dir)
            ident = str(rel.with_suffix(""))
        except ValueError:
            ident = f.stem
        completions.append(ident)
    return sorted(set(completions))


def _registered_search_roots(aikito_dir: Path) -> List[Path]:
    """Return existing workspace and registered project roots without duplicates."""
    roots = [aikito_dir.resolve()]
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for config_path in sorted(projects_dir.glob("*/agent.toml")):
            try:
                config = tomllib.loads(config_path.read_text(encoding="utf-8"))
            except (OSError, tomllib.TOMLDecodeError):
                continue
            raw_path = config.get("path")
            if not isinstance(raw_path, str) or not raw_path:
                continue
            project_path = Path(raw_path).expanduser().resolve()
            if project_path.is_dir() and project_path not in roots:
                roots.append(project_path)
    return roots


def list_paths(aikito_dir: Path, prefix: str) -> List[str]:
    """Find basename-prefix matches below registered projects and the workspace."""
    prefix = prefix.strip()
    if not prefix or "/" in prefix:
        return []

    ignored_dirs = {
        ".git",
        ".hg",
        ".svn",
        ".tox",
        ".venv",
        "__pycache__",
        "node_modules",
        "target",
        "vendor",
    }
    matches: set[str] = set()
    for root in _registered_search_roots(aikito_dir):
        for current, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                name
                for name in dirnames
                if name not in ignored_dirs and not name.startswith(".")
            ]
            for name in (*dirnames, *filenames):
                if name.startswith(prefix):
                    matches.add(str((Path(current) / name).resolve()))
                    if len(matches) >= 100:
                        return sorted(matches)
    return sorted(matches)


def get_candidates(
    category: str, aikito_dir: Path, query: str | None = None
) -> List[str]:
    """Dispatch to the correct candidate lister by category name."""
    dispatch = {
        "projects": list_projects,
        "skills": list_skills,
        "subagents": list_subagents,
        "mcps": list_mcps,
        "mcp": list_mcps,
        "memories": list_memories,
        "memory-completions": list_memory_completions,
        "inbox": list_inbox_completions,
        "inbox-completions": list_inbox_completions,
    }
    if category == "paths":
        return list_paths(aikito_dir, query or "")
    fn = dispatch.get(category)
    if fn is None:
        raise ValueError(
            f"Unknown candidate category: {category!r}. "
            f"Valid categories: {', '.join(sorted(dispatch))}"
        )
    return fn(aikito_dir)


# ---------------------------------------------------------------------------
# Shell completion script generators (reflection-based from ArgumentParser)
# ---------------------------------------------------------------------------


def extract_cli_schema(parser: argparse.ArgumentParser) -> dict:
    """Reflect command structure, aliases, and flags from ArgumentParser."""

    def get_flags(p: argparse.ArgumentParser) -> List[str]:
        flags = []
        for action in p._actions:
            for opt in action.option_strings:
                flags.append(opt)
        return sorted(flags)

    def get_subparser_action(p: argparse.ArgumentParser):
        for action in p._actions:
            if isinstance(action, argparse._SubParsersAction):
                return action
        return None

    top_flags = get_flags(parser)
    commands: dict[str, dict] = {}

    sub_action = get_subparser_action(parser)
    if sub_action:
        for cmd_name, subp in sub_action.choices.items():
            cmd_flags = get_flags(subp)
            sub_commands: dict[str, dict] = {}
            child_sub_action = get_subparser_action(subp)
            if child_sub_action:
                for sub_name, child_p in child_sub_action.choices.items():
                    sub_commands[sub_name] = {
                        "flags": get_flags(child_p),
                    }
            commands[cmd_name] = {
                "flags": cmd_flags,
                "subcommands": sub_commands,
            }

    return {
        "flags": top_flags,
        "commands": commands,
    }


def _get_schema(parser: argparse.ArgumentParser | None = None) -> dict:
    """Retrieve CLI schema from provided parser or by reflecting build_parser()."""
    if parser is not None:
        return extract_cli_schema(parser)

    try:
        from aikito_cli_loader import load_cli

        return extract_cli_schema(load_cli().build_parser())
    except Exception as exc:
        raise RuntimeError(
            "Failed to load ArgumentParser for shell completion generation."
        ) from exc


def generate_zsh(parser: argparse.ArgumentParser | None = None) -> str:
    """Generate a Zsh completion script for aikito using reflected parser schema."""
    schema = _get_schema(parser)
    cmds = " ".join(sorted(schema["commands"].keys()))
    top_flags = " ".join(sorted(schema["flags"]))

    cmd_sub_cases = []
    cmd_flag_cases = []
    sub_flag_cases = []

    for cmd, data in sorted(schema["commands"].items()):
        subs = sorted(data["subcommands"].keys())
        if subs:
            cmd_sub_cases.append(f"                ({cmd}) compadd {' '.join(subs)} ;;")
        flags = data["flags"]
        if flags:
            cmd_flag_cases.append(
                f"                ({cmd}) compadd {' '.join(flags)} ;;"
            )

        for sub, sub_data in sorted(data["subcommands"].items()):
            s_flags = sub_data["flags"]
            if s_flags:
                sub_flag_cases.append(
                    f"                ({cmd} {sub}) compadd {' '.join(s_flags)} ;;"
                )

    cmd_subs_str = "\n".join(cmd_sub_cases)
    cmd_flags_str = "\n".join(cmd_flag_cases)
    sub_flags_str = "\n".join(sub_flag_cases)

    return f"""#compdef aikito
# Aikito shell completion for Zsh.
#
# Installation:
#   - Via fpath (recommended): Save as _aikito in a directory on $fpath (e.g. ~/.zsh/completion/_aikito)
#   - Via eval:                Add to ~/.zshrc:  eval "$(aikito completion zsh)"

_aikito() {{
    local -a commands top_flags
    commands=({cmds})
    top_flags=({top_flags})

    local cur="${{words[CURRENT]}}"

    case $CURRENT in
        2)
            if [[ $cur == -* ]]; then
                compadd -a top_flags
            else
                compadd -a commands
            fi
            ;;
        3)
            local cmd="${{words[2]}}"
            if [[ $cur == -* ]]; then
                case $cmd in
{cmd_flags_str}
                esac
            else
                case $cmd in
{cmd_subs_str}
                    (adopt|init)
                        _files -/
                        if [[ -n $cur && $cur != */* ]]; then
                            local -a cands
                            cands=(${{(f)"$(aikito completion candidates paths "$cur" 2>/dev/null)"}})
                            (( ${{#cands}} )) && compadd -U -a cands
                        fi
                        ;;
                esac
            fi
            ;;
        4)
            local cmd="${{words[2]}}"
            local sub="${{words[3]}}"
            if [[ $cur == -* ]]; then
                case "$cmd $sub" in
{sub_flags_str}
                esac
            else
                case "$cmd $sub" in
                    (show\\ memory|edit\\ memory|rename\\ memory|rm\\ memory|remove\\ memory)
                        local line
                        local -a cands displays
                        for line in ${{(f)"$(aikito completion candidates memory-completions 2>/dev/null)"}}; do
                            cands+=("${{line%%$'\\t'*}}")
                            displays+=("${{line%%$'\\t'*}}${{line#*$'\\t'}}")
                        done
                        compadd -d displays -a cands
                        ;;
                    (show\\ inbox|edit\\ inbox|rm\\ inbox|remove\\ inbox)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates inbox-completions 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ skill|show\\ skills|edit\\ skill|edit\\ skills)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates skills 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ subagent|show\\ subagents|edit\\ subagent|edit\\ subagents)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates subagents 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ mcp|show\\ mcps|edit\\ mcp|edit\\ mcps)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates mcps 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ instructions|edit\\ instructions|maintain\\ memory)
                        local cands
                        cands=(global . ${{(f)"$(aikito completion candidates projects 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ project|show\\ projects)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates projects 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (sync\\ project)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates projects 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (init\\ workspace)
                        _files -/
                        ;;
                    (completion\\ candidates)
                        compadd projects skills memories subagents mcps
                        ;;
                esac
            fi
            ;;
        5)
            local cmd="${{words[2]}}"
            local sub="${{words[3]}}"
            case "$cmd $sub" in
                (sync\\ project|init\\ project)
                    _files -/
                    if [[ -n $cur && $cur != */* ]]; then
                        local -a cands
                        cands=(${{(f)"$(aikito completion candidates paths "$cur" 2>/dev/null)"}})
                        (( ${{#cands}} )) && compadd -U -a cands
                    fi
                    ;;
            esac
            ;;
    esac
}}

if [[ "$funcstack[1]" == *"_aikito"* ]]; then
    _aikito "$@"
else
    _aikito_register() {{
        # 1. compdef — standard registration
        (( $+functions[compdef] )) && compdef _aikito aikito
        # 2. Direct _comps assignment — survives compdef unavailability
        (( ${{+_comps}} ))         && _comps[aikito]=_aikito
    }}

    # Register now (handles the case where compinit already ran)
    _aikito_register

    # Also register on first prompt — handles deferred compinit
    autoload -Uz add-zsh-hook
    _aikito_register_hook() {{
        _aikito_register
        add-zsh-hook -d precmd _aikito_register_hook
        unfunction _aikito_register_hook 2>/dev/null
        unfunction _aikito_register    2>/dev/null
    }}
    add-zsh-hook precmd _aikito_register_hook
fi
"""


def generate_bash(parser: argparse.ArgumentParser | None = None) -> str:
    """Generate a Bash completion script for aikito using reflected parser schema."""
    schema = _get_schema(parser)
    cmds = " ".join(sorted(schema["commands"].keys()))
    top_flags = " ".join(sorted(schema["flags"]))

    cmd_sub_cases = []
    cmd_flag_cases = []
    sub_flag_cases = []

    for cmd, data in sorted(schema["commands"].items()):
        subs = sorted(data["subcommands"].keys())
        if subs:
            cmd_sub_cases.append(f"""\
        {cmd})
            COMPREPLY=( $(compgen -W "{" ".join(subs)}" -- "$cur") )
            return 0
            ;;""")
        flags = data["flags"]
        if flags:
            cmd_flag_cases.append(f"""\
        {cmd})
            COMPREPLY=( $(compgen -W "{" ".join(flags)}" -- "$cur") )
            return 0
            ;;""")

        for sub, sub_data in sorted(data["subcommands"].items()):
            s_flags = sub_data["flags"]
            if s_flags:
                sub_flag_cases.append(f"""\
        "{cmd} {sub}")
            COMPREPLY=( $(compgen -W "{" ".join(s_flags)}" -- "$cur") )
            return 0
            ;;""")

    cmd_subs_str = "\n".join(cmd_sub_cases)
    cmd_flags_str = "\n".join(cmd_flag_cases)
    sub_flags_str = "\n".join(sub_flag_cases)

    return f"""# Aikito shell completion for Bash.
# Add to ~/.bashrc or ~/.bash_profile:  eval "$(aikito completion bash)"

_aikito_completion() {{
    local cur prev words cword
    _init_completion 2>/dev/null || {{
        COMPREPLY=()
        cur="${{COMP_WORDS[COMP_CWORD]}}"
        prev="${{COMP_WORDS[COMP_CWORD-1]}}"
    }}

    local commands="{cmds}"
    local top_flags="{top_flags}"

    if [[ $COMP_CWORD -eq 1 ]]; then
        if [[ $cur == -* ]]; then
            COMPREPLY=( $(compgen -W "$top_flags" -- "$cur") )
        else
            COMPREPLY=( $(compgen -W "$commands" -- "$cur") )
        fi
        return 0
    fi

    local cmd="${{COMP_WORDS[1]}}"
    local sub="${{COMP_WORDS[2]}}"

    if [[ $COMP_CWORD -eq 2 ]]; then
        if [[ $cur == -* ]]; then
            case $cmd in
{cmd_flags_str}
            esac
        else
            case $cmd in
{cmd_subs_str}
                adopt|init)
                    COMPREPLY=( $(compgen -d -- "$cur") )
                    if [[ -n $cur && $cur != */* ]]; then
                        while IFS= read -r candidate; do
                            [[ -n $candidate ]] && COMPREPLY+=("$candidate")
                        done < <(aikito completion candidates paths "$cur" 2>/dev/null)
                    fi
                    return 0
                    ;;
            esac
        fi
        return 0
    fi

    if [[ $COMP_CWORD -ge 3 ]]; then
        if [[ $cur == -* ]]; then
            case "$cmd $sub" in
{sub_flags_str}
            esac
            case $cmd in
{cmd_flags_str}
            esac
            return 0
        fi

        case "$cmd $sub" in
            show\\ memory|edit\\ memory|rename\\ memory|rm\\ memory|remove\\ memory)
                local candidate display
                while IFS=$'\\t' read -r candidate display; do
                    [[ $candidate == "$cur"* ]] && COMPREPLY+=("$candidate")
                done < <(aikito completion candidates memory-completions 2>/dev/null)
                return 0
                ;;
            show\\ inbox|edit\\ inbox|rm\\ inbox|remove\\ inbox)
                local candidates
                candidates=$(aikito completion candidates inbox-completions 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ skill|show\\ skills|edit\\ skill|edit\\ skills)
                local candidates
                candidates=$(aikito completion candidates skills 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ subagent|show\\ subagents|edit\\ subagent|edit\\ subagents)
                local candidates
                candidates=$(aikito completion candidates subagents 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ mcp|show\\ mcps|edit\\ mcp|edit\\ mcps)
                local candidates
                candidates=$(aikito completion candidates mcps 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ instructions|edit\\ instructions|maintain\\ memory)
                local projects
                projects=$(aikito completion candidates projects 2>/dev/null)
                COMPREPLY=( $(compgen -W "global . $projects" -- "$cur") )
                return 0
                ;;
            show\\ project|show\\ projects)
                local projects
                projects=$(aikito completion candidates projects 2>/dev/null)
                COMPREPLY=( $(compgen -W "$projects" -- "$cur") )
                return 0
                ;;
            sync\\ project)
                if [[ $COMP_CWORD -eq 3 ]]; then
                    local candidates
                    candidates=$(aikito completion candidates projects 2>/dev/null)
                    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                elif [[ $COMP_CWORD -eq 4 ]]; then
                    COMPREPLY=( $(compgen -d -- "$cur") )
                    if [[ -n $cur && $cur != */* ]]; then
                        while IFS= read -r candidate; do
                            [[ -n $candidate ]] && COMPREPLY+=("$candidate")
                        done < <(aikito completion candidates paths "$cur" 2>/dev/null)
                    fi
                fi
                return 0
                ;;
            init\\ project)
                if [[ $COMP_CWORD -eq 4 ]]; then
                    COMPREPLY=( $(compgen -d -- "$cur") )
                    if [[ -n $cur && $cur != */* ]]; then
                        while IFS= read -r candidate; do
                            [[ -n $candidate ]] && COMPREPLY+=("$candidate")
                        done < <(aikito completion candidates paths "$cur" 2>/dev/null)
                    fi
                fi
                return 0
                ;;
            init\\ workspace)
                COMPREPLY=( $(compgen -d -- "$cur") )
                if [[ -n $cur && $cur != */* ]]; then
                    while IFS= read -r candidate; do
                        [[ -n $candidate ]] && COMPREPLY+=("$candidate")
                    done < <(aikito completion candidates paths "$cur" 2>/dev/null)
                fi
                return 0
                ;;
            completion\\ candidates)
                COMPREPLY=( $(compgen -W "projects skills memories subagents mcps" -- "$cur") )
                return 0
                ;;
        esac
    fi
}}

complete -F _aikito_completion aikito
"""


def generate_fish(parser: argparse.ArgumentParser | None = None) -> str:
    """Generate a Fish completion script for aikito using reflected parser schema."""
    schema = _get_schema(parser)
    cmds = " ".join(sorted(schema["commands"].keys()))

    cmds_lines = [f"complete -c aikito -f -n '__fish_use_subcommand' -a '{cmds}'"]
    for flag in sorted(schema["flags"]):
        name = flag.lstrip("-")
        if len(flag) == 2:
            cmds_lines.append(
                f"complete -c aikito -f -n '__fish_use_subcommand' -s {name}"
            )
        else:
            cmds_lines.append(
                f"complete -c aikito -f -n '__fish_use_subcommand' -l {name}"
            )

    sub_lines = []
    flag_lines = []

    for cmd, data in sorted(schema["commands"].items()):
        subs = sorted(data["subcommands"].keys())
        if subs:
            subs_str = " ".join(subs)
            sub_lines.append(
                f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}' -a '{subs_str}'"
            )
        for flag in sorted(data["flags"]):
            name = flag.lstrip("-")
            if len(flag) == 2:
                flag_lines.append(
                    f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}' -s {name}"
                )
            else:
                flag_lines.append(
                    f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}' -l {name}"
                )

        for sub, sub_data in sorted(data["subcommands"].items()):
            for flag in sorted(sub_data["flags"]):
                name = flag.lstrip("-")
                if len(flag) == 2:
                    flag_lines.append(
                        f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}; and __fish_seen_subcommand_from {sub}' -s {name}"
                    )
                else:
                    flag_lines.append(
                        f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}; and __fish_seen_subcommand_from {sub}' -l {name}"
                    )

    dyn_lines = [
        "# Dynamic candidates & positionals",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit rename rm remove; and __fish_seen_subcommand_from memory' "
        "-a '(aikito completion candidates memory-completions 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit rm remove; and __fish_seen_subcommand_from inbox' "
        "-a '(aikito completion candidates inbox-completions 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from skill skills' "
        "-a '(aikito completion candidates skills 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from subagent subagents' "
        "-a '(aikito completion candidates subagents 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from mcp mcps' "
        "-a '(aikito completion candidates mcps 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from instructions' "
        "-a 'global . (aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from maintain; and __fish_seen_subcommand_from memory' "
        "-a 'global . (aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show; and __fish_seen_subcommand_from project projects' "
        "-a '(aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from sync; and __fish_seen_subcommand_from project' "
        "-a '(aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from completion; and __fish_seen_subcommand_from candidates' "
        "-a 'projects skills memories memory-completions subagents mcps inbox inbox-completions paths'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from init; and __fish_seen_subcommand_from workspace project'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from sync; and __fish_seen_subcommand_from project'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from adopt'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from adopt; "
        "and test (count (commandline -opc)) -eq 2' "
        "-a '(aikito completion candidates paths (commandline -ct) 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from init; "
        "and __fish_seen_subcommand_from workspace; "
        "and test (count (commandline -opc)) -eq 3' "
        "-a '(aikito completion candidates paths (commandline -ct) 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from init sync; "
        "and __fish_seen_subcommand_from project; "
        "and test (count (commandline -opc)) -eq 4' "
        "-a '(aikito completion candidates paths (commandline -ct) 2>/dev/null)'",
    ]

    parts = (
        [
            "# Aikito shell completion for Fish.",
            "# Copy to ~/.config/fish/completions/aikito.fish",
        ]
        + [
            "# or run: aikito completion fish > ~/.config/fish/completions/aikito.fish",
            "",
        ]
        + cmds_lines
        + [""]
        + sub_lines
        + [""]
        + sorted(set(flag_lines))
        + [""]
        + dyn_lines
    )
    return "\n".join(parts) + "\n"


def generate_powershell(parser: argparse.ArgumentParser | None = None) -> str:
    """Generate a native PowerShell completion script for aikito."""
    from aikito_completion_powershell import generate_powershell as _gen_pwsh

    return _gen_pwsh(parser)

