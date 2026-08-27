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
aikito init project example ~/code/example
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
resources:

```toml
name = "example"
path = "~/code/example"
sync_mode = "link"
skills = ["durable-memory"]
```

The project name identifies its workspace configuration. The `path` points to
the project receiving the managed resources. Native project instruction paths
come from the workspace root `agents.toml`; project configs do not duplicate
that list. `sync_mode` controls only the selected project skills:

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
