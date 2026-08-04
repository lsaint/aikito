# CLI Reference

Aikito uses an operation-first command structure. Run
`aikito <command> --help` for the complete options supported by the installed
version.

| Command | Purpose |
| --- | --- |
| `aikito init [path]` | Create a workspace skeleton and initialize Git |
| `aikito adopt [path]` | Preview existing local configuration adoption |
| `aikito adopt --apply` | Apply the reviewed adoption plan |
| `aikito status` | Show the synchronization dashboard |
| `aikito status mcp` | Inspect managed MCP state |
| `aikito status subagents` | Inspect subagent rendering state |
| `aikito status memory` | List memory scopes and notes |
| `aikito sync global` | Synchronize global instructions and skills |
| `aikito sync project <name>` | Synchronize a project's `.agents/` directory |
| `aikito sync mcp` | Synchronize MCP entries |
| `aikito sync subagents` | Render and synchronize subagents |
| `aikito auth mcp <agent> <server>` | Authenticate a configured MCP server |
| `aikito show memory <target>` | Print a memory note |
| `aikito edit memory <target>` | Open a memory note in `$VISUAL` or `$EDITOR` |
| `aikito version` | Print the CLI version |

## Discovery

```bash
aikito --help
aikito sync --help
aikito status --help
aikito --version
```

## Write Boundaries

Commands differ in their effect:

- `status`, `show`, and the default `adopt` plan are read-only;
- `init` creates or updates a recognized workspace;
- `adopt --apply` writes imported resources into the workspace after backup;
- `sync` writes managed Agent or project runtime configuration;
- `edit` delegates a canonical memory file to an external editor.

Use resource-specific `--dry-run` options where available and consult the
[Safety model](safety.md) before applying changes to an existing setup.
