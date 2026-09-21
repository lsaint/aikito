# Public Python API Invariants

### INV-API-01: Exported Symbols `[current]` {: #inv-api-01 }

The public API is strictly defined by `src/aikito/__init__.py::__all__`:
```python
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
    "__version__",
]
```
Internal modules (`aikito.project`, `aikito.sync`, etc.) are private and must not be imported by external consumers.

### INV-API-02: `Project.load` Contract `[current]` {: #inv-api-02 }

- Signature: `Project.load(name: str, workspace: Path | str | None = None, home: Path | str | None = None) -> Project`
- Strictly read-only: Loads project definition from workspace `projects/<name>/agent.toml`.
- Does not modify workspace pointer (`~/.config/aikito/workspace`) or workspace files.
- Raises `ProjectNotFoundError` if `projects/<name>/agent.toml` does not exist.
- Raises `InvalidProjectConfigError` if name is invalid, workspace path is relative, or TOML is malformed.

### INV-API-03: Path Resolution Contract `[current]` {: #inv-api-03 }

- `Project.paths`: Property returning `tuple[Path, ...]`, containing all configured candidate paths resolved against the host. Does not filter by existence.
- `Project.resolve_path()`: Returns the unique active checkout `Path`.
- Raises `NoAvailableProjectPathError` if no configured path exists on the current host.
- Raises `AmbiguousProjectPathError` if more than one configured path exists on the current host.

### INV-API-04: `Project.add_path` Contract `[current]` {: #inv-api-04 }

- Signature: `Project.add_path(path: Path | str) -> Project`
- Preserves caller immutability: Original `Project` instance remains unchanged; returns a freshly loaded `Project` reflecting the appended path.
- Appends candidate path to project's `agent.toml`. Does not prepare resources, create directories, or launch agents.

### INV-API-05: `Project.prepare` Contract `[current]` {: #inv-api-05 }

- Signature: `Project.prepare(agent: str, path: Path | str | None = None) -> PreparedProject`
- **Scope**: Prepares project-scoped instructions (`AGENTS.md`), selected skills (`.agents/skills/`), and memory (`.agents/memory/`).
- **Boundaries**:
  - Does **not** launch the agent subprocess.
  - Does **not** synchronize global instructions, global skills, MCP servers, or subagents.
  - Does **not** modify workspace pointer or update project configuration.
  - When explicit `path` is passed, it is used directly without writing it into `agent.toml`.
- **Exception Contract**:
  - `UnsupportedProjectAgentError`: `agent` is not registered in workspace `agents.toml`.
  - `ProjectPrepareConflictError`: Managed resources cannot be safely synchronized, symlink capability is missing, or conflict markers are detected.
  - `NoAvailableProjectPathError`: Raised when `path` is omitted and no configured path exists on this host, **or** when explicit `path` does not exist.
  - `InvalidProjectConfigError`: Raised when explicit `path` is empty, of invalid type, or exists but is not a directory.
- **No Force Parameter**: `prepare()` has no `force` parameter and does not adopt CLI force authorization.

### INV-API-06: `PreparedProject` Contract `[current]` {: #inv-api-06 }

`PreparedProject` is a frozen dataclass exposing:
- `name: str`: Project name.
- `agent: str`: Agent identifier.
- `cwd: Path`: Resolved working directory for the agent.
- `env_overrides: Mapping[str, str]`: Read-only mapping of environment variable overrides.

### INV-API-07: Exception Hierarchy `[current]` {: #inv-api-07 }

All public exceptions inherit from `ProjectError -> RuntimeError`.
- `AmbiguousProjectPathError`: Exposes `project_name: str`, `paths: tuple[Path, ...]`.
- `ProjectPrepareConflictError`: Exposes `project_name: str`, `conflicts: tuple[str, ...]`.

---

