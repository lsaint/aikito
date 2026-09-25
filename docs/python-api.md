# Python API Reference

The `aikito` package exposes a small, stable public API for integrating project
preparation into external runners and CI pipelines. Import directly from
`aikito`; internal modules are not part of the public API surface.

```python
from aikito import (
    Project,
    PreparedProject,
    Workspace,
    WorkspaceInspection,
    WorkspaceSyncPreview,
    WorkspaceFinding,
    WorkspaceProjectView,
)
```

---

## `Project`

A canonical project definition loaded from one Aikito workspace.

### `Project.load`

```python
@classmethod
def load(
    name: str,
    workspace: Path | str | None = None,
    home: Path | str | None = None,
) -> Project
```

Load a named project without modifying the workspace.

**Parameters**

- **`name`** `str` — Project name as it appears in the workspace's `projects/` directory.
  Must match `[A-Za-z0-9][A-Za-z0-9._-]*`.
- **`workspace`** `Path | str | None` — Absolute path to the Aikito workspace.
  Defaults to the workspace resolved from `home`.
- **`home`** `Path | str | None` — Home directory used for workspace resolution.
  Defaults to the current user's home directory.

**Returns** a `Project` instance.

**Raises**

- [`InvalidProjectConfigError`](#invalidprojectconfigerror) – name is invalid, workspace path is not absolute, or `agent.toml` cannot be parsed.
- [`ProjectNotFoundError`](#projectnotfounderror) – no `agent.toml` exists for the given name.

---

### `Project.prepare`

```python
def prepare(
    self,
    agent: str,
    path: Path | str | None = None,
) -> PreparedProject
```

Apply persistent project sync rules and return Agent launch inputs.

Preparation synchronises project-scoped instructions, selected skills, and
memory. It does **not** launch the Agent or synchronise global instructions,
global skills, MCP servers, or subagents.

**Parameters**

- **`agent`** `str` — Agent name as configured in the workspace's `agents/<name>.toml`.
- **`path`** `Path | str | None` — Optional explicit target directory. When omitted, the project
  must have exactly one active path on the current host. When provided, that directory is prepared
  directly without modifying `agent.toml`; intended for ephemeral CI and deployment checkouts.

**Returns** a [`PreparedProject`](#preparedproject).

**Raises**

- [`UnsupportedProjectAgentError`](#unsupportedprojectagenterror) – `agent` is not configured in `agents/<name>.toml`.
- [`ProjectPrepareConflictError`](#projectprepareconflicterror) – managed resources cannot be synchronised safely, or symlink support is unavailable.
- [`NoAvailableProjectPathError`](#noavailableprojectpatherror) – no configured path exists on this host (when `path` is omitted), or the explicit `path` does not exist.
- [`AmbiguousProjectPathError`](#ambiguousprojectpatherror) – multiple paths are active and no explicit `path` was supplied.
- [`InvalidProjectConfigError`](#invalidprojectconfigerror) – `path` is empty, not a string/Path, or exists but is not a directory.

---

### `Project.add_path`

```python
def add_path(self, path: Path | str) -> Project
```

Register a new candidate path in the project's `agent.toml` and return an
updated `Project` instance. The original instance is unchanged.

**Parameters**

- **`path`** `Path | str` — Absolute or home-relative path to the checkout directory.

**Returns** a fresh `Project` loaded after the append.

**Raises**

- [`InvalidProjectConfigError`](#invalidprojectconfigerror) – path is empty, not a string/Path, or the append failed.
- [`ProjectNotFoundError`](#projectnotfounderror) – `agent.toml` is missing.

---

### `Project.paths`

```python
@property
def paths(self) -> tuple[Path, ...]
```

All configured paths resolved for the current host, regardless of whether
they exist.

---

### `Project.resolve_path`

```python
def resolve_path(self) -> Path
```

Return the sole active path, refusing to guess between multiple checkouts.

**Raises**

- [`NoAvailableProjectPathError`](#noavailableprojectpatherror) – no configured path exists on this host.
- [`AmbiguousProjectPathError`](#ambiguousprojectpatherror) – more than one configured path exists on this host.

---

## `PreparedProject`

A frozen dataclass returned by [`Project.prepare`](#projectprepare).

```python
@dataclass(frozen=True)
class PreparedProject:
    name: str                         # Project name
    agent: str                        # Agent name
    cwd: Path                         # Resolved working directory for the Agent
    env_overrides: Mapping[str, str]  # Read-only environment overrides
```

Pass `cwd` as the working directory and merge `env_overrides` into the
subprocess environment when launching the Agent.

---

## Exceptions

All exceptions inherit from `ProjectError → RuntimeError`.

### `ProjectError`

Base class for all public Aikito project API failures.

### `ProjectNotFoundError`

Raised when a named project is absent from the selected workspace.

### `InvalidProjectConfigError`

Raised when project or Agent configuration cannot be loaded safely (bad TOML,
missing files, or invalid parameter types).

### `NoAvailableProjectPathError`

Raised when none of a project's configured paths exists on this host, or when
an explicit `path` supplied to `Project.prepare()` does not exist.

### `AmbiguousProjectPathError`

Raised when several project paths are active on this host and no explicit
`path` was supplied.

Attributes:

- **`project_name`** `str` — Project name.
- **`paths`** `tuple[Path, ...]` — All active paths.

### `UnsupportedProjectAgentError`

Raised when the requested agent is not configured in `agents/<name>.toml`.

### `ProjectPrepareConflictError`

Raised when managed project resources cannot be synchronised safely.

Attributes:

- **`project_name`** `str` — Project name.
- **`conflicts`** `tuple[str, ...]` — Human-readable conflict descriptions.

### `WorkspaceError`

Base class for all public Aikito workspace API failures (`RuntimeError`).

### `WorkspaceNotFoundError`

Raised when a specified or resolved workspace directory does not exist (`WorkspaceError`, `FileNotFoundError`).

### `InvalidWorkspaceError`

Raised when a workspace path is relative or malformed (`WorkspaceError`, `ValueError`).

---

## Usage Examples

### Minimal

```python
from aikito import Project

project = Project.load("example")
prepared = project.prepare(agent="agy")
# Launch agent with cwd=prepared.cwd, env merged with prepared.env_overrides
```

### Explicit path (CI / ephemeral checkout)

```python
from aikito import Project

project = Project.load("example")
prepared = project.prepare(agent="agy", path="/path/to/checkout")
```

### Register a new path before preparing

```python
from aikito import Project

project = Project.load("example")
project = project.add_path("/path/to/new-checkout")
prepared = project.prepare(agent="agy")
```

### Error handling

```python
from aikito import (
    Project,
    AmbiguousProjectPathError,
    NoAvailableProjectPathError,
    ProjectPrepareConflictError,
    UnsupportedProjectAgentError,
)

try:
    project = Project.load("example")
    prepared = project.prepare(agent="agy")
except UnsupportedProjectAgentError as e:
    print(f"Agent not configured: {e}")
except (NoAvailableProjectPathError, AmbiguousProjectPathError) as e:
    print(f"Path resolution failed: {e}")
except ProjectPrepareConflictError as e:
    print(f"Conflicts: {', '.join(e.conflicts)}")
```


---

## `Workspace`

A public facade for inspecting and planning operations on an Aikito workspace.

### `Workspace.load`

```python
@classmethod
def load(
    workspace: Path | str | None = None,
    home: Path | str | None = None,
) -> Workspace
```

Load an Aikito workspace strictly read-only without modifying the pointer file (`~/.config/aikito/workspace`) or creating files/directories.

**Parameters**

- **`workspace`** `Path | str | None` — Absolute path to the Aikito workspace. Defaults to the resolved workspace.
- **`home`** `Path | str | None` — Home directory used for workspace resolution. Defaults to user home.

**Returns** a `Workspace` instance.

**Raises**

- [`InvalidWorkspaceError`](#invalidworkspaceerror) – workspace path is relative or malformed.
- [`WorkspaceNotFoundError`](#workspacenotfounderror) – the specified workspace directory does not exist.

---

### `Workspace.inspect`

```python
def inspect(self) -> WorkspaceInspection
```

Return a strictly read-only structured inspection of the workspace, including configured agents, projects, MCP servers, skills, subagents, and doctor findings.

**Returns** a [`WorkspaceInspection`](#workspaceinspection).

---

### `Workspace.plan_sync`

```python
def plan_sync(self) -> WorkspaceSyncPreview
```

Return a strictly read-only synchronization preview without mutating files, acquiring writer locks, or creating backups.

**Returns** a [`WorkspaceSyncPreview`](#workspacesyncpreview).

---

## Data Models

### `WorkspaceFinding`

Frozen dataclass exposing:

- **`status`** `str` — Severity status (`"FAIL"`, `"WARN"`, etc.).
- **`code`** `str` — Machine-readable diagnostic rule code.
- **`message`** `str` — Human-readable finding description.
- **`resource`** `str` — Associated file path or resource key.
- **`fix_hint`** `str` — Suggested remediation step.

### `WorkspaceProjectView`

Frozen dataclass exposing:

- **`name`** `str` — Project name.
- **`status`** `str` — Binding status (`"OK"`, `"OFFLINE"`, `"UNBOUND"`, `"CONFLICT"`).
- **`active_paths`** `tuple[str, ...]` — Active checkout paths on this host.
- **`offline_paths`** `tuple[str, ...]` — Configured paths that do not exist on this host.

### `WorkspaceInspection`

Frozen dataclass exposing:

- **`workspace_dir`** `Path` — Absolute path to the workspace.
- **`configured_agents`** `tuple[str, ...]` — Configured agent identifiers.
- **`projects`** `tuple[WorkspaceProjectView, ...]` — Project view snapshots.
- **`diagnostics`** `tuple[WorkspaceFinding, ...]` — Actionable warnings or failures.
- **`mcps`** `tuple[str, ...]` — Canonical MCP server names.
- **`skills`** `tuple[str, ...]` — Canonical skill names.
- **`subagents`** `tuple[str, ...]` — Canonical subagent names.
- **`ready_for_sync`** `bool` — True if no blocking errors prevent synchronization.

### `WorkspaceSyncPreview`

Frozen dataclass exposing:

- **`workspace_path`** `Path` — Absolute path to the workspace.
- **`changes`** `int` — Count of planned changes.
- **`unchanged`** `int` — Count of unchanged resources.
- **`offline`** `int` — Count of offline projects.
- **`warnings`** `int` — Count of warnings.
- **`conflicts`** `int` — Count of conflicts.
- **`errors`** `int` — Count of blocking errors.
- **`can_apply`** `bool` — True if safe to apply.
- **`will_mutate`** `bool` — True if changes > 0.
- **`findings`** `tuple[WorkspaceFinding, ...]` — Issues found during planning.
- **`operations`** `tuple[str, ...]` — Sanitized operation descriptions.

---

!!! note
    `Project.prepare()` accepts no `--force` parameter and does not reuse
    CLI force authorization. For manual project synchronisation or force
    overwrites use `aikito sync project <name> --force`. See
    [Engineering Invariants](architecture/invariants-api.md#inv-api-05)
    for the API invariant specification.

