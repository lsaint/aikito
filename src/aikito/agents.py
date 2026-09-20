"""Agent platform identities, registry loading, and availability inspection."""

from __future__ import annotations

import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Mapping

from .compat import get_physical_path, is_directory_case_sensitive

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


# Canonical "is this Agent installed on this machine" registry.
# Shared by init detection, doctor diagnostics, and synchronization gating.
# A value is (display_name, binary_on_path, home-relative marker directory);
# either signal counts as installed.
AGENT_INSTALL_MARKERS: dict[str, tuple[str, str, Path]] = {
    "codex": ("Codex", "codex", Path(".codex")),
    "claude-code": ("Claude Code", "claude", Path(".claude")),
    "agy": ("Antigravity CLI", "agy", Path(".gemini/config")),
    "opencode": ("OpenCode", "opencode", Path(".config/opencode")),
    "github-copilot": ("GitHub Copilot CLI", "copilot", Path(".copilot")),
    "dsh": ("DeepSeek Harness", "dsh", Path(".dsh")),
    "grok": ("Grok Build", "grok", Path(".grok")),
    "pi": ("Pi", "pi", Path(".pi")),
}


@dataclass(frozen=True)
class AgentAvailability:
    """Tri-state availability of an Agent platform with evidence."""

    status: str  # "installed", "not_installed", "unknown"
    evidence: str

    @property
    def is_installed(self) -> bool:
        return self.status == "installed"

    @property
    def is_not_installed(self) -> bool:
        return self.status == "not_installed"

    @property
    def is_unknown(self) -> bool:
        return self.status == "unknown"


@dataclass(frozen=True)
class Agent:
    """Identity and declarative resource paths for one target agent platform."""

    name: str
    display_name: str
    instruction_path: Path | None = None
    project_instruction_path: Path | None = None
    skills_path: Path | None = None


class AgentRegistryError(ValueError):
    """Raised when agents.toml cannot be loaded or validated."""


def load_agent_document(aikito_dir: Path) -> Mapping[str, Any]:
    """Load and validate the top-level agents.toml document."""
    if not aikito_dir.exists() or not aikito_dir.is_dir():
        raise AgentRegistryError(
            f"Aikito workspace directory not found: {aikito_dir}. "
            "Run 'aikito init workspace' to initialize."
        )
    config_path = aikito_dir / "agents.toml"
    if not config_path.exists():
        raise AgentRegistryError(
            f"Agents config not found: {config_path}. "
            "Run 'aikito init workspace' to initialize."
        )
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise AgentRegistryError(f"Invalid agents config {config_path}: {exc}") from exc
    agents = document.get("agents")
    if not isinstance(agents, dict):
        raise AgentRegistryError(f"'agents' must be a table in {config_path}")
    return document


def _resolve_home_path(home: Path, value: object, field: str, agent: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentRegistryError(f"Agent '{agent}' requires a string '{field}'")
    return home / value


def _resolve_project_path(value: object, field: str, agent: str) -> Path:
    if not isinstance(value, str) or not value:
        raise AgentRegistryError(f"Agent '{agent}' requires a string '{field}'")
    path = Path(value)
    if path.is_absolute() or path == Path(".") or ".." in path.parts:
        raise AgentRegistryError(
            f"Agent '{agent}' requires a safe relative '{field}', got: {value}"
        )
    return path


def check_agent_availability(
    agent: str | Agent,
    home: Path,
    target_path: Path | None = None,
) -> AgentAvailability:
    """Determine whether an agent is installed, not installed, or unknown."""
    agent_name = agent.name if isinstance(agent, Agent) else str(agent)
    marker = AGENT_INSTALL_MARKERS.get(agent_name)
    if marker is not None:
        _display, binary, relative_marker = marker
        if shutil.which(binary):
            return AgentAvailability("installed", "binary_on_path")
        if (home / relative_marker).exists():
            return AgentAvailability("installed", "marker_directory")
        return AgentAvailability("not_installed", "marker_not_found")

    # Custom agent without install marker
    cand_parent = target_path.parent if target_path is not None else None
    if cand_parent is None and isinstance(agent, Agent):
        if agent.skills_path is not None:
            cand_parent = agent.skills_path.parent
        elif agent.instruction_path is not None:
            cand_parent = agent.instruction_path.parent
    if cand_parent is not None and cand_parent.exists():
        return AgentAvailability("installed", "target_parent_exists")
    return AgentAvailability("unknown", "cannot_determine")


def is_agent_installed(
    agent_name: str,
    home: Path,
    target_path: Path | None = None,
) -> bool | None:
    """Return the canonical install state for an agent (True/False/None)."""
    avail = check_agent_availability(agent_name, home, target_path=target_path)
    if avail.is_installed:
        return True
    if avail.is_not_installed:
        return False
    return None


@dataclass(frozen=True)
class AgentRegistry:
    """Registry of configured Agents loaded from agents.toml."""

    agents: dict[str, Agent]

    @classmethod
    def from_document(cls, document: Mapping[str, Any], home: Path) -> AgentRegistry:
        """Build the base Agent registry from one parsed agents.toml document."""
        agents_data = document.get("agents")
        if not isinstance(agents_data, dict):
            raise AgentRegistryError("'agents' must be a table")

        loaded: dict[str, Agent] = {}
        for name, spec in agents_data.items():
            if not isinstance(spec, dict):
                raise AgentRegistryError(f"Agent '{name}' must be a table")
            instruction_value = spec.get("instruction_path")
            instr_path = (
                _resolve_home_path(home, instruction_value, "instruction_path", name)
                if instruction_value is not None
                else None
            )
            project_instruction_value = spec.get("project_instruction_path")
            proj_instr_path = (
                _resolve_project_path(
                    project_instruction_value, "project_instruction_path", name
                )
                if project_instruction_value is not None
                else None
            )
            skills_value = spec.get("skills_path")
            skills_path = (
                _resolve_home_path(home, skills_value, "skills_path", name)
                if skills_value is not None
                else None
            )
            loaded[name] = Agent(
                name=name,
                display_name=str(spec.get("display_name", name)),
                instruction_path=instr_path,
                project_instruction_path=proj_instr_path,
                skills_path=skills_path,
            )
        return cls(loaded)

    @classmethod
    def load_strict(cls, aikito_dir: Path, home: Path) -> AgentRegistry:
        """Load agents.toml and raise AgentRegistryError on invalid input."""
        return cls.from_document(load_agent_document(aikito_dir), home)

    @classmethod
    def load(cls, aikito_dir: Path, home: Path) -> AgentRegistry:
        """Load agents.toml, returning an empty registry for invalid input."""
        try:
            return cls.load_strict(aikito_dir, home)
        except AgentRegistryError:
            return cls({})

    def get(self, name: str) -> Agent | None:
        return self.agents.get(name)

    def __getitem__(self, name: str) -> Agent:
        return self.agents[name]

    def __contains__(self, name: str) -> bool:
        return name in self.agents

    def __iter__(self) -> Iterator[str]:
        return iter(self.agents)

    def values(self):
        return self.agents.values()

    def items(self):
        return self.agents.items()

    def __len__(self) -> int:
        return len(self.agents)


@dataclass(frozen=True)
class Target:
    """A resolved physical target for a resource with associated agent consumers.

    Kinds:
    - managed_entry: Single managed directory item (e.g. ~/.agents/skills/<name>).
    - managed_container: Managed container whose children are individually managed (e.g. ~/.agents/skills).
    - consumer_link: Agent consumer link pointing to a container or file (e.g. ~/.claude/skills).
    """

    kind: str  # "managed_entry", "managed_container", "consumer_link"
    scope: str  # "global", "project"
    path: Path
    canonical_source: Path | None = None
    consumers: tuple[str, ...] = ()  # Agent names that consume this target
    consumer_display_names: tuple[str, ...] = ()

    @property
    def is_same_object(self) -> bool:
        """Return True if target path and canonical source resolve to the exact same filesystem object."""
        if self.canonical_source is None:
            return False

        try:
            return _normalized_physical_path(self.path) == _normalized_physical_path(
                self.canonical_source
            )
        except Exception:
            try:
                return self.path.resolve(strict=False) == self.canonical_source.resolve(
                    strict=False
                )
            except Exception:
                return False


def _normalized_physical_path(path: Path) -> str:
    """Return the normalized identity of the filesystem object at path."""
    physical = get_physical_path(path)
    probe = physical if physical.is_dir() else physical.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    normalized = str(physical)
    if probe.exists() and not is_directory_case_sensitive(probe):
        return normalized.casefold()
    return normalized


def _physical_target_key(path: Path) -> str:
    """Identify a managed directory entry without following its final symlink."""
    physical_parent = get_physical_path(path.parent)
    probe = physical_parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    entry_name = path.name
    if probe.exists() and not is_directory_case_sensitive(probe):
        return str(physical_parent).casefold() + "/" + entry_name.casefold()
    return str(physical_parent) + "/" + entry_name


def _group_agent_paths(
    entries: Iterator[tuple[Path, str, str]],
) -> list[tuple[Path, list[tuple[str, str]]]]:
    """Group configured paths by physical filesystem identity."""
    grouped: dict[str, tuple[Path, list[tuple[str, str]]]] = {}
    for path, name, display_name in entries:
        key = _physical_target_key(path)
        if key not in grouped:
            grouped[key] = (path, [])
        grouped[key][1].append((name, display_name))
    return sorted(grouped.values(), key=lambda item: str(item[0]))


def check_target_availability(
    target: Target,
    home: Path,
) -> AgentAvailability:
    """Check availability across all consumers of a Target."""
    statuses = [
        check_agent_availability(consumer, home, target_path=target.path)
        for consumer in target.consumers
    ]
    if any(s.is_installed for s in statuses):
        installed_ev = next(s.evidence for s in statuses if s.is_installed)
        return AgentAvailability("installed", installed_ev)
    if statuses and all(s.is_not_installed for s in statuses):
        return AgentAvailability("not_installed", "all_consumers_not_installed")
    return AgentAvailability("unknown", "cannot_determine")


def resolve_targets(
    resource_kind: str,
    aikito_dir: Path,
    home: Path,
    *,
    project_path: Path | None = None,
    project_name: str | None = None,
    active_only: bool = False,
    registry: AgentRegistry | None = None,
) -> tuple[Target, ...]:
    """Resolve physical targets and deduplicate shared agent consumers.

    Supported resource kinds:
    - 'global_skills': Returns Target for agent consumer links (consumer_link).
      Deduplicates 8 bundled agents into 3 physical targets.
    - 'global_instructions': Returns Target for each agent's global AGENTS.md link (consumer_link).
    - 'project_instructions': Returns Target for project AGENTS.md links (consumer_link).
    """
    if registry is None:
        registry = AgentRegistry.load(aikito_dir, home)

    if resource_kind == "global_skills":
        canonical_source = home / ".agents" / "skills"
        grouped = _group_agent_paths(
            (
                (agent.skills_path, agent.name, agent.display_name)
                for agent in registry.values()
                if agent.skills_path is not None
            )
        )

        targets: list[Target] = []
        for path, consumers in grouped:
            names = tuple(c[0] for c in consumers)
            display_names = tuple(c[1] for c in consumers)
            t = Target(
                kind="consumer_link",
                scope="global",
                path=path,
                canonical_source=canonical_source,
                consumers=names,
                consumer_display_names=display_names,
            )
            if active_only:
                avail = check_target_availability(t, home)
                if not avail.is_installed and not path.parent.exists():
                    continue
            targets.append(t)
        return tuple(targets)

    elif resource_kind == "global_instructions":
        canonical_source = aikito_dir / "global" / "AGENTS.md"
        grouped = _group_agent_paths(
            (
                (agent.instruction_path, agent.name, agent.display_name)
                for agent in registry.values()
                if agent.instruction_path is not None
            )
        )

        targets = []
        for path, consumers in grouped:
            names = tuple(c[0] for c in consumers)
            display_names = tuple(c[1] for c in consumers)
            t = Target(
                kind="consumer_link",
                scope="global",
                path=path,
                canonical_source=canonical_source,
                consumers=names,
                consumer_display_names=display_names,
            )
            if active_only:
                avail = check_target_availability(t, home)
                if not avail.is_installed and not path.parent.exists():
                    continue
            targets.append(t)
        return tuple(targets)

    elif resource_kind == "project_instructions":
        if project_path is None:
            return ()
        canonical_source = (
            aikito_dir / "projects" / project_name / "AGENTS.md"
            if project_name
            else None
        )
        grouped = _group_agent_paths(
            (
                (
                    project_path / agent.project_instruction_path,
                    agent.name,
                    agent.display_name,
                )
                for agent in registry.values()
                if agent.project_instruction_path is not None
            )
        )

        targets = []
        for path, consumers in grouped:
            names = tuple(c[0] for c in consumers)
            display_names = tuple(c[1] for c in consumers)
            t = Target(
                kind="consumer_link",
                scope="project",
                path=path,
                canonical_source=canonical_source,
                consumers=names,
                consumer_display_names=display_names,
            )
            if active_only and not path.parent.exists():
                avail = check_target_availability(t, home)
                if not avail.is_installed:
                    continue
            targets.append(t)
        return tuple(targets)

    else:
        raise ValueError(
            f"Unknown resource kind for target resolution: {resource_kind}"
        )
