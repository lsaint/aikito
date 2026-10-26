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

For this task, review the complete selected scope. Inspect every note under notes/ and relevant inbound wikilinks for accuracy, durability, duplication, ownership, naming, and continued value. Use current code, configuration, documentation, and Git history to verify claims; age alone does not justify retirement.

Compare notes with relevant canonical skills and instructions. Keep procedures in skills, rules in instructions, and durable decisions and rationale in memory. Do not modify skills or instructions. Preserve unrelated user changes.

Perform read-only inspection without repeated permission requests. Present one concise proposal with affected files and reasons, grouping updates, merges, moves, retirements, and wikilink repairs as needed. Report the number of notes reviewed, any unread or unverified notes, and whether meaningful maintenance is needed. Collect unresolved questions for the user in this proposal.

Limit changes to the selected scope. Separately identify moves or wikilink repairs that require changes outside it, and obtain explicit approval for those files.

Do not modify files, stage changes, or create commits until the user confirms the proposal. Tool auto-approval does not count as proposal confirmation. If interactive follow-up is unavailable, return the proposal and stop.

After confirmation, apply the approved changes without asking for each operation. If new findings require expanding the approved plan, ask before proceeding. Repair affected wikilinks, verify memory integrity, and follow the durable-memory skill's Git commit rules. Do not create or maintain a separate memory index. Do not push.
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
        "workspace": str(aikito_dir.resolve()),
    }
    try:
        command = [part.format_map(values) for part in runner.command]
        command[1:1] = [arg.format_map(values) for arg in runner.auto_approve_args]
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
