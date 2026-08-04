# Set Up a Project

Register a project when it needs its own instructions, selected skills, or
project-specific memory.

## Prerequisites

- The Aikito workspace has been initialized.
- The target project already exists.
- Every selected skill exists under the workspace `skills/` directory.

## Configure the Project

Create `~/aikito/projects/example/agent.toml`:

```toml
name = "example"
path = "~/code/example"
sync_mode = "link"
skills = ["durable-memory"]
memory = []
```

Add optional instructions and memory:

```text
projects/example/
├── agent.toml
├── AGENTS.md
└── memory/
    ├── index.md
    └── notes/
```

The project name identifies its workspace configuration. The `path` points to
the project receiving the managed `.agents/` runtime directory.

## Synchronize and Verify

```bash
aikito sync project example
aikito status
aikito status memory
```

Inspect the target project's `.agents/` directory and resolve any reported
unmanaged conflicts manually. Do not store unrelated files under
`.agents/skills/` or `.agents/memory/`; Aikito owns those directories.

See [Architecture](architecture.md) for the canonical-source model and
[Memory workflow](memory-workflow.md) for choosing global versus project scope.
