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

from .compat import can_symlink, safe_relative_path
from .conflict import collect_resource_conflicts
from .mcp import MCPConfigError, load_agents
from .project import (
    _resolve_project_path,
    append_candidate_path_to_config,
    resolve_project_binding,
)
from .project_sync import apply_project_sync_batch, build_project_sync_batch
from .workspace import resolve_workspace

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
    def load(
        cls,
        name: str,
        workspace: Path | str | None = None,
        home: Path | str | None = None,
    ) -> Project:
        """Load a named project without changing the persistent workspace pointer."""
        if not isinstance(name, str) or not PROJECT_NAME_RE.fullmatch(name):
            raise InvalidProjectConfigError(f"Invalid Aikito project name: {name!r}")

        home_path = (
            Path.home().resolve() if home is None else Path(home).expanduser().resolve()
        )
        if workspace is None:
            workspace_path = resolve_workspace(home_path)
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
        conflicts = collect_resource_conflicts([config_path], home_path)
        if conflicts:
            raise InvalidProjectConfigError("; ".join(conflicts))
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
        return cls(name, workspace_path, home_path, MappingProxyType(config))

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

    def add_path(self, path: Path | str) -> Project:
        """Register a new candidate path in the project's agent.toml and return updated Project."""
        resolved = _resolve_supplied_project_path(path, self._home)

        config_path = self.workspace / "projects" / self.name / "agent.toml"
        if not config_path.is_file():
            raise ProjectNotFoundError(
                f"Aikito project config not found: {config_path}"
            )

        raw_to_append = safe_relative_path(resolved, self._home)
        try:
            append_candidate_path_to_config(config_path, raw_to_append, self._home)
        except Exception as exc:
            raise InvalidProjectConfigError(
                f"Failed to append candidate path to {config_path}: {exc}"
            ) from exc

        return self.load(self.name, workspace=self.workspace, home=self._home)

    def prepare(
        self,
        agent: str,
        path: Path | str | None = None,
    ) -> PreparedProject:
        """Prepare persistent project resources and return Agent launch inputs."""
        try:
            agents = load_agents(self.workspace, self._home)
        except MCPConfigError as exc:
            raise InvalidProjectConfigError(str(exc)) from exc
        if agent not in agents:
            raise UnsupportedProjectAgentError(
                f"Unsupported Aikito project agent: {agent}; agent is not configured"
            )
        if not can_symlink():
            raise ProjectPrepareConflictError(
                self.name,
                ("Symbolic link support is required to prepare project resources",),
            )

        if path is None:
            project_path = self.resolve_path()
        else:
            project_path = _resolve_supplied_project_path(path, self._home)

        batch = build_project_sync_batch(
            self.workspace,
            self._home,
            self.name,
            dict(self._config),
            explicit_path=project_path,
            force=False,
            register_explicit_path=False,
        )
        if not batch.can_apply:
            errors = list(batch.preflight_findings)
            for op in batch.skill_plan.operations:
                if op.finding and op.finding not in errors:
                    errors.append(op.finding)
            if batch.instruction_plan:
                for op in batch.instruction_plan.operations:
                    if op.finding and op.finding not in errors:
                        errors.append(op.finding)
            if batch.memory_plan:
                for op in batch.memory_plan.operations:
                    if op.finding and op.finding not in errors:
                        errors.append(op.finding)
            raise ProjectPrepareConflictError(
                self.name, tuple(errors or ["Project preparation conflict detected"])
            )

        result = apply_project_sync_batch(
            batch, dict(self._config), self._home, dry_run=False
        )
        if not result.is_success:
            raise ProjectPrepareConflictError(
                self.name, (result.error_message or "Project preparation failed",)
            )

        return PreparedProject(
            name=self.name,
            agent=agent,
            cwd=project_path,
            env_overrides=MappingProxyType({}),
        )


def _resolve_supplied_project_path(path: Path | str, home: Path) -> Path:
    """Resolve a caller-supplied path against the Project's captured home."""
    if not isinstance(path, (str, Path)):
        raise InvalidProjectConfigError(
            f"Project path must be a string or Path, got {type(path).__name__}"
        )
    raw = str(path).strip()
    if not raw:
        raise InvalidProjectConfigError("Project path cannot be empty")
    resolved = _resolve_project_path(raw, home)
    if resolved is None:
        raise InvalidProjectConfigError(f"Invalid project path: {path!r}")
    if not resolved.exists():
        raise NoAvailableProjectPathError(f"Project path does not exist: {resolved}")
    if not resolved.is_dir():
        raise InvalidProjectConfigError(f"Project path is not a directory: {resolved}")
    return resolved


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


def sync_project_path(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    data: dict[str, Any],
    home: Path,
    *,
    dry_run: bool = False,
) -> None:
    """Synchronize one preflighted project path using project skill sync engine."""
    batch = build_project_sync_batch(
        aikito_dir, home, project_name, data, explicit_path=project_path, force=False
    )
    if not batch.can_apply:
        errs = list(batch.preflight_findings)
        for op in batch.skill_plan.operations:
            if op.finding and op.finding not in errs:
                errs.append(op.finding)
        raise RuntimeError(
            "; ".join(errs or [f"Project sync failed for '{project_name}'"])
        )

    res = apply_project_sync_batch(batch, data, home, dry_run=dry_run)
    if not res.is_success:
        raise RuntimeError(
            res.error_message or f"Project sync failed for '{project_name}'"
        )


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
