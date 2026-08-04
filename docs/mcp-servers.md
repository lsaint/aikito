# Synchronize MCP Servers

Aikito stores canonical MCP definitions in `mcps.toml` and updates only the
managed entries in each supported Agent's native configuration.

## Before Synchronizing

- Review the server command, arguments, environment references, and target
  Agents in `mcps.toml`.
- Keep tokens and credentials in environment variables rather than plaintext
  canonical configuration.
- Commit or back up configuration you may need to recover independently.

## Preview

```bash
aikito sync mcp --dry-run
aikito status mcp
```

The preview shows planned writes and conflicts without applying them.

## Apply and Verify

```bash
aikito sync mcp
aikito status mcp
aikito status mcp --live
```

The normal status command compares canonical definitions with managed Agent
configuration. Live status performs additional runtime checks where supported.

Example `aikito status mcp` output from a configured workspace:

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
