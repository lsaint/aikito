# CLI Reference

## `aikito web`

Start the read-only local Web Console on `127.0.0.1`:

```bash
aikito web
aikito web --port 8765
aikito web --no-open
```

The Console browses canonical resources and governance status. It does not
modify workspace files or expose MCP secret values.

Aikito uses an operation-first command structure. Run
`aikito <command> --help` for the complete options supported by the installed
version.

| Command | Purpose |
| --- | --- |
| `aikito init workspace [path]` | Create a workspace skeleton and initialize Git |
| `aikito init project [name] [path]` | Register a code project and synchronize its `.agents/` runtime |
| `aikito add skill <name>` | Create a canonical skill skeleton and register it in `skills.toml` or project config |
| `aikito add subagent <name>` | Create a canonical subagent skeleton and register it in `subagents.toml` |
| `aikito add mcp <name>` | Create a canonical MCP server configuration in `mcps/<name>.toml` |
| `aikito adopt [path]` | Preview existing local configuration adoption |
| `aikito adopt --apply` | Apply the reviewed adoption plan |
| `aikito status` | Show the synchronization dashboard |
| `aikito diff` | Show unified diffs for drifted MCP, subagent, and copied project skill resources |
| `aikito sync global [--dry-run]` | Synchronize or preview global instructions and skills |
| `aikito sync project <name> [--dry-run] [--force]` | Synchronize or preview a project's `.agents/` directory |
| `aikito sync mcp` | Synchronize MCP entries |
| `aikito sync subagents` | Render and synchronize subagents |
| `aikito auth mcp <agent> <server>` | Authenticate a configured MCP server |
| `aikito show mcp [server] [--agent agent]` | Inspect the MCP matrix or drill into a server, Agent, or managed entry |
| `aikito show subagents [target] [--agent agent]` | Inspect the subagent matrix, drill into platform options per agent, or print instructions |
| `aikito show project [name]` | List registered projects or inspect one project's configuration and sync status |
| `aikito show instructions [global|project|.]` | List or print global and project instructions |
| `aikito show inbox [target]` | Print raw markdown content of an inbox note, or list all inbox notes if target is omitted |
| `aikito show memory [target]` | Print a memory note, or list all memory notes if target is omitted |
| `aikito show skill [target]` | Print a skill's SKILL.md file, or list all skills if target is omitted |
| `aikito rename memory <target> <new-name>` | Atomically rename a memory note, update its index entry, and refactor inbound wikilinks |
| `aikito rm memory <target>` | Remove a memory note, prune its index entry, and scan for inbound wikilinks |
| `aikito edit memory <target>` | Open a memory note in `$VISUAL` or `$EDITOR` |
| `aikito maintain memory [global\|<project>\|.] [--agent <name>]` | Launch an Agent to review one complete memory scope and propose maintenance before making changes |
| `aikito edit instructions <global|project|.>` | Open canonical instructions in `$VISUAL` or `$EDITOR` |
| `aikito edit skill <target>` | Open a skill's SKILL.md in `$VISUAL` or `$EDITOR` |
| `aikito edit subagent <target>` | Open a subagent's instruction markdown in `$VISUAL` or `$EDITOR` |
| `aikito doctor [--fix]` | Run deep workspace diagnostics (and auto-repair fixable index issues) |
| `aikito completion zsh\|bash\|fish` | Print a shell completion script |
| `aikito completion candidates projects\|skills\|subagents\|mcps\|memories\|memory-completions\|inbox\|inbox-completions\|paths [prefix]` | List dynamic completion candidates |
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
- `add` creates a canonical resource skeleton and performs required registration;
- `adopt --apply` writes imported resources into the workspace after backup;
- `sync` writes managed Agent or project runtime configuration;
- `edit` delegates a canonical memory, skill, instruction, or subagent file to an external editor.
- `maintain memory` launches an interactive Agent whose prompt requires confirmation before writes.

Use `--dry-run` to preview project, MCP, and subagent synchronization, and consult the
[Safety model](safety.md) before applying changes to an existing setup.

`aikito maintain memory` defaults to the project containing the current
directory and launches the `codex` runner configured in `agents.toml`. Use
`global` or a registered project name to select another scope, and `--agent`
to choose `codex`, `claude-code`, `agy`, `opencode`, or `github-copilot`.
Custom Agents can define `[agents.<name>.runner]` with a `command` array.
Supported placeholders are `{prompt}`, `{workdir}`, `{scope}`, and
`{memory_dir}`. Optional `[agents.<name>.runner.env]` string values override
the inherited process environment and support the same placeholders:

```toml
[agents.codex.runner.env]
HTTPS_PROXY = "http://127.0.0.1:1234"
```

Runner environment values are local configuration. The open-source export
removes every `runner.env` value while preserving runner commands and a safe
commented example; never rely on repository publication as a secrets store.

## Drift Diff

After `aikito status` reports drift, inspect every drifted managed MCP entry and
subagent file at once:

```bash
aikito diff
```

The command compares actual Agent configuration and copied project skills
against Aikito's expected rendering and prints unified diffs. MCP credentials
and sensitive headers are redacted. Binary project skill files are reported
without printing their contents. Missing resources and unmanaged conflicts
remain status findings and are not rendered as drift diffs.

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

## Inbox

`inbox/` acts as a staging directory for raw notes captured from browser AI conversations before they are curated into durable memory.

List inbox notes sorted by modification time (`Name` and `Modified`):

```bash
aikito show inbox
```

Print the raw Markdown content of an inbox note by exact name or unique prefix:

```bash
aikito show inbox perplexity-ai-positioning
aikito show inbox perplexity
```

The inbox directory defaults to `~/aikito/inbox` and can be customized in `config.toml`:

```toml
[inbox]
path = "~/aikito/inbox"
```

## Projects

List each registered project's path, synchronization mode, instructions,
selected skill count, project memory note count, and aggregate sync status:

```bash
aikito show projects
```

Inspect one project's canonical and project directories, configuration, and sync
issues when present:

```bash
aikito show project example
```

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
aikito completion candidates subagents
aikito completion candidates mcps
aikito completion candidates memories
aikito completion candidates inbox-completions
aikito completion candidates paths agent
```

Path arguments combine normal local filesystem completion with basename-prefix
matches from the Aikito workspace and registered project roots. Hidden and
generated dependency directories are skipped, and ambiguous matches remain
visible for explicit selection.

Installation via `brew install lsaint/tap/aikito` automatically installs
Zsh and Bash completions without modifying `~/.zshrc`.
