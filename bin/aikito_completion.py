"""
Shell completion support for the Aikito CLI.

Provides:
- Reflection-based completion script generators for Zsh, Bash, and Fish shells.
- Lightweight dynamic candidate listing (projects, skills, memories).

Design principle: stdlib-only, no third-party dependencies.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import List


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
        p.name for p in projects_dir.iterdir()
        if p.is_dir() and not p.name.startswith(".")
    )


def list_skills(aikito_dir: Path) -> List[str]:
    """Return sorted list of skill names collected from workspace and project configs."""
    rows = collect_skills_rows(aikito_dir)
    return sorted(set(r.skill_name for r in rows))


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


def get_candidates(category: str, aikito_dir: Path) -> List[str]:
    """Dispatch to the correct candidate lister by category name."""
    dispatch = {
        "projects": list_projects,
        "skills": list_skills,
        "memories": list_memories,
    }
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

    # 1. Try sys.modules if loaded by test framework or caller
    if "aikito_cli" in sys.modules:
        return extract_cli_schema(sys.modules["aikito_cli"].build_parser())

    # 2. Try loading bin/aikito dynamically
    import importlib.machinery
    import importlib.util

    aikito_path = Path(__file__).parent / "aikito"
    if aikito_path.is_file():
        try:
            loader = importlib.machinery.SourceFileLoader("aikito_cli", str(aikito_path))
            spec = importlib.util.spec_from_loader(loader.name, loader)
            if spec:
                mod = importlib.util.module_from_spec(spec)
                loader.exec_module(mod)
                return extract_cli_schema(mod.build_parser())
        except Exception:
            pass

    raise RuntimeError("Failed to load ArgumentParser for shell completion generation.")


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
            cmd_flag_cases.append(f"                ({cmd}) compadd {' '.join(flags)} ;;")

        for sub, sub_data in sorted(data["subcommands"].items()):
            s_flags = sub_data["flags"]
            if s_flags:
                sub_flag_cases.append(f"                ({cmd} {sub}) compadd {' '.join(s_flags)} ;;")

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
                    (show\\ memory|edit\\ memory)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates memories 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ skill|show\\ skills|edit\\ skill|edit\\ skills)
                        local cands
                        cands=(${{(f)"$(aikito completion candidates skills 2>/dev/null)"}})
                        compadd -a cands
                        ;;
                    (show\\ instructions|edit\\ instructions)
                        local cands
                        cands=(global . ${{(f)"$(aikito completion candidates projects 2>/dev/null)"}})
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
                        compadd projects skills memories
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
            COMPREPLY=( $(compgen -W "{' '.join(subs)}" -- "$cur") )
            return 0
            ;;""")
        flags = data["flags"]
        if flags:
            cmd_flag_cases.append(f"""\
        {cmd})
            COMPREPLY=( $(compgen -W "{' '.join(flags)}" -- "$cur") )
            return 0
            ;;""")

        for sub, sub_data in sorted(data["subcommands"].items()):
            s_flags = sub_data["flags"]
            if s_flags:
                sub_flag_cases.append(f"""\
        "{cmd} {sub}")
            COMPREPLY=( $(compgen -W "{' '.join(s_flags)}" -- "$cur") )
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
            show\\ memory|edit\\ memory)
                local candidates
                candidates=$(aikito completion candidates memories 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ skill|show\\ skills|edit\\ skill|edit\\ skills)
                local candidates
                candidates=$(aikito completion candidates skills 2>/dev/null)
                COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                return 0
                ;;
            show\\ instructions|edit\\ instructions)
                local projects
                projects=$(aikito completion candidates projects 2>/dev/null)
                COMPREPLY=( $(compgen -W "global . $projects" -- "$cur") )
                return 0
                ;;
            sync\\ project)
                if [[ $COMP_CWORD -eq 3 ]]; then
                    local candidates
                    candidates=$(aikito completion candidates projects 2>/dev/null)
                    COMPREPLY=( $(compgen -W "$candidates" -- "$cur") )
                elif [[ $COMP_CWORD -eq 4 ]]; then
                    COMPREPLY=( $(compgen -d -- "$cur") )
                fi
                return 0
                ;;
            init\\ project)
                if [[ $COMP_CWORD -eq 4 ]]; then
                    COMPREPLY=( $(compgen -d -- "$cur") )
                fi
                return 0
                ;;
            init\\ workspace)
                COMPREPLY=( $(compgen -d -- "$cur") )
                return 0
                ;;
            completion\\ candidates)
                COMPREPLY=( $(compgen -W "projects skills memories" -- "$cur") )
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

    cmds_lines = [
        f"complete -c aikito -f -n '__fish_use_subcommand' -a '{cmds}'"
    ]
    for flag in sorted(schema["flags"]):
        name = flag.lstrip("-")
        if len(flag) == 2:
            cmds_lines.append(f"complete -c aikito -f -n '__fish_use_subcommand' -s {name}")
        else:
            cmds_lines.append(f"complete -c aikito -f -n '__fish_use_subcommand' -l {name}")

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
                flag_lines.append(f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}' -s {name}")
            else:
                flag_lines.append(f"complete -c aikito -f -n '__fish_seen_subcommand_from {cmd}' -l {name}")

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
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from memory' "
        "-a '(aikito completion candidates memories 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from skill skills' "
        "-a '(aikito completion candidates skills 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from show edit; and __fish_seen_subcommand_from instructions' "
        "-a 'global . (aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from sync; and __fish_seen_subcommand_from project' "
        "-a '(aikito completion candidates projects 2>/dev/null)'",
        "complete -c aikito -f -n '__fish_seen_subcommand_from completion; and __fish_seen_subcommand_from candidates' "
        "-a 'projects skills memories'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from init; and __fish_seen_subcommand_from workspace project'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from sync; and __fish_seen_subcommand_from project'",
        "complete -c aikito -F -n '__fish_seen_subcommand_from adopt'",
    ]

    parts = (
        ["# Aikito shell completion for Fish.", "# Copy to ~/.config/fish/completions/aikito.fish"]
        + ["# or run: aikito completion fish > ~/.config/fish/completions/aikito.fish", ""]
        + cmds_lines
        + [""]
        + sub_lines
        + [""]
        + sorted(set(flag_lines))
        + [""]
        + dyn_lines
    )
    return "\n".join(parts) + "\n"
