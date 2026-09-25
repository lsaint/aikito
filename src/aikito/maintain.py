"""Launch an Agent to perform confirmation-gated Aikito memory maintenance."""

import os
import subprocess
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .agents import AgentRegistryError, RunnerCapability, load_agent_definition
from .compat import resolve_executable
from .project import resolve_project_binding
from .resolve import ProjectContextConflictError, detect_current_project


class MemoryMaintenanceError(RuntimeError):
    """Raised when a maintenance scope or Agent runner cannot be resolved."""


@dataclass(frozen=True)
class MemoryMaintenanceScope:
    name: str
    memory_dir: Path
    workdir: Path


@dataclass(frozen=True)
class _LoadedProject:
    name: str
    memory_dir: Path
    config_path: Path
    active_paths: tuple[Path, ...]
    has_candidates: bool


def _load_project(project_dir: Path) -> _LoadedProject | None:
    config_path = project_dir / "agent.toml"
    memory_dir = project_dir / "memory"
    if not config_path.is_file():
        return None
    try:
        config = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise MemoryMaintenanceError(f"Failed to read {config_path}: {exc}") from exc
    binding = resolve_project_binding(config, Path.home())
    return _LoadedProject(
        project_dir.name,
        memory_dir.resolve(),
        config_path,
        tuple(entry.resolved_path for entry in binding.active_entries),
        bool(binding.entries),
    )


def _best_cwd_match(cwd: Path, roots: tuple[Path, ...]) -> Path | None:
    matches = [root for root in roots if cwd == root or cwd.is_relative_to(root)]
    if not matches:
        return None
    return max(matches, key=lambda root: len(root.parts))


def _resolved_project_scope(
    name: str, memory_dir: Path, project_path: Path
) -> MemoryMaintenanceScope:
    if not memory_dir.is_dir():
        raise MemoryMaintenanceError(
            f"Project '{name}' is registered but has no memory scope: {memory_dir}"
        )
    return MemoryMaintenanceScope(name, memory_dir, project_path)


def _named_project_workdir(project: _LoadedProject, cwd: Path) -> Path:
    if not project.has_candidates:
        raise MemoryMaintenanceError(
            f"Project path is missing in {project.config_path}"
        )
    if not project.active_paths:
        raise MemoryMaintenanceError(
            f"Project '{project.name}' is offline on this host"
        )
    matched = _best_cwd_match(cwd, project.active_paths)
    if matched is not None:
        return matched
    if len(project.active_paths) == 1:
        return project.active_paths[0]
    listed = ", ".join(str(path) for path in project.active_paths)
    raise MemoryMaintenanceError(
        f"Project '{project.name}' has multiple local paths; "
        f"run this command from one of them: {listed}"
    )


def resolve_memory_maintenance_scope(
    aikito_dir: Path, target: str, cwd: Path
) -> MemoryMaintenanceScope:
    """Resolve global, named-project, or current-project memory to canonical paths."""
    if target == "global":
        memory_dir = aikito_dir / "memory"
        if not memory_dir.is_dir():
            raise MemoryMaintenanceError(f"Global memory scope not found: {memory_dir}")
        return MemoryMaintenanceScope(
            "global", memory_dir.resolve(), aikito_dir.resolve()
        )

    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        raise MemoryMaintenanceError("No registered projects found")

    projects: list[_LoadedProject] = []
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir():
            continue
        loaded = _load_project(project_dir)
        if loaded is not None:
            projects.append(loaded)

    if target == ".":
        try:
            detected = detect_current_project(aikito_dir, cwd, Path.home())
        except ProjectContextConflictError as exc:
            names = ", ".join(exc.projects)
            raise MemoryMaintenanceError(
                f"Current directory '{exc.path}' belongs to multiple projects: {names}"
            ) from exc
        if detected is None:
            raise MemoryMaintenanceError(
                f"Current directory is not inside a registered project: {cwd.resolve()}"
            )
        target = detected

    for project in projects:
        if project.name == target:
            workdir = _named_project_workdir(project, cwd.resolve())
            return _resolved_project_scope(project.name, project.memory_dir, workdir)
    raise MemoryMaintenanceError(f"Memory scope '{target}' not found")


def load_agent_runner(aikito_dir: Path, agent_name: str) -> RunnerCapability:
    config_path = aikito_dir / "agents" / f"{agent_name}.toml"
    try:
        agent = load_agent_definition(aikito_dir, Path.home(), agent_name)
    except AgentRegistryError as exc:
        raise MemoryMaintenanceError(str(exc)) from exc
    runner = agent.runner
    if runner is None:
        raise MemoryMaintenanceError(
            f"Agent '{agent_name}' has no runner configuration in {config_path}"
        )
    return runner


def build_memory_maintenance_prompt(scope: MemoryMaintenanceScope) -> str:
    return f"""Use the durable-memory skill to perform proactive maintenance of the selected Aikito memory scope.

Selected scope: {scope.name}
Canonical memory directory: {scope.memory_dir}

For this task, review the complete selected scope rather than maintaining memory only opportunistically.

Inspect every memory note in the scope and relevant inbound wikilinks. Evaluate each note for accuracy, durability, duplication, scope ownership, naming, and continued decision value.

Use current code, configuration, documentation, and Git history when needed to verify claims. Do not treat age alone as evidence that a note is obsolete. Preserve unrelated user changes.

Also compare notes with relevant canonical skills and instructions. Treat duplicated or conflicting operational guidance as a maintenance issue. Keep reusable procedures in skills, binding rules in instructions, and retain in memory only durable decisions, rationale, or constraints not readily available from those sources. Resolve conflicts using current code, configuration, documentation, tests, or Git history when they provide sufficient evidence. When a conflict cannot be verified objectively, present the alternatives and ask the user to decide. Do not modify skills or instructions as part of this workflow; report any required upstream correction separately.

Propose the smallest set of meaningful changes. Group the proposal into update, merge, move, retire, and wikilink repair. For every proposed change, explain the reason and identify the affected files. Explicitly report when no meaningful maintenance is needed.

Do not modify files, stage changes, or create commits until the user confirms the proposal.

After confirmation, apply only the approved changes, repair affected indices and wikilinks, verify memory integrity, and follow the durable-memory skill's Git commit rules. Do not push.
"""


def run_memory_maintenance(
    aikito_dir: Path, target: str, agent_name: str, cwd: Path
) -> int:
    scope = resolve_memory_maintenance_scope(aikito_dir, target, cwd)
    prompt = build_memory_maintenance_prompt(scope)
    runner = load_agent_runner(aikito_dir, agent_name)
    values = {
        "prompt": prompt,
        "scope": scope.name,
        "workdir": str(scope.workdir),
        "memory_dir": str(scope.memory_dir),
    }
    try:
        command = [part.format_map(values) for part in runner.command]
        configured_env = {
            key: value.format_map(values) for key, value in runner.env.items()
        }
    except (KeyError, IndexError, ValueError) as exc:
        raise MemoryMaintenanceError(f"Invalid runner placeholder: {exc}") from exc
    try:
        process_env = os.environ.copy()
        process_env.update(configured_env)
        return subprocess.run(
            resolve_executable(command), cwd=scope.workdir, env=process_env
        ).returncode
    except OSError as exc:
        raise MemoryMaintenanceError(
            f"Failed to launch Agent '{agent_name}': {exc}"
        ) from exc
