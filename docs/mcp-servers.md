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
aikito show mcp <server> --live
aikito show mcp <server> --agent
aikito show mcp <server> --agent <agent>
aikito show mcp <server> --agent <agent> --live
aikito show mcp --agent <agent>
aikito edit mcp <server>
```

The normal show command compares canonical definitions with managed Agent
configuration. `aikito show mcp <server>` displays the canonical configuration file
content (`mcps/<server>.toml`), fully aligned with `show skill` and `show subagents`.
Live status performs additional runtime checks where supported.

Targeted live inspection connects to the remote MCP endpoint through each
Agent-native configuration, completes the MCP initialization lifecycle, and
runs only `tools/list`. It compares connection status, configured authentication
method, and visible tool count across Agents:

```text
┌─────────────────┬─────────┬───────────────────────┬───────┐
│ Agent           │ Connect │ Auth method           │ Tools │
├─────────────────┼─────────┼───────────────────────┼───────┤
│ Codex           │ ✓       │ Basic · env header    │ 3     │
│ Antigravity CLI │ ✓       │ Basic · inline header │ 27    │
└─────────────────┴─────────┴───────────────────────┴───────┘
```

Adding `--agent <agent>` narrows the probe to one Agent and prints its tool
names below the table. The live probe never calls an MCP tool. OAuth credentials
owned by an Agent runtime cannot be reused by Aikito and are reported as skipped.
Credential values remain redacted, redirects are rejected, responses are bounded,
and connection failures are summarized below the table. Aikito refuses to send
configured credentials over plaintext HTTP unless the endpoint is loopback.

Example `aikito show mcp` output from a configured workspace:

```text
┌─────────────┬───────┬─────────────┬─────────────────┬──────────┬────────────────────┬──────────────────┐
│ MCP Server  │ Codex │ Claude Code │ Antigravity CLI │ OpenCode │ GitHub Copilot CLI │ DeepSeek Harness │
├─────────────┼───────┼─────────────┼─────────────────┼──────────┼────────────────────┼──────────────────┤
│ local-tools │ ✓     │ ✓           │ ✓               │ ✓        │ ✓                  │ ✓                │
│ knowledge   │ ✓     │ ✓           │ –               │ ✓        │ ✓                  │ ✓                │
└─────────────┴───────┴─────────────┴─────────────────┴──────────┴────────────────────┴──────────────────┘
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
