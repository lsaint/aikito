# Manage MCP Servers

Aikito stores canonical MCP definitions in `mcps/*.toml` and updates only the
managed entries in each supported Agent's native configuration. Pi is omitted
from MCP synchronization; see [Architecture](architecture.md) for per-agent
capability boundaries.

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

## Add or Import MCP Servers

Create a new canonical MCP server configuration with a remote URL:

```bash
aikito add mcp github-mcp --url https://api.githubcopilot.com/mcp
```

Import from an external configuration file or remote endpoint:

```bash
# Import from a remote URL directly (infers name from URL path)
aikito add mcp --from https://example.com/v1/mcp --sync

# Import from a single JSON or TOML server definition (must contain a remote URL)
aikito add mcp weather --from ./weather.json --sync

# Atomically replace an existing configuration
aikito add mcp weather --from ./weather-v2.json --force --sync
```

> **Note:** `--from` only supports remote MCP servers (entries that carry an HTTP/HTTPS `url`).
> Stdio-only entries (`command` / `args` / `env` without a `url`) are not importable and will produce an error.
> To import a server from a multi-server file such as `claude_desktop_config.json`, the target entry must expose a remote URL; pass `--name <server>` to select it.

- `--from <source>`: Path to a local `.json` / `.toml` configuration file or remote HTTP/HTTPS URL. Server name is inferred from the filename or key when omitted.
- `--sync`: Immediately synchronizes the added MCP server into configured Agent runtimes. Aikito executes a preflight dry-run check first: if any Agent encounters a configuration conflict, no Agent runtime file is touched and the canonical file is safely rolled back. If any runtime write fails mid-sync, all already-written Agent configs are atomically restored.
- `--force`: Atomically replaces an existing canonical definition in `mcps/<name>.toml` while preserving existing `overrides`, `authentication`, and custom `agents` tables (unless explicitly specified). This only applies to the canonical file and does not bypass downstream Agent conflict protections during `--sync`.
- **Credential Protection**: Plaintext secrets in headers (such as `Authorization: Bearer <token>`, `X-Password`, or `Cookie`) are automatically sanitized into secure environment variable references (`${AIKITO_<SERVER>_<KEY>}`) to prevent credential leakage into Git. The CLI outputs only the variable *name* — never the secret value — along with instructions to set it at runtime. URL-embedded credentials (userinfo and sensitive query parameters such as `?token=`) are also stripped. Valid environment references (`${VAR}`, `{env:VAR}`, `!!js process.env.VAR`) are preserved intact.

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

## Built-in Agent Servers

Certain Agent runtimes bundle or recommend proprietary MCP servers (such as `openaiDeveloperDocs` in Codex). To prevent `aikito adopt` from adopting these Agent-native defaults into workspace-managed configurations, list them under the Agent's MCP table in `agents.toml`:

```toml
[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
builtin_mcps = ["openaiDeveloperDocs"]
```

When `aikito adopt` scans local Agent configurations, any server listed in `builtin_mcps` that is not shared by other Agents is automatically skipped.
Hyphen-to-underscore name matching is applied only to Agents whose registry entry
uses `name_style = "underscore"`. Matching names with the same URL are treated
as the same MCP server; Agent-specific headers, environment variables, and other
runtime fields do not cause adoption conflicts. Different URLs still block
adoption instead of silently choosing one.

## Removing MCP Servers

To remove a canonical MCP server definition from the workspace, run:

```bash
aikito rm mcp <name>
```

Add `--sync` to immediately unregister and remove the server configuration from all configured Agent runtimes:

```bash
aikito rm mcp <name> --sync
```
