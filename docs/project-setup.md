# Set Up a Project

Register a project when it needs its own instructions, selected skills, or
project-specific memory.

## Prerequisites

- The Aikito workspace has been initialized.
- The target project already exists.
- Every selected skill exists under the workspace `skills/` directory.

## Register the Project

From the existing code project directory:

```bash
cd ~/code/example
aikito init project
```

This uses the directory name as the project name. To specify both explicitly:

```bash
aikito init project example ~/code/example \
  --description "Example service workspace"
```

The command creates and synchronizes:

```text
projects/example/
├── agent.toml
├── AGENTS.md
└── memory/
    ├── index.md
    └── notes/
```

It is safe to run again and preserves existing canonical files. A project name
already registered to another path, or unmanaged resources under the target
`.agents/` or an agent-native instruction path, is reported as a conflict instead
of being overwritten.

## Configure Project Resources

Edit the generated `<workspace>/projects/example/agent.toml` to select shared
resources and configure target paths:

```toml
name = "example"
description = "Example service workspace"
sync_mode = "link"
skills = ["durable-memory"]

# Candidate paths can be configured in one of three ways:
# 1. Single path:
path = "~/code/example"

# 2. Path list (e.g. multiple local checkouts or git worktrees):
# paths = ["~/code/example", "~/code/example-worktree"]

# 3. Named path table (ideal for cross-platform roaming across Mac/Win/Linux):
# [paths]
# mac = "~/code/example"
# win = "D:/code/example"
# worktree = "~/code/example-feature"
```

The project name identifies its workspace configuration. The optional
`description` is human-readable display metadata and does not affect project
resolution or synchronization.

### Path Resolution, Primary Path, and Offline Semantics

Aikito evaluates candidate paths dynamically on the local machine:
- **Active paths**: Candidate directories that currently exist on the local filesystem.
- **Offline paths & Offline projects**: Candidate directories that do not exist locally (for example,
  Windows paths when running on macOS, or a secondary worktree not yet cloned).
  When some candidate paths exist locally, the project syncs across those active paths.
  When NO candidate paths exist on the current host, the project is considered **offline** on this host.
  `aikito status` displays offline status with a dimmed badge (`–`), and `aikito doctor` reports the project as healthy
  (`Project '<name>': offline on this host (<candidates>)`), preserving candidate paths for seamless roaming across multiple machines through Git without false-positive failures.
- **Primary path**: For single-target display and default resolution, Aikito selects
  the **first active path** in configuration order. If no candidates exist locally, it
  falls back to the first defined candidate.

Native project instruction paths come from the workspace root `agents.toml`; project
configs do not duplicate that list. `sync_mode` controls only the selected project skills:

An empty canonical project `AGENTS.md` disables instruction synchronization.
Aikito removes only its own obsolete links and leaves a repository-owned
`AGENTS.md` untouched; `show project` reports that file as an informational notice.

- Keep the default `link` mode when this Aikito workspace is the shared source
  of truth and projects should receive skill updates immediately.
- Choose `copy` when the project repository should contain and track a managed
  snapshot of its selected skills, such as for collaborators or CI environments
  that do not share the workspace.

Project instructions and memory always remain linked, regardless of
`sync_mode`. Copied skills may diverge from their canonical workspace versions;
review and reconcile collaborator changes before running a synchronization that
would replace them. See [Architecture](architecture.md#project-skill-sync-modes)
for the full design trade-off.

### Multi-Active Synchronization Behavior

- `aikito sync project <name>`: Automatically synchronizes instructions, skills,
  and memory across **all active paths** detected on the machine. This allows multiple
  Git worktrees for the same repository to share agent instructions and memory
  effortlessly without separate project registrations.
- `aikito sync project <name> <project_path>`: Synchronizes a specific target path.
  If the target path is not yet present in `agent.toml`, Aikito automatically appends
  it as a new candidate.
- **Fail-Fast Preflight Validation**: Synchronization performs all validation,
  conflict checks, and drift detection across all active paths *before* writing any
  links or copying any files to disk. If any path fails preflight checks, the entire
  operation aborts without leaving any path in a half-synchronized state.

## Synchronize and Verify

```bash
aikito sync project example --dry-run
aikito sync project example
aikito show project example
aikito status
aikito show memory
```

The dry run reports planned links and copies without changing the runtime or
workspace configuration. Synchronization stops before writing when it finds
unmanaged conflicts or drifted copied skills. Use `aikito diff` to review copy
mode drift, reconcile changes that should survive, and use `--force` only when
the reviewed runtime changes may be discarded. Deselected workspace links and
unchanged canonical copies are recognized as managed and cleaned automatically;
other stale content remains a conflict. Do not store unrelated files under
`.agents/skills/` or `.agents/memory/`; Aikito owns those directories.

See [Architecture](architecture.md) for the canonical-source model and
[Memory workflow](memory-workflow.md) for choosing global versus project scope.
