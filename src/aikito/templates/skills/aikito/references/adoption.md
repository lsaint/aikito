# Adoption

`aikito adopt` discovers supported Agent-native resources and imports them into
the workspace only after the complete plan passes preflight. It writes to the
workspace, not back to Agent-native configuration; runtime targets change later
during explicit synchronization.

Use `aikito adopt --dry-run --verbose` for a detailed read-only plan. Successful
application creates timestamped backups under
`~/.aikito/backups/adopt_<timestamp>`.

Adoption is blocked by:

- conflicting instructions;
- unreadable or malformed source configuration;
- invalid generated MCP or subagent resources.

`aikito doctor` reports the same structured findings but never adopts or skips
resources, including with `--fix`. Follow its repair or review actions before
retrying adoption.

When a valid resource should intentionally remain external, repeat one-shot
skip options such as `--skip instructions`, `--skip mcp/<name>`, or
`--skip subagent/<name>`. Skips are shown in the result and apply to one
invocation only. Never use them to bypass unreadable or malformed source data.
Agent-builtin MCP servers configured under `builtin_mcps` in `agents/*.toml`
are automatically omitted from adoption.

After adoption succeeds, inspect the imported canonical resources and run
`aikito sync`. Synchronization also preflights the complete plan before writing;
use `aikito sync --dry-run --verbose` when a detailed read-only plan is needed.
