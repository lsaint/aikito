# CLI Reference

Aikito uses an operation-first command structure. Run
`aikito <command> --help` for the complete options supported by the installed
version.

| Command | Purpose |
| --- | --- |
| `aikito init workspace [path]` | Create a workspace skeleton and initialize Git |
| `aikito init project [name] [path]` | Register a code project and synchronize its `.agents/` runtime |
| `aikito adopt [path]` | Preview existing local configuration adoption |
| `aikito adopt --apply` | Apply the reviewed adoption plan |
| `aikito status` | Show the synchronization dashboard |
| `aikito status mcp` | Inspect managed MCP state |
| `aikito status subagents` | Inspect subagent rendering state |
| `aikito status memory` | List memory scopes and notes |
| `aikito status skills` | Drill down into registered and project skills |
| `aikito sync global` | Synchronize global instructions and skills |
| `aikito sync project <name>` | Synchronize a project's `.agents/` directory |
| `aikito sync mcp` | Synchronize MCP entries |
| `aikito sync subagents` | Render and synchronize subagents |
| `aikito auth mcp <agent> <server>` | Authenticate a configured MCP server |
| `aikito show memory <target>` | Print a memory note |
| `aikito show skill <target>` | Print a skill's SKILL.md file |
| `aikito edit memory <target>` | Open a memory note in `$VISUAL` or `$EDITOR` |
| `aikito edit skill <target>` | Open a skill's SKILL.md in `$VISUAL` or `$EDITOR` |
| `aikito doctor` | Run deep workspace diagnostics |
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
- `init workspace` creates or updates a recognized workspace;
- `init project` creates an idempotent canonical project skeleton and its runtime links;
- `adopt --apply` writes imported resources into the workspace after backup;
- `sync` writes managed Agent or project runtime configuration;
- `edit` delegates a canonical memory or skill file to an external editor.

Use resource-specific `--dry-run` options where available and consult the
[Safety model](safety.md) before applying changes to an existing setup.

## Initialization

Initialize the one workspace that stores all global and project-scoped Aikito
resources:

```bash
aikito init workspace ~/aikito
```

Register a code project from its directory. The directory name becomes the
project name by default:

```bash
cd ~/code/example
aikito init project
```

Both values can be explicit:

```bash
aikito init project example ~/code/example
```

Project initialization creates `agent.toml`, `AGENTS.md`, and the project
memory skeleton under `~/aikito/projects/<name>/`, then synchronizes the target
project's `.agents/` runtime. It is idempotent. A project name already bound to
another path, or unmanaged resources at a target runtime path, is reported as a
conflict rather than overwritten.
