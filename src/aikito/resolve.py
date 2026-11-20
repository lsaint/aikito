"""Target resolution and editor launching for drill-down CLI commands.

``show``/``edit``/``rename``/``rm`` all resolve user-supplied names or prefixes
to canonical workspace files. This module keeps that logic out of the entry
script so the entry stays a thin parser plus dispatch.
"""

import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .config import get_inbox_path
from .context_footprint import format_instruction_size
from .instructions import check_global_overlay_guidance_status
from .inbox import resolve_inbox_target_for_command
from .link import classify_symlink, symlink_verdict_to_status
from .agents import AgentRegistry, detect_team_instruction, load_agent_definitions
from .memory import ensure_safe_path
from .project import resolve_project_binding
from .compat import (
    get_default_editor,
    resolve_executable,
    safe_relative_path,
    split_command,
)
from .render import SkillRow
from .status import collect_skills_rows
from .subagent import SubagentConfigError, load_subagent_definitions


class SkillTargetConflictError(Exception):
    def __init__(self, target: str, candidates: list[SkillRow]):
        super().__init__(f"Multiple skills match '{target}'")
        self.target = target
        self.candidates = candidates


class ProjectContextConflictError(Exception):
    def __init__(self, path: Path, projects: list[str]):
        super().__init__(
            f"Current directory '{path}' belongs to multiple projects: {', '.join(projects)}"
        )
        self.path = path
        self.projects = projects


def detect_current_project(
    aikito_dir: Path,
    cwd: Path,
    home: Path,
) -> str | None:
    """Return the registered project name if cwd is inside one of its active paths

    or inside its registered workspace project folder. Uses longest-path match for nested roots.
    Raises ProjectContextConflictError if the best matching path belongs to multiple projects.
    """
    current = cwd.resolve()
    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        return None

    # 1. Check if cwd is inside aikito_dir/projects/<project_name>
    try:
        resolved_proj_dir = projects_dir.resolve()
        if current != resolved_proj_dir and current.is_relative_to(resolved_proj_dir):
            rel = current.relative_to(resolved_proj_dir)
            if rel.parts:
                candidate = rel.parts[0]
                if (
                    not candidate.startswith(".")
                    and (projects_dir / candidate).is_dir()
                    and (projects_dir / candidate / "agent.toml").is_file()
                ):
                    return candidate
    except (ValueError, AttributeError):
        pass

    # 2. Local binding active paths matching (Longest Prefix Match)
    matches: list[tuple[str, Path]] = []
    for proj_folder in sorted(projects_dir.iterdir()):
        if not proj_folder.is_dir() or proj_folder.name.startswith("."):
            continue
        agent_toml = proj_folder / "agent.toml"
        if not agent_toml.is_file():
            continue
        try:
            config = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError):
            continue
        binding = resolve_project_binding(config, home)
        for entry in binding.active_entries:
            resolved_entry = entry.resolved_path.resolve()
            if current == resolved_entry or current.is_relative_to(resolved_entry):
                matches.append((proj_folder.name, resolved_entry))

    if not matches:
        return None

    max_len = max(len(entry_path.parts) for _, entry_path in matches)
    best_matches = [m for m in matches if len(m[1].parts) == max_len]
    unique_projects = sorted(set(name for name, _ in best_matches))

    if len(unique_projects) == 1:
        return unique_projects[0]

    raise ProjectContextConflictError(current, unique_projects)


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
        binding = resolve_project_binding(config, home)
        if binding.active_entries:
            for entry in binding.active_entries:
                sources.append(
                    (project_dir.name, instructions_path, entry.resolved_path)
                )
        elif binding.entries:
            sources.append(
                (
                    project_dir.name,
                    instructions_path,
                    binding.entries[0].resolved_path,
                )
            )
        else:
            sources.append((project_dir.name, instructions_path, None))
    return sources


def resolve_instruction_target(
    aikito_dir: Path, home: Path, target: str, cwd: Path
) -> tuple[str, Path]:
    detail = resolve_instruction_detail(aikito_dir, home, target, cwd)
    return detail.name, detail.private_path


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
    for definition in load_agent_definitions(aikito_dir, home).values():
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


@dataclass(frozen=True)
class GlobalInstructionStatus:
    canonical_display: str
    overlay_status: str


def collect_global_instruction_status(
    aikito_dir: Path, home: Path
) -> GlobalInstructionStatus:
    canonical_path = aikito_dir / "global" / "AGENTS.md"
    canon_display = safe_relative_path(canonical_path, home)
    if canonical_path.is_file():
        try:
            content = canonical_path.read_text(encoding="utf-8")
            canon_display = f"{canon_display} ({format_instruction_size(content)})"
        except OSError:
            canon_display = f"{canon_display} (-)"
    else:
        canon_display = f"{canon_display} (-)"

    overlay_status, _ = check_global_overlay_guidance_status(canonical_path)
    return GlobalInstructionStatus(
        canonical_display=canon_display,
        overlay_status=overlay_status,
    )


@dataclass(frozen=True)
class ProjectInstructionRow:
    project: str
    mode: str
    public: str
    private: str
    status: str


@dataclass(frozen=True)
class InstructionDetail:
    name: str
    project_path: Path | None
    private_path: Path
    public_path: Path | None
    is_overlay: bool
    status: str


def resolve_project_instruction_mode(
    project_path: Path | None,
    private_path: Path,
    registry: AgentRegistry | None = None,
) -> tuple[str, Path | None, Path | None, str]:
    """Resolve instruction mode, public team file, target symlink, and symlink status.

    Returns:
        (mode, public_file, target_symlink, status)
    """
    has_private = False
    try:
        if private_path.is_file():
            content = private_path.read_text(encoding="utf-8")
            if content.strip():
                has_private = True
    except OSError:
        pass

    public_file = None
    is_overlay = False
    if project_path is not None:
        is_overlay, public_file = detect_team_instruction(
            project_path, private_path, registry
        )

    if is_overlay:
        mode = "overlay"
        target_symlink = (
            project_path / ".agents" / "AGENTS.md" if project_path else None
        )
    elif has_private or project_path is not None:
        mode = "direct" if has_private else "-"
        target_symlink = project_path / "AGENTS.md" if project_path else None
        if (
            target_symlink is not None
            and not target_symlink.exists()
            and not target_symlink.is_symlink()
        ):
            legacy_target = project_path / ".agents" / "AGENTS.md"
            if legacy_target.exists() or legacy_target.is_symlink():
                target_symlink = legacy_target
    else:
        mode = "-"
        target_symlink = None

    if has_private and project_path is not None and target_symlink is not None:
        status = symlink_verdict_to_status(
            classify_symlink(target_symlink, private_path)
        )
    else:
        status = "-"

    return mode, public_file, target_symlink, status


def collect_project_instruction_status(
    aikito_dir: Path, home: Path
) -> list[ProjectInstructionRow]:
    rows: list[ProjectInstructionRow] = []
    registry = AgentRegistry.load(aikito_dir, home)

    sources = find_instruction_sources(aikito_dir, home)[1:]
    name_counts: dict[str, int] = {}
    for name, _, _ in sources:
        name_counts[name] = name_counts.get(name, 0) + 1

    for name, source, project_path in sources:
        label = (
            f"{name} ({safe_relative_path(project_path, home)})"
            if name_counts.get(name, 0) > 1 and project_path is not None
            else name
        )

        mode, public_file, _, status = resolve_project_instruction_mode(
            project_path, source, registry
        )

        private_lines_display = "-"
        try:
            if source.is_file():
                content = source.read_text(encoding="utf-8")
                if content.strip():
                    private_lines_display = f"{len(content.splitlines())}L"
        except OSError:
            pass

        public_lines_display = "-"
        if public_file is not None:
            try:
                pub_content = public_file.read_text(encoding="utf-8", errors="replace")
                public_lines_display = f"{len(pub_content.splitlines())}L"
            except OSError:
                public_lines_display = "-"

        rows.append(
            ProjectInstructionRow(
                project=label,
                mode=mode,
                public=public_lines_display,
                private=private_lines_display,
                status=status,
            )
        )
    return rows


def resolve_instruction_detail(
    aikito_dir: Path, home: Path, target: str, cwd: Path
) -> InstructionDetail:
    sources = find_instruction_sources(aikito_dir, home)
    detected_project = None
    if target == ".":
        try:
            detected_project = detect_current_project(aikito_dir, cwd, home)
        except ProjectContextConflictError as exc:
            names = ", ".join(exc.projects)
            print(
                f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                file=sys.stderr,
            )
            sys.exit(1)
        if not detected_project:
            print(
                f"[ERROR] Current directory is not inside a registered project: {cwd.resolve()}",
                file=sys.stderr,
            )
            sys.exit(1)
        target_name = detected_project
    else:
        target_name = target

    matched_entry = None
    for name, instructions_path, project_path in sources:
        if name == target_name:
            if target == "." and project_path is not None:
                try:
                    if (
                        project_path.resolve() == cwd.resolve()
                        or project_path.resolve() in cwd.resolve().parents
                    ):
                        matched_entry = (name, instructions_path, project_path)
                        break
                except OSError:
                    pass
            if matched_entry is None:
                matched_entry = (name, instructions_path, project_path)

    if matched_entry is None:
        print(f"[ERROR] Instructions target '{target}' not found.", file=sys.stderr)
        print(
            "Run 'aikito show instructions' to view available targets.", file=sys.stderr
        )
        sys.exit(1)

    name, private_path, project_path = matched_entry
    registry = AgentRegistry.load(aikito_dir, home)
    mode, public_file, _, status = resolve_project_instruction_mode(
        project_path, private_path, registry
    )

    return InstructionDetail(
        name=name,
        project_path=project_path,
        private_path=private_path,
        public_path=public_file,
        is_overlay=(mode == "overlay"),
        status=status,
    )


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


def resolve_project_filter(
    aikito_dir: Path,
    project_target: str,
    cwd: Path,
    home: Path,
) -> str:
    """Resolve project target string (e.g. name, prefix, or '.') to an exact project name or 'global'."""
    clean_target = project_target.strip()
    if clean_target.lower() == "global":
        return "global"

    projects_dir = aikito_dir / "projects"

    if clean_target == ".":
        try:
            detected = detect_current_project(aikito_dir, cwd, home)
        except ProjectContextConflictError as exc:
            names = ", ".join(exc.projects)
            print(
                f"[CONFLICT] Multiple projects match current directory '{exc.path}': {names}",
                file=sys.stderr,
            )
            sys.exit(1)

        if detected is not None:
            return detected

        print(
            f"[ERROR] Current directory is not inside a registered project: {cwd.resolve()}",
            file=sys.stderr,
        )
        sys.exit(1)

    registered: list[str] = []
    if projects_dir.is_dir():
        for p in sorted(projects_dir.iterdir()):
            if p.is_dir() and not p.name.startswith("."):
                registered.append(p.name)

    exact = [p for p in registered if p == clean_target]
    if exact:
        return exact[0]

    matches_prefix = [p for p in registered if p.startswith(clean_target)]
    if len(matches_prefix) == 1:
        return matches_prefix[0]

    if len(matches_prefix) > 1:
        names = ", ".join(matches_prefix)
        print(
            f"[CONFLICT] Multiple projects match '{clean_target}': {names}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[ERROR] Project '{clean_target}' not found.", file=sys.stderr)
    sys.exit(1)


__all__ = [
    "GlobalInstructionStatus",
    "InstructionDetail",
    "ProjectContextConflictError",
    "ProjectInstructionRow",
    "SkillTargetConflictError",
    "collect_global_instruction_status",
    "collect_instruction_agent_status",
    "collect_project_instruction_status",
    "detect_current_project",
    "find_instruction_sources",
    "open_in_editor",
    "resolve_inbox_target",
    "resolve_instruction_detail",
    "resolve_instruction_target",
    "resolve_mcp_target_for_command",
    "resolve_project_filter",
    "resolve_project_instruction_mode",
    "resolve_skill_target",
    "resolve_skill_target_for_command",
    "resolve_subagent_target_for_command",
]
