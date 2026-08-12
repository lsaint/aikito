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
| `aikito diff` | Show unified diffs for all drifted MCP and subagent resources |
| `aikito sync global` | Synchronize global instructions and skills |
| `aikito sync project <name>` | Synchronize a project's `.agents/` directory |
| `aikito sync mcp` | Synchronize MCP entries |
| `aikito sync subagents` | Render and synchronize subagents |
| `aikito auth mcp <agent> <server>` | Authenticate a configured MCP server |
| `aikito show mcp [server] [--agent agent]` | Inspect the MCP matrix or drill into a server, Agent, or managed entry |
| `aikito show subagents` | Inspect subagent rendering state across agents |
| `aikito show instructions [global|project|.]` | List or print global and project instructions |
| `aikito show memory [target]` | Print a memory note, or list all memory notes if target is omitted |
| `aikito show skill [target]` | Print a skill's SKILL.md file, or list all skills if target is omitted |
| `aikito edit memory <target>` | Open a memory note in `$VISUAL` or `$EDITOR` |
| `aikito edit instructions <global|project|.>` | Open canonical instructions in `$VISUAL` or `$EDITOR` |
| `aikito edit skill <target>` | Open a skill's SKILL.md in `$VISUAL` or `$EDITOR` |
| `aikito doctor` | Run deep workspace diagnostics |
| `aikito completion zsh\|bash\|fish` | Print a shell completion script |
| `aikito completion candidates projects\|skills\|memories` | List dynamic completion candidates |
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

- `status`, `diff`, `show`, `completion`, and the default `adopt` plan are read-only;
- `init workspace` creates or updates a recognized workspace;
- `init project` creates an idempotent canonical project skeleton and its runtime links;
- `adopt --apply` writes imported resources into the workspace after backup;
- `sync` writes managed Agent or project runtime configuration;
- `edit` delegates a canonical memory or skill file to an external editor.

Use resource-specific `--dry-run` options where available and consult the
[Safety model](safety.md) before applying changes to an existing setup.

## Drift Diff

After `aikito status` reports drift, inspect every drifted managed MCP entry and
subagent file at once:

```bash
aikito diff
```

The command compares actual Agent configuration against Aikito's expected
rendering and prints unified diffs. MCP credentials and sensitive headers are
redacted. Missing resources and unmanaged conflicts remain status findings and
are not rendered as drift diffs.

## Instructions

Aikito calls this resource “instructions” and stores its canonical content in
`AGENTS.md` for ecosystem compatibility.

Without a target, show how each Agent is connected to global instructions,
followed by a compact Projects section. A project with missing or empty
canonical instructions is shown as `-`; otherwise its runtime link is shown
as `linked`, `missing`, or `conflict`. Provide `global`, a project name, or `.`
to print raw Markdown content:

```bash
aikito show instructions
aikito show instructions global
aikito show instructions example
aikito show instructions .
aikito edit instructions example
aikito edit instructions .
```

`.` resolves the registered project containing the current directory and notes
that global instructions are also active.

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
project's `.agents/` runtime. The initial `AGENTS.md` is empty until
project-specific instructions are added. Initialization is idempotent. A
project name already bound to another path, or unmanaged resources at a target
runtime path, is reported as a conflict rather than overwritten.

## Shell Completion

Aikito ships its own completion scripts so you do not need third-party packages.

**Zsh** — add one line to `~/.zshrc`:

```zsh
eval "$(aikito completion zsh)"
```

**Bash** — add one line to `~/.bashrc` or `~/.bash_profile`:

```bash
eval "$(aikito completion bash)"
```

**Fish** — install the completion file once:

```fish
aikito completion fish > ~/.config/fish/completions/aikito.fish
```

Completion covers all commands, subcommands, and options statically.
When tab-completing a memory note, skill name, or project name, Aikito
calls a lightweight internal interface that reads only the local workspace
files with no network requests or expensive diagnostics:

```bash
aikito completion candidates projects
aikito completion candidates skills
aikito completion candidates memories
```

Installation via `brew install lsaint/tap/aikito` automatically installs
Zsh and Bash completions without modifying `~/.zshrc`.
