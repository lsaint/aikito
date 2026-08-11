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
`.agents/`, is reported as a conflict instead of being overwritten.

## Configure Project Resources

Edit the generated `~/aikito/projects/example/agent.toml` to select shared
resources:

```toml
name = "example"
path = "~/code/example"
sync_mode = "link"
skills = ["durable-memory"]
memory = []
```

The project name identifies its workspace configuration. The `path` points to
the project receiving the managed `.agents/` runtime directory. `sync_mode`
controls only the selected project skills:

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
aikito sync project example
aikito status
aikito show memory
```

Inspect the target project's `.agents/` directory and resolve any reported
unmanaged conflicts manually. Do not store unrelated files under
`.agents/skills/` or `.agents/memory/`; Aikito owns those directories.

See [Architecture](architecture.md) for the canonical-source model and
[Memory workflow](memory-workflow.md) for choosing global versus project scope.
