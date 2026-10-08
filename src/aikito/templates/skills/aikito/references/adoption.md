# Adoption

`aikito adopt` discovers supported Agent-native resources and imports them into
the workspace only after the complete plan passes preflight. It writes to the
workspace, not back to Agent-native configuration; runtime targets change later
during explicit synchronization.

Use `aikito adopt --dry-run --verbose` for a detailed read-only plan. Successful
application creates timestamped backups under
`~/.aikito/backups/adopt_<timestamp>`. Native MCP configurations whose adapter
materializes credentials are excluded from whole-file backups; their contents
are still fingerprinted to reject stale adoption plans.

Sources follow workspace Agent definitions, with bundled definitions used to
discover unregistered built-in Agents. MCP and subagent sources are imported
only when their adapter supports adoption. Instruction sources sharing one
physical file are read once. Claude Desktop MCP files and the legacy
`~/.gemini/config/AGENTS.md` instruction file remain supported.

When a new MCP or subagent resource targets an unregistered built-in Agent,
adoption registers its bundled definition before writing the resource. Existing
resources and instruction-only imports do not trigger registration. Registered
Agent definitions are preserved.

All supported MCP import adapters convert recognized sensitive headers to
environment-variable references. Existing references and non-sensitive headers
are preserved, and the original source is unchanged. Before synchronizing,
inspect the imported `mcps/<name>.toml` and configure any generated
`AIKITO_<SERVER>_<HEADER>` variables in the runtime environment.

Adoption is blocked by:

- conflicting instructions;
- unreadable or malformed source configuration;
- invalid generated MCP or subagent resources;
- skipping an Agent registration while keeping a new resource that requires it.

`aikito doctor` reports the same structured findings but never adopts or skips
resources, including with `--fix`. Follow its repair or review actions before
retrying adoption.

When a valid resource should intentionally remain external, repeat one-shot
skip options such as `--skip instructions`, `--skip mcp/<name>`, or
`--skip subagent/<name>`. Use `--skip agent/<name>` to omit a planned registration;
also skip the new resources that require it. Skips are shown in the result and
apply to one invocation only. Never use them to bypass unreadable or malformed
source data.
Agent-builtin MCP servers configured under `builtin_mcps` in `agents/*.toml`
are automatically omitted from adoption.

After adoption succeeds, inspect the imported canonical resources and run
`aikito sync`. Synchronization also preflights the complete plan before writing;
use `aikito sync --dry-run --verbose` when a detailed read-only plan is needed.
