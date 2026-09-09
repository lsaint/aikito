"""Public project preparation API.

This module hides workspace lookup, host-specific path selection, and the
existing persistent project synchronization rules behind ``Project.prepare``.
It deliberately does not launch Agents or synchronize shared global config.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from .compat import can_symlink
from .init import project_sync_validation_error
from .mcp import MCPConfigError, collect_project_instruction_targets, load_agents
from .project import (
    RuntimeCleanupPlan,
    collect_single_project_skill_states,
    find_selected_runtime_conflicts,
    plan_runtime_cleanup,
    resolve_project_binding,
)
from .sync import (
    apply_runtime_cleanup,
    ensure_dir,
    sync_project_instruction,
    sync_resource,
)
from .workspace import resolve_workspace

SUPPORTED_PROJECT_AGENTS = ("pi",)
PROJECT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProjectError(RuntimeError):
    """Base class for public Aikito project API failures."""


class ProjectNotFoundError(ProjectError):
    """Raised when a named project is absent from the selected workspace."""


class InvalidProjectConfigError(ProjectError):
    """Raised when project or Agent configuration cannot be loaded safely."""


class NoAvailableProjectPathError(ProjectError):
    """Raised when none of a project's configured paths exists on this host."""


class AmbiguousProjectPathError(ProjectError):
    """Raised when several project paths are active and none may be guessed."""

    def __init__(self, project_name: str, paths: tuple[Path, ...]) -> None:
        self.project_name = project_name
        self.paths = paths
        joined = ", ".join(str(path) for path in paths)
        super().__init__(
            f"Multiple paths are available for project '{project_name}': {joined}"
        )


class UnsupportedProjectAgentError(ProjectError):
    """Raised when V1 cannot prepare a project for the requested Agent."""


class ProjectPrepareConflictError(ProjectError):
    """Raised when managed project resources cannot be synchronized safely."""

    def __init__(self, project_name: str, conflicts: tuple[str, ...]) -> None:
        self.project_name = project_name
        self.conflicts = conflicts
        super().__init__(
            f"Project preparation failed for '{project_name}': " + "; ".join(conflicts)
        )


@dataclass(frozen=True)
class PreparedProject:
    """Agent launch inputs produced after persistent project preparation."""

    name: str
    agent: str
    cwd: Path
    env_overrides: Mapping[str, str]


@dataclass(frozen=True)
class Project:
    """A canonical project definition loaded from one Aikito workspace."""

    name: str
    workspace: Path
    _home: Path
    _config: Mapping[str, Any]

    @classmethod
    def load(cls, name: str, workspace: Path | str | None = None) -> Project:
        """Load a named project without changing the persistent workspace pointer."""
        if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name):
            raise InvalidProjectConfigError(f"Invalid Aikito project name: {name!r}")

        home = Path.home()
        if workspace is None:
            workspace_path = resolve_workspace(home)
        else:
            supplied = Path(workspace).expanduser()
            if not supplied.is_absolute():
                raise InvalidProjectConfigError(
                    f"Aikito workspace path must be absolute: {workspace}"
                )
            workspace_path = supplied.resolve()

        config_path = workspace_path / "projects" / name / "agent.toml"
        if not config_path.is_file():
            raise ProjectNotFoundError(f"Aikito project not found: {name}")
        try:
            config = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise InvalidProjectConfigError(
                f"Invalid Aikito project config {config_path}: {exc}"
            ) from exc

        configured_name = config.get("name")
        if configured_name is not None and configured_name != name:
            raise InvalidProjectConfigError(
                f"Project name in {config_path} must be '{name}', got "
                f"{configured_name!r}"
            )
        _validate_project_config(config_path, config)
        return cls(name, workspace_path, home, MappingProxyType(config))

    @property
    def paths(self) -> tuple[Path, ...]:
        """Return all configured paths resolved for the current host."""
        binding = resolve_project_binding(dict(self._config), self._home)
        return tuple(entry.resolved_path for entry in binding.entries)

    def resolve_path(self) -> Path:
        """Return the sole active path, refusing to guess between checkouts."""
        binding = resolve_project_binding(dict(self._config), self._home)
        active_paths = tuple(entry.resolved_path for entry in binding.active_entries)
        if not active_paths:
            raise NoAvailableProjectPathError(
                f"No available path for project '{self.name}' on this machine"
            )
        if len(active_paths) > 1:
            raise AmbiguousProjectPathError(self.name, active_paths)
        return active_paths[0]

    def prepare(self, agent: str) -> PreparedProject:
        """Prepare persistent project resources and return Agent launch inputs."""
        if agent not in SUPPORTED_PROJECT_AGENTS:
            supported = ", ".join(SUPPORTED_PROJECT_AGENTS)
            raise UnsupportedProjectAgentError(
                f"Unsupported Aikito project agent: {agent}; V1 supports: {supported}"
            )
        try:
            agents = load_agents(self.workspace, self._home)
        except MCPConfigError as exc:
            raise InvalidProjectConfigError(str(exc)) from exc
        if agent not in agents:
            raise UnsupportedProjectAgentError(
                f"Aikito project agent is not configured: {agent}"
            )
        if not can_symlink():
            raise ProjectPrepareConflictError(
                self.name,
                ("Symbolic link support is required to prepare project resources",),
            )

        project_path = self.resolve_path()
        errors = collect_project_prepare_errors(
            self.workspace,
            self.name,
            project_path,
            dict(self._config),
            self._home,
        )
        if errors:
            raise ProjectPrepareConflictError(self.name, tuple(errors))
        try:
            sync_project_path(
                self.workspace,
                self.name,
                project_path,
                dict(self._config),
                self._home,
            )
        except (OSError, RuntimeError) as exc:
            raise ProjectPrepareConflictError(self.name, (str(exc),)) from exc

        return PreparedProject(
            name=self.name,
            agent=agent,
            cwd=project_path,
            env_overrides=MappingProxyType({}),
        )


def _validate_project_config(config_path: Path, config: dict[str, Any]) -> None:
    for field in ("skills", "memory"):
        value = config.get(field, [])
        if not isinstance(value, list) or not all(
            isinstance(item, str) and item for item in value
        ):
            raise InvalidProjectConfigError(
                f"Project field '{field}' must be a list of non-empty strings in "
                f"{config_path}"
            )

    sync_mode = config.get("sync_mode", "link")
    if sync_mode not in ("link", "copy"):
        raise InvalidProjectConfigError(
            f"Project field 'sync_mode' must be 'link' or 'copy' in {config_path}"
        )

    paths = config.get("paths")
    path = config.get("path")
    valid_paths = (
        paths is None
        or (
            isinstance(paths, list)
            and all(isinstance(item, str) and item for item in paths)
        )
        or (
            isinstance(paths, dict)
            and all(
                isinstance(label, str) and label and isinstance(value, str) and value
                for label, value in paths.items()
            )
        )
    )
    valid_path = (
        path is None
        or (isinstance(path, str) and bool(path))
        or (
            isinstance(path, list)
            and all(isinstance(item, str) and item for item in path)
        )
    )
    if not valid_paths or not valid_path:
        raise InvalidProjectConfigError(
            f"Project paths must contain non-empty strings in {config_path}"
        )


@dataclass(frozen=True)
class _ProjectSyncInputs:
    skills: list[str]
    memory_files: list[str]
    sync_mode: str
    agents_dir: Path
    agents_skills_dir: Path
    agents_memory_dir: Path
    proj_mem_source: Path
    skill_cleanup: RuntimeCleanupPlan
    memory_cleanup: RuntimeCleanupPlan
    cleanup_conflicts: tuple[Path, ...]


def _resolve_project_sync_inputs(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    data: dict[str, Any],
) -> _ProjectSyncInputs:
    skills = [str(name) for name in data.get("skills", [])]
    memory_files = [str(name) for name in data.get("memory", [])]
    sync_mode = str(data.get("sync_mode", "link")).lower()

    agents_dir = project_path / ".agents"
    agents_skills_dir = agents_dir / "skills"
    agents_memory_dir = agents_dir / "memory"

    proj_mem_source = aikito_dir / "projects" / project_name / "memory"
    if not proj_mem_source.exists():
        proj_mem_source = aikito_dir / "memory" / project_name

    selected_skills = set(skills)
    selected_memory = {Path(name).parts[0] for name in memory_files if Path(name).parts}
    if proj_mem_source.is_dir():
        selected_memory.update(item.name for item in proj_mem_source.iterdir())

    skill_cleanup = plan_runtime_cleanup(
        agents_skills_dir,
        selected_skills,
        (aikito_dir / "skills",),
        allow_matching_copies=False,
    )
    memory_cleanup = plan_runtime_cleanup(
        agents_memory_dir,
        selected_memory,
        (aikito_dir / "memory", aikito_dir / "projects" / project_name / "memory"),
        allow_matching_copies=False,
    )
    selected_skill_conflicts = find_selected_runtime_conflicts(
        agents_skills_dir,
        selected_skills,
        aikito_dir / "skills",
        allow_drifted_copies=sync_mode == "copy",
    )
    cleanup_conflicts = (*memory_cleanup.conflicts, *selected_skill_conflicts)

    return _ProjectSyncInputs(
        skills=skills,
        memory_files=memory_files,
        sync_mode=sync_mode,
        agents_dir=agents_dir,
        agents_skills_dir=agents_skills_dir,
        agents_memory_dir=agents_memory_dir,
        proj_mem_source=proj_mem_source,
        skill_cleanup=skill_cleanup,
        memory_cleanup=memory_cleanup,
        cleanup_conflicts=cleanup_conflicts,
    )


def collect_project_prepare_errors(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    data: dict[str, Any],
    home: Path,
    *,
    force: bool = False,
) -> list[str]:
    """Return every conflict before any persistent project resource is changed."""
    errors: list[str] = []
    validation_error = project_sync_validation_error(
        aikito_dir, project_name, project_path, home
    )
    if validation_error:
        errors.append(validation_error)

    inputs = _resolve_project_sync_inputs(aikito_dir, project_name, project_path, data)
    errors.extend(
        f"Project skill source does not exist: {aikito_dir / 'skills' / skill_name}"
        for skill_name in inputs.skills
        if not (aikito_dir / "skills" / skill_name).is_dir()
    )
    errors.extend(
        f"Project memory source does not exist: {aikito_dir / 'memory' / memory_file}"
        for memory_file in inputs.memory_files
        if not (aikito_dir / "memory" / memory_file).exists()
    )
    errors.extend(
        f"Unmanaged project runtime item: {path}" for path in inputs.cleanup_conflicts
    )

    if inputs.sync_mode == "copy":
        states = collect_single_project_skill_states(
            aikito_dir, project_name, project_path, inputs.skills
        )
        conflicts = [state for state in states if state.status == "CONFLICT"]
        drifted = [state for state in states if state.status == "DRIFT"]
        errors.extend(
            f"Project skill {project_name}/{state.skill_name}: {state.reason}"
            for state in conflicts
        )
        if drifted and not force:
            errors.extend(
                f"Project skill {project_name}/{state.skill_name} drifted "
                f"at {state.runtime_path}"
                for state in drifted
            )
            errors.append(
                "Copied project skills contain drift. Run 'aikito diff' "
                "and reconcile changes, or use --force after review."
            )
    return errors


def sync_project_path(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    data: dict[str, Any],
    home: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Synchronize one preflighted project path using existing ownership rules."""
    inputs = _resolve_project_sync_inputs(aikito_dir, project_name, project_path, data)
    operation = "Previewing sync for" if dry_run else "Syncing"
    print(f"[INFO] {operation} project '{project_name}' (mode: {inputs.sync_mode})")

    for path in inputs.skill_cleanup.conflicts:
        print(f"[INFO] Preserving project-owned skill: {path}")
    apply_runtime_cleanup(
        (*inputs.skill_cleanup.cleanup, *inputs.memory_cleanup.cleanup), dry_run
    )

    if not dry_run:
        ensure_dir(inputs.agents_dir)
        ensure_dir(inputs.agents_skills_dir)
        ensure_dir(inputs.agents_memory_dir)

    for skill_name in inputs.skills:
        source = aikito_dir / "skills" / skill_name
        target = inputs.agents_skills_dir / skill_name
        if not sync_resource(source, target, mode=inputs.sync_mode, dry_run=dry_run):
            raise RuntimeError(f"Failed to synchronize project skill: {skill_name}")

    for memory_file in inputs.memory_files:
        source = aikito_dir / "memory" / memory_file
        target = inputs.agents_memory_dir / memory_file
        if not sync_resource(source, target, mode="link", dry_run=dry_run):
            raise RuntimeError(f"Failed to synchronize project memory: {memory_file}")

    if inputs.proj_mem_source.is_dir():
        for item in inputs.proj_mem_source.iterdir():
            target = inputs.agents_memory_dir / item.name
            if not sync_resource(item, target, mode="link", dry_run=dry_run):
                raise RuntimeError(
                    f"Failed to synchronize project memory item: {item.name}"
                )
    else:
        print(f"[INFO] No project memory dir found at {inputs.proj_mem_source}")

    project_instructions = aikito_dir / "projects" / project_name / "AGENTS.md"
    if not project_instructions.exists():
        print(
            f"[INFO] No AGENTS.md found for project '{project_name}' at "
            f"{project_instructions}"
        )
        return

    instruction_targets = collect_project_instruction_targets(
        aikito_dir, project_path, home
    )
    instructions_enabled = bool(
        project_instructions.read_text(encoding="utf-8", errors="replace").strip()
    )
    possible_stale_targets = {inputs.agents_dir / "AGENTS.md"}
    if not instructions_enabled:
        possible_stale_targets.update(instruction_targets)
    managed_stale_targets = tuple(
        sorted(
            target
            for target in possible_stale_targets
            if target.is_symlink()
            and target.resolve(strict=False)
            == project_instructions.resolve(strict=False)
        )
    )
    apply_runtime_cleanup(managed_stale_targets, dry_run)
    if not instructions_enabled:
        print(
            f"[INFO] Project instructions are empty for '{project_name}'; "
            "no Agent-native instruction links are required."
        )
        return

    for target, agent_names in instruction_targets.items():
        print(f"[INFO] Project instructions for {', '.join(agent_names)}")
        if not sync_project_instruction(project_instructions, target, dry_run):
            raise RuntimeError(f"Project instructions already exist: {target}")


__all__ = [
    "AmbiguousProjectPathError",
    "InvalidProjectConfigError",
    "NoAvailableProjectPathError",
    "PreparedProject",
    "Project",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectPrepareConflictError",
    "UnsupportedProjectAgentError",
]
