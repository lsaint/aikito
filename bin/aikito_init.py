"""
Workspace initialization module for Aikito.
Creates workspace skeleton, configuration templates, .gitignore, and initializes git repo.

Template content is owned by ``templates/`` next to ``bin/``; this module only
enforces validation, source-root protection, and file placement. See
``aikito_templates`` for template loading and per-agent filtering.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Optional

import tomllib

from aikito_mcp import MCPConfigError, collect_project_instruction_targets
from aikito_platform import safe_relative_path
from aikito_project import resolve_project_binding
from aikito_templates import (
    BUNDLED_SKILL_NAMES,
    bundled_skill_path,
    detect_existing_agents,
    detected_agent_names,
    render_project_files,
    render_workspace_files,
    verify_templates,
)


CLI_SOURCE_ROOT = Path(__file__).resolve().parents[1]

SOURCE_CHECKOUT_MARKERS = (
    Path("LICENSE"),
    Path("README.md"),
    Path("bin/aikito"),
)

WORKSPACE_FILE_MARKERS = (
    Path("agents.toml"),
    Path("skills.toml"),
    Path("subagents.toml"),
)

WORKSPACE_DIRECTORY_MARKERS = (
    Path("mcps"),
    Path("memory"),
    Path("projects"),
    Path("skills"),
    Path("global"),
)

__all__ = [
    "CLI_SOURCE_ROOT",
    "BUNDLED_SKILL_NAMES",
    "SOURCE_CHECKOUT_MARKERS",
    "WORKSPACE_FILE_MARKERS",
    "WORKSPACE_DIRECTORY_MARKERS",
    "init_workspace",
    "init_project",
    "project_validation_error",
    "project_sync_validation_error",
]


def _target_validation_error(target_dir: Path) -> Optional[str]:
    source_root = CLI_SOURCE_ROOT.resolve()
    if target_dir == source_root or source_root in target_dir.parents:
        return (
            "Refusing to initialize an Aikito workspace inside the CLI source "
            f"tree.\n  Source: {source_root}\n  Target: {target_dir}"
        )

    if not target_dir.exists():
        return None
    if not target_dir.is_dir():
        return f"Target path exists but is not a directory: {target_dir}"
    if not any(target_dir.iterdir()):
        return None

    if all((target_dir / marker).exists() for marker in SOURCE_CHECKOUT_MARKERS):
        return (
            "Target looks like an Aikito source checkout. Keep the CLI source "
            f"and workspace in separate directories: {target_dir}"
        )

    has_workspace_files = all(
        (target_dir / marker).is_file() for marker in WORKSPACE_FILE_MARKERS
    )
    has_workspace_directories = all(
        (target_dir / marker).is_dir() for marker in WORKSPACE_DIRECTORY_MARKERS
    )
    if has_workspace_files and has_workspace_directories:
        return None

    return (
        "Target directory is not empty and is not a recognized Aikito "
        f"workspace: {target_dir}"
    )


def _load_templates_error() -> Optional[str]:
    """Return a startup error if any required template asset is missing."""
    errors = verify_templates()
    return "\n".join(errors) if errors else None


def init_workspace(target_dir: Path, home: Path, force: bool = False) -> bool:
    """
    Initializes an Aikito user data workspace in target_dir.
    Creates skeleton directories, template configs, leading-slash .gitignore, and git repo.
    """
    target_dir = target_dir.expanduser().resolve()

    validation_error = _target_validation_error(target_dir)
    if validation_error:
        print(f"[ERROR] {validation_error}", file=sys.stderr)
        return False

    template_error = _load_templates_error()
    if template_error:
        print(f"[ERROR] {template_error}", file=sys.stderr)
        return False

    detected_agents = detect_existing_agents(home)
    installed_agent_names = detected_agent_names(detected_agents)

    print(f"[INFO] Initializing Aikito workspace in: {target_dir}")

    # 1. Create skeleton directories
    dirs_to_create = [
        target_dir,
        target_dir / "global",
        target_dir / "projects",
        target_dir / "mcps",
        target_dir / "skills",
        target_dir / "subagents",
        target_dir / "memory" / "notes",
    ]

    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            print(f"[CREATE DIR] {d}")

    # 2. Write template files
    files_to_create = render_workspace_files(target_dir, installed_agent_names)
    for file_path, content, desc in files_to_create:
        if not file_path.exists() or force:
            file_path.write_text(content, encoding="utf-8")
            status_tag = (
                "[FORCE WRITE]" if force and file_path.exists() else "[CREATE FILE]"
            )
            print(f"{status_tag} {file_path} ({desc})")
        else:
            print(f"[SKIP FILE] {file_path} (Already exists)")

    for skill_name in BUNDLED_SKILL_NAMES:
        bundled_skill_target = target_dir / "skills" / skill_name
        skill_source = bundled_skill_path(skill_name)
        if bundled_skill_target.exists():
            print(f"[SKIP DIR] {bundled_skill_target} (Already exists)")
            continue
        shutil.copytree(skill_source, bundled_skill_target)
        print(f"[CREATE DIR] {bundled_skill_target} (Bundled {skill_name} skill)")

    # 3. Git Init
    git_dir = target_dir / ".git"
    if not git_dir.exists():
        try:
            subprocess.run(
                ["git", "init", str(target_dir)],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                check=True,
            )

            print(f"[GIT INIT] Initialized Git repository in {target_dir}")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to run 'git init': {e.stderr.strip()}")
            return False
    else:
        print(f"[SKIP GIT] Git repository already exists in {target_dir}")

    # 4. Check existing agents & print next-step hints
    print("\n[SUCCESS] Aikito workspace initialization complete!")

    if detected_agents:
        print("\n[INFO] Detected existing local agent configuration(s):")
        for name, p in detected_agents:
            print(f"  - {name} ({p})")
        print(
            "\n💡 Hint: You can run 'aikito adopt' to preview adoption changes, or 'aikito adopt --apply' to apply."
        )

    return True


def _display_path(path: Path, home: Path) -> str:
    return safe_relative_path(path, home)


def _validate_project_name(project_name: str) -> Optional[str]:
    import re

    if not project_name or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", project_name
    ):
        return (
            "Project name must start with a letter or digit and contain only "
            "letters, digits, dots, underscores, or hyphens."
        )
    return None


def init_project(
    aikito_dir: Path,
    project_path: Path,
    project_name: Optional[str] = None,
    home: Optional[Path] = None,
    description: Optional[str] = None,
) -> Optional[str]:
    """Create an idempotent canonical project definition and return its name."""
    aikito_dir = aikito_dir.expanduser().resolve()
    project_path = project_path.expanduser().resolve()
    resolved_name = project_name or project_path.name
    home = home or Path.home()

    validation_error = project_validation_error(
        aikito_dir, resolved_name, project_path, home
    )
    if validation_error:
        print(f"[ERROR] {validation_error}", file=sys.stderr)
        return None

    project_dir = aikito_dir / "projects" / resolved_name
    memory_dir = project_dir / "memory"
    notes_dir = memory_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    config_path = project_dir / "agent.toml"
    if not config_path.exists():
        display_path = _display_path(project_path, home)
        config_lines = [f'name = "{resolved_name}"']
        if description and description.strip():
            config_lines.append(
                f"description = {json.dumps(description.strip(), ensure_ascii=False)}"
            )
        config_lines.extend(
            [
                f'path = "{display_path}"',
                'sync_mode = "link"',
                "skills = []",
            ]
        )
        config_path.write_text("\n".join(config_lines) + "\n", encoding="utf-8")
        print(f"[CREATE FILE] {config_path}")
    else:
        print(f"[SKIP FILE] {config_path} (Already exists)")

    for file_path, content in render_project_files(project_dir):
        if file_path.exists():
            print(f"[SKIP FILE] {file_path} (Already exists)")
            continue
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_text(content, encoding="utf-8")
        print(f"[CREATE FILE] {file_path}")

    print(f"[SUCCESS] Project '{resolved_name}' initialized in Aikito workspace.")
    return resolved_name


def _project_validation_error(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    home: Path,
    *,
    reject_unexpected_entries: bool,
) -> Optional[str]:
    name_error = _validate_project_name(project_name)
    if name_error:
        return name_error

    if not project_path.exists():
        return f"Project path does not exist: {project_path}"
    if not project_path.is_dir():
        return f"Project path is not a directory: {project_path}"

    if not (aikito_dir / "agents.toml").is_file():
        return f"Aikito workspace is not initialized: {aikito_dir}"

    config_path = aikito_dir / "projects" / project_name / "agent.toml"
    config_data = None
    if config_path.exists():
        try:
            with open(config_path, "rb") as config_file:
                config_data = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return f"Failed to read existing project config {config_path}: {exc}"

        binding = resolve_project_binding(config_data, home)
        if binding.entries:
            matched = any(
                entry.resolved_path == project_path for entry in binding.entries
            )
            if not matched and reject_unexpected_entries:
                registered = ", ".join(
                    str(entry.resolved_path) for entry in binding.entries
                )
                return (
                    f"Project '{project_name}' is already registered to "
                    f"{registered}, not {project_path}."
                )

    canonical_instructions = aikito_dir / "projects" / project_name / "AGENTS.md"
    try:
        instruction_targets = collect_project_instruction_targets(
            aikito_dir, project_path, home
        )
    except MCPConfigError as exc:
        return str(exc)
    instructions_enabled = canonical_instructions.is_file() and bool(
        canonical_instructions.read_text(encoding="utf-8", errors="replace").strip()
    )
    if instructions_enabled:
        for target, agent_names in instruction_targets.items():
            if target.is_symlink():
                if target.resolve(strict=False) == canonical_instructions.resolve(
                    strict=False
                ):
                    continue
            elif not target.exists():
                continue
            return (
                f"Unmanaged project instructions for {', '.join(agent_names)} "
                f"already exist: {target}"
            )

    expected_entries = {"skills": set(), "memory": set()}
    if config_data is not None:
        expected_entries["skills"].update(config_data.get("skills", []))
        expected_entries["memory"].update(config_data.get("memory", []))
        project_memory = aikito_dir / "projects" / project_name / "memory"
        if project_memory.is_dir():
            expected_entries["memory"].update(
                item.name for item in project_memory.iterdir()
            )

    for managed_dir_name, allowed_entries in expected_entries.items():
        managed_dir = project_path / ".agents" / managed_dir_name
        if managed_dir.is_symlink() or not managed_dir.exists():
            continue
        if not managed_dir.is_dir():
            return f"Unmanaged project resources already exist: {managed_dir}"
        if managed_dir_name == "skills":
            continue
        if not reject_unexpected_entries:
            continue
        unexpected_entries = {
            item.name for item in managed_dir.iterdir()
        } - allowed_entries
        if unexpected_entries:
            return (
                f"Unmanaged project resources already exist in {managed_dir}: "
                f"{', '.join(sorted(unexpected_entries))}"
            )

    return None


def project_validation_error(
    aikito_dir: Path, project_name: str, project_path: Path, home: Path
) -> Optional[str]:
    """Validate registration, where every pre-existing runtime entry is foreign."""
    return _project_validation_error(
        aikito_dir,
        project_name,
        project_path,
        home,
        reject_unexpected_entries=True,
    )


def project_sync_validation_error(
    aikito_dir: Path, project_name: str, project_path: Path, home: Path
) -> Optional[str]:
    """Validate repeat sync structure before managed-entry ownership is planned."""
    return _project_validation_error(
        aikito_dir,
        project_name,
        project_path,
        home,
        reject_unexpected_entries=False,
    )
