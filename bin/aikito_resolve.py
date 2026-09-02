"""Target resolution and editor launching for drill-down CLI commands.

``show``/``edit``/``rename``/``rm`` all resolve user-supplied names or prefixes
to canonical workspace files. This module keeps that logic out of the entry
script so the entry stays a thin parser plus dispatch.
"""

import subprocess
import sys
import tomllib
from pathlib import Path

from aikito_config import get_inbox_path
from aikito_inbox import resolve_inbox_target_for_command
from aikito_link import classify_symlink, symlink_verdict_to_status
from aikito_mcp import load_agents
from aikito_memory import ensure_safe_path
from aikito_platform import (
    get_default_editor,
    resolve_executable,
    safe_relative_path,
    split_command,
)
from aikito_render import SkillRow
from aikito_status import collect_skills_rows
from aikito_subagent import SubagentConfigError, load_subagent_definitions


class SkillTargetConflictError(Exception):
    def __init__(self, target: str, candidates: list[SkillRow]):
        super().__init__(f"Multiple skills match '{target}'")
        self.target = target
        self.candidates = candidates


def resolve_skill_target(aikito_dir: Path, target: str) -> SkillRow:
    target_norm = target.strip()
    rows = collect_skills_rows(aikito_dir)

    exact_matches = [r for r in rows if r.skill_name == target_norm]
    if len(exact_matches) == 1:
        return exact_matches[0]

    prefix_matches = [r for r in rows if r.skill_name.startswith(target_norm)]
    if len(prefix_matches) == 1:
        return prefix_matches[0]

    if len(prefix_matches) > 1:
        raise SkillTargetConflictError(target, prefix_matches)

    print(f"[ERROR] Skill '{target}' not found.", file=sys.stderr)
    print("Run 'aikito show skills' to view available skills.", file=sys.stderr)
    sys.exit(1)


def resolve_skill_target_for_command(
    aikito_dir: Path, target: str, operation: str
) -> Path:
    try:
        skill_row = resolve_skill_target(aikito_dir, target)
    except SkillTargetConflictError as exc:
        print(
            f"[CONFLICT] Multiple skills match '{exc.target}':\n",
            file=sys.stderr,
        )
        for item in exc.candidates:
            print(f"  - {item.skill_name} ({item.scope})", file=sys.stderr)
        print("\nPlease specify the exact skill name, e.g.:", file=sys.stderr)
        for item in exc.candidates:
            print(
                f"  aikito {operation} skill {item.skill_name}",
                file=sys.stderr,
            )
        sys.exit(1)

    skills_root = aikito_dir / "skills"
    skill_file = skills_root / skill_row.skill_name / "SKILL.md"

    ensure_safe_path(skill_file, [skills_root], "skill")

    if not skill_file.is_file():
        print(
            f"[ERROR] SKILL.md not found for skill '{skill_row.skill_name}' at {skill_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    return skill_file


def resolve_subagent_target_for_command(
    aikito_dir: Path, target: str, operation: str
) -> Path:
    try:
        definitions = load_subagent_definitions(aikito_dir, allow_empty=True)
    except SubagentConfigError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)

    target_norm = target.strip()
    names = sorted(definitions.keys())

    exact_matches = [name for name in names if name == target_norm]
    if len(exact_matches) == 1:
        matched_name = exact_matches[0]
    else:
        prefix_matches = [name for name in names if name.startswith(target_norm)]
        if len(prefix_matches) == 1:
            matched_name = prefix_matches[0]
        elif len(prefix_matches) > 1:
            print(
                f"[CONFLICT] Multiple subagents match '{target}':\n",
                file=sys.stderr,
            )
            for name in prefix_matches:
                print(f"  - {name}", file=sys.stderr)
            print("\nPlease specify the exact subagent name, e.g.:", file=sys.stderr)
            for name in prefix_matches:
                print(
                    f"  aikito {operation} subagent {name}",
                    file=sys.stderr,
                )
            sys.exit(1)
        else:
            print(f"[ERROR] Subagent '{target}' not found.", file=sys.stderr)
            print(
                "Run 'aikito show subagents' to view available subagents.",
                file=sys.stderr,
            )
            sys.exit(1)

    subagents_root = aikito_dir / "subagents"
    subagent_file = subagents_root / f"{matched_name}.md"

    ensure_safe_path(subagent_file, [subagents_root], "subagent")

    if not subagent_file.is_file():
        print(
            f"[ERROR] Instructions file for subagent '{matched_name}' not found at {subagent_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    return subagent_file


def resolve_mcp_target_for_command(
    aikito_dir: Path, target: str, operation: str
) -> Path:
    mcps_dir = aikito_dir / "mcps"
    if not mcps_dir.is_dir():
        print(f"[ERROR] MCP directory not found at {mcps_dir}", file=sys.stderr)
        sys.exit(1)

    target_norm = target.strip()
    files = sorted(mcps_dir.glob("*.toml"))
    names = [f.stem for f in files if f.is_file() and not f.name.startswith(".")]

    exact_matches = [name for name in names if name == target_norm]
    if len(exact_matches) == 1:
        matched_name = exact_matches[0]
    else:
        prefix_matches = [name for name in names if name.startswith(target_norm)]
        if len(prefix_matches) == 1:
            matched_name = prefix_matches[0]
        elif len(prefix_matches) > 1:
            print(
                f"[CONFLICT] Multiple MCP servers match '{target}':\n",
                file=sys.stderr,
            )
            for name in prefix_matches:
                print(f"  - {name}", file=sys.stderr)
            print("\nPlease specify the exact MCP server name, e.g.:", file=sys.stderr)
            for name in prefix_matches:
                print(
                    f"  aikito {operation} mcp {name}",
                    file=sys.stderr,
                )
            sys.exit(1)
        else:
            print(f"[ERROR] MCP server '{target}' not found.", file=sys.stderr)
            print(
                "Run 'aikito show mcp' to view available MCP servers.", file=sys.stderr
            )
            sys.exit(1)

    mcp_file = mcps_dir / f"{matched_name}.toml"
    ensure_safe_path(mcp_file, [mcps_dir], "mcp")

    if not mcp_file.is_file():
        print(
            f"[ERROR] Config file for MCP server '{matched_name}' not found at {mcp_file}",
            file=sys.stderr,
        )
        sys.exit(1)

    return mcp_file


def find_instruction_sources(
    aikito_dir: Path, home: Path
) -> list[tuple[str, Path, Path | None]]:
    """Return instruction target, source file, and registered project path."""
    sources = [("global", aikito_dir / "global" / "AGENTS.md", None)]
    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        return sources

    for project_dir in sorted(projects_dir.iterdir()):
        config_path = project_dir / "agent.toml"
        instructions_path = project_dir / "AGENTS.md"
        if not project_dir.is_dir() or not config_path.is_file():
            continue
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            print(f"[WARN] Failed to read {config_path}: {exc}", file=sys.stderr)
            continue
        raw_path = config.get("path")
        project_path = None
        if isinstance(raw_path, str) and raw_path:
            project_path = (
                home / raw_path[2:]
                if raw_path.startswith("~/")
                else Path(raw_path).expanduser()
            ).resolve()
        sources.append((project_dir.name, instructions_path, project_path))
    return sources


def resolve_instruction_target(
    aikito_dir: Path, home: Path, target: str, cwd: Path
) -> tuple[str, Path]:
    sources = find_instruction_sources(aikito_dir, home)
    if target == ".":
        current = cwd.resolve()
        matches = [
            source
            for source in sources
            if source[2] is not None
            and (current == source[2] or current.is_relative_to(source[2]))
        ]
        if matches:
            name, instructions_path, _ = max(
                matches, key=lambda source: len(source[2].parts)
            )
            return name, instructions_path
        print(
            f"[ERROR] Current directory is not inside a registered project: {current}",
            file=sys.stderr,
        )
        sys.exit(1)

    for name, instructions_path, _ in sources:
        if name == target:
            return name, instructions_path

    print(f"[ERROR] Instructions target '{target}' not found.", file=sys.stderr)
    print("Run 'aikito show instructions' to view available targets.", file=sys.stderr)
    sys.exit(1)


def collect_instruction_agent_status(
    aikito_dir: Path, home: Path
) -> list[tuple[str, str, str]]:
    source = aikito_dir / "global" / "AGENTS.md"
    rows = []
    status_names = {
        "OK": "linked",
        "MISSING": "missing",
        "CONFLICT": "conflict",
        "SKIP": "skipped",
    }
    for definition in load_agents(aikito_dir, home).values():
        target = definition.instruction_path
        if target is None or not target.parent.exists():
            status = "SKIP"
        else:
            status = symlink_verdict_to_status(classify_symlink(target, source))
        target_display = "-" if target is None else safe_relative_path(target, home)
        rows.append(
            (definition.display_name, target_display, status_names.get(status, status))
        )
    return rows


def collect_project_instruction_status(
    aikito_dir: Path, home: Path
) -> list[tuple[str, str]]:
    rows = []
    status_names = {
        "OK": "linked",
        "MISSING": "missing",
        "CONFLICT": "conflict",
    }

    for name, source, project_path in find_instruction_sources(aikito_dir, home)[1:]:
        try:
            has_content = source.is_file() and bool(
                source.read_text(encoding="utf-8").strip()
            )
        except OSError:
            has_content = False
        if not has_content or project_path is None:
            rows.append((name, "-"))
            continue
        target = project_path / ".agents" / "AGENTS.md"
        status = symlink_verdict_to_status(classify_symlink(target, source))
        rows.append((name, status_names.get(status, status.lower())))
    return rows


def open_in_editor(target_file: Path) -> None:
    editor_env = get_default_editor()
    editor_parts = split_command(editor_env)
    if not editor_parts:
        editor_parts = [editor_env]

    cmd_args = resolve_executable(editor_parts) + [str(target_file)]

    try:
        res = subprocess.run(cmd_args)

        if res.returncode != 0:
            sys.exit(res.returncode)
    except Exception as exc:
        print(
            f"[ERROR] Failed to launch editor '{editor_env}': {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def resolve_inbox_target(aikito_dir: Path, target: str, operation: str) -> Path:
    """Resolve an inbox note target relative to the workspace inbox directory."""
    inbox_dir = get_inbox_path(aikito_dir)
    return resolve_inbox_target_for_command(inbox_dir, target, operation)


__all__ = [
    "SkillTargetConflictError",
    "collect_instruction_agent_status",
    "collect_project_instruction_status",
    "find_instruction_sources",
    "open_in_editor",
    "resolve_inbox_target",
    "resolve_instruction_target",
    "resolve_mcp_target_for_command",
    "resolve_skill_target",
    "resolve_skill_target_for_command",
    "resolve_subagent_target_for_command",
]
