# 3. Synchronize and Verify

Adoption made your existing resources canonical inside the Aikito workspace.
Synchronization now connects those sources back to every supported Agent in the
format and location it expects.

## Ask your agent

> Synchronize the complete Aikito workspace and verify the result with
> `aikito status`. Synchronization preflights every active scope before writing.
> If it stops, explain the affected source and target, and ask before forcing,
> pruning, or manually resolving anything. Confirm which Agents now consume the
> adopted instructions, skills, MCP servers, and subagents.

## Synchronize manually

```bash
aikito sync
aikito status
```

`aikito sync` first builds the complete workspace plan. It writes only when all
active scopes are safe, skips resources for Agents that are offline on this
machine, and does not silently replace unmanaged files. Use `--dry-run` only
when you specifically want to stop after the read-only plan; add `--verbose`
when exact items and paths are useful.

If synchronization reports an unmanaged target, inspect both versions and
follow the [conflict guide](troubleshooting.md#existing-files-conflict). Use
`--force` or `--prune` only after choosing what should be discarded.

## Verify the result

In `aikito status`, confirm that your installed Agents show the expected
instructions, skills, MCP configurations, and subagents. Then inspect any
resource that matters to you:

```bash
aikito show instructions global
aikito show mcp
aikito show subagents
```

The setup you already relied on is now governed from one Git-managed source.
Future edits happen in the workspace and flow outward through synchronization.

Next, [connect a code project](project-setup.md),
[keep a durable decision](durable-memory.md), or review
[workspace ownership and synchronization](architecture.md).
