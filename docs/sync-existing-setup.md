# 3. Synchronize and Verify

Adoption made your supported existing instructions, MCP servers, and subagents canonical inside the Aikito workspace. Skills are imported explicitly.
Synchronization now connects those sources back to every supported Agent in the
format and location it expects.

## Ask your agent

> Synchronize the complete Aikito workspace and verify the result with
> `aikito status`. Synchronization preflights every active scope before writing.
> If it stops, explain the affected source and target, and ask before forcing,
> pruning, or manually resolving anything. Confirm which Agents now consume the
> adopted instructions, MCP servers, and subagents, plus any explicitly imported skills.

## Synchronize manually

```bash
aikito sync
aikito status
```

`aikito sync` first builds the complete workspace plan. It writes only when all
active scopes are safe, skips resources for Agents that are offline on this
machine, and does not silently replace unmanaged files with differing content. Use `--dry-run` only
when you specifically want to stop after the read-only plan; add `--verbose`
when exact items and paths are useful.

When synchronizing after adoption, if an existing target file (such as a global
instruction file or subagent definition) has equivalent content to the canonical source,
Aikito automatically takes over ownership (replacing the regular file with a canonical symlink
or injecting the subagent managed marker) after creating a timestamped backup under
`~/.aikito/backups/sync_<timestamp>/`. If the content has diverged, synchronization preserves
the unmanaged target and reports a conflict.

If synchronization reports an unmanaged target conflict, inspect both versions and
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

Next, [connect a code project](project-setup.md) to bring your first
repository into the workspace.
