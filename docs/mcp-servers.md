# Synchronize MCP Servers

Aikito stores canonical MCP definitions in `mcps/*.toml` and updates only the
managed entries in each supported Agent's native configuration.

## Before Synchronizing

- Review the server command, arguments, environment references, and target
  Agents in `mcps/<server>.toml`.
- Keep tokens and credentials in environment variables rather than plaintext
  canonical configuration.
- Commit or back up configuration you may need to recover independently.

## Preview

```bash
aikito sync mcp --dry-run
aikito show mcp
```

The preview shows planned writes and conflicts without applying them.

## Apply and Verify

```bash
aikito sync mcp
aikito show mcp
aikito show mcp --live
aikito show mcp <server>
aikito show mcp <server> --agent
aikito show mcp <server> --agent <agent>
aikito show mcp --agent <agent>
aikito edit mcp <server>
```

The normal show command compares canonical definitions with managed Agent
configuration. `aikito show mcp <server>` displays the canonical configuration file
content (`mcps/<server>.toml`), fully aligned with `show skill` and `show subagents`.
Live status performs additional runtime checks where supported.

Example `aikito show mcp` output from a configured workspace:

```text
┌─────────────┬───────┬─────────────┬───────────────────────┬──────────┐
│ MCP Server  │ Codex │ Claude Code │ Antigravity CLI (agy) │ OpenCode │
├─────────────┼───────┼─────────────┼───────────────────────┼──────────┤
│ local-tools │ ✓     │ ✓           │ ✓                     │ ✓        │
│ knowledge   │ ✓     │ ✓           │ –                     │ ✓        │
└─────────────┴───────┴─────────────┴───────────────────────┴──────────┘
```

`✓` means the managed entry is synchronized; `–` means that integration is not
selected for the server.

## Detail Views

`show mcp` supports detailed inspection across Agent targets. `show mcp <server> --agent` shows
where one canonical MCP definition is installed and its per-agent status. `--agent <agent>` lists the managed
and unmanaged MCP entries present in one Agent's native configuration. Combining
them (`show mcp <server> --agent <agent>`) shows one server/Agent intersection, including its configuration path,
format, status, and managed entry.

Detail views never print an Agent's entire configuration file. Header values,
secret tokens, and password fields in managed entries are redacted, while environment
variable references remain visible for diagnostics. Unmanaged entries are listed
by name and status without printing their content.

## Authentication

Authenticate a configured server for a specific Agent with:

```bash
aikito auth mcp <agent> <server>
```

Aikito converts detected plaintext secrets to environment-variable references
when adopting existing configuration. It does not make arbitrary secrets safe
to commit. Review [Safety model](safety.md) before publishing the workspace.

Unrelated Agent configuration is preserved. Aikito reports unmanaged
collisions instead of silently overwriting them.
