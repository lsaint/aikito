"""Workspace template loading and rendering for ``aikito init``.

Template files live under ``templates/`` next to ``bin/`` so the source
checkout root stays free of workspace-shaped files. The Agent registry is
assembled in canonical registry order from ``agents/_header.toml`` and one
``agents/<name>.toml`` fragment per Agent; initialization selects only detected
Agent fragments, while doctor uses the same fragments to build the full
registry. Other templates are loaded as individual files, and ``skills/`` holds
the bundled skills. ``render_workspace_files`` drives every file that lands in
a fresh workspace; ``render_project_files`` handles project-level templates.
"""

import shutil
from pathlib import Path
from typing import List, Tuple

from aikito_mcp import AGENT_INSTALL_MARKERS, is_agent_installed

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"
BUNDLED_SKILL_NAMES = ("aikito", "durable-memory")

# Workspace-level destinations and their source assets under templates/.
# agents/_header.toml marks agents.toml for per-Agent assembly during rendering.
TEMPLATE_FILES: list[tuple[str, str, str]] = [
    ("config.toml", "config.toml", "Workspace config template"),
    ("agents.toml", "agents/_header.toml", "Detected agents config"),
    ("skills.toml", "skills.toml", "Global skills config"),
    ("subagents.toml", "subagents.toml", "Subagents config template"),
    ("memory/index.md", "memory/index.md", "Memory index file"),
    ("global/AGENTS.md", "global/AGENTS.md", "Global agent instructions"),
    (".gitignore", "gitignore", "Workspace .gitignore with leading slashes"),
]

# Project-level template destinations under projects/<name>/. Destination keys
# mark each rewrite rule below for readability.
PROJECT_TEMPLATE_FILES: list[tuple[str, str]] = [
    ("AGENTS.md", "project/AGENTS.md"),
    ("memory/index.md", "project/memory/index.md"),
]


class TemplateError(RuntimeError):
    """Raised when a required template asset is missing from templates/."""

    pass


def _template_path(name: str) -> Path:
    path = TEMPLATES_DIR / name
    if not path.is_file() and not path.is_dir():
        raise TemplateError(
            f"Workspace template not found: {path}. "
            "The templates/ directory may be incomplete."
        )
    return path


def _load_template(name: str) -> str:
    return _template_path(name).read_text(encoding="utf-8")


def load_global_agents_template() -> str:
    return _load_template("global/AGENTS.md")


def load_default_memory_instruction() -> str:
    return load_global_agents_template().rstrip()


def load_agents_template() -> str:
    return _join_agent_templates(tuple(AGENT_INSTALL_MARKERS))


def filter_agents_template(agent_names: tuple[str, ...]) -> str:
    """Render the registry using only the selected per-Agent templates."""
    selected = set(agent_names)
    ordered_names = tuple(name for name in AGENT_INSTALL_MARKERS if name in selected)
    return _join_agent_templates(ordered_names)


def _join_agent_templates(agent_names: tuple[str, ...]) -> str:
    parts = [_load_template("agents/_header.toml").rstrip()]
    parts.extend(_load_template(f"agents/{name}.toml").strip() for name in agent_names)
    if not agent_names:
        parts.append("[agents]")
    return "\n\n".join(parts) + "\n"


def detect_existing_agents(home: Path) -> List[Tuple[str, Path]]:
    """Return installed registry agents in template order."""
    detected = []
    for agent_name, (
        display_name,
        binary,
        relative_marker,
    ) in AGENT_INSTALL_MARKERS.items():
        if not is_agent_installed(agent_name, home):
            continue
        executable = shutil.which(binary)
        detected.append(
            (display_name, Path(executable) if executable else home / relative_marker)
        )
    return detected


def detected_agent_names(
    detected_agents: List[Tuple[str, Path]],
) -> tuple[str, ...]:
    detected_display_names = {name for name, _ in detected_agents}
    return tuple(
        name
        for name, (display_name, _binary, _marker) in AGENT_INSTALL_MARKERS.items()
        if display_name in detected_display_names
    )


def bundled_skill_path(name: str) -> Path:
    return TEMPLATES_DIR / "skills" / name


def verify_templates() -> list[str]:
    """Return validation errors for every required workspace template asset."""
    required_files = [
        *(template_name for _dest, template_name, _description in TEMPLATE_FILES),
        *(template_name for _dest, template_name in PROJECT_TEMPLATE_FILES),
        *(f"agents/{name}.toml" for name in AGENT_INSTALL_MARKERS),
        *(f"skills/{name}/SKILL.md" for name in BUNDLED_SKILL_NAMES),
    ]
    return [
        f"Workspace template not found: {TEMPLATES_DIR / name}"
        for name in required_files
        if not (TEMPLATES_DIR / name).is_file()
    ]


def render_workspace_files(
    target_dir: Path | str, installed_agent_names: tuple[str, ...]
) -> list[tuple[Path, str, str]]:
    """Return (destination, content, description) for each workspace template."""
    target_dir = Path(target_dir)
    rendered = []
    for dest_rel, template_name, description in TEMPLATE_FILES:
        if dest_rel == "agents.toml":
            content = filter_agents_template(installed_agent_names)
        else:
            content = _load_template(template_name)
        rendered.append((target_dir / dest_rel, content, description))
    return rendered


def render_project_files(project_dir: Path | str) -> list[tuple[Path, str]]:
    """Return (destination, content) for project-level template files."""
    project_dir = Path(project_dir)
    return [
        (project_dir / dest_rel, _load_template(template_name))
        for dest_rel, template_name in PROJECT_TEMPLATE_FILES
    ]


__all__ = [
    "BUNDLED_SKILL_NAMES",
    "TEMPLATES_DIR",
    "TemplateError",
    "bundled_skill_path",
    "detect_existing_agents",
    "detected_agent_names",
    "filter_agents_template",
    "load_agents_template",
    "load_default_memory_instruction",
    "load_global_agents_template",
    "render_project_files",
    "render_workspace_files",
    "verify_templates",
]
