# Manage Subagents

Aikito keeps canonical subagent definitions in `subagents.toml` and the
`subagents/` directory, then renders them into formats supported by each Agent.

## Preview

Review the canonical definition and preview the rendering plan:

```bash
aikito sync subagents --dry-run
aikito show subagents
```

The plan identifies creates, updates, unsupported capabilities, or unmanaged
target conflicts.

## Apply and Verify

```bash
aikito sync subagents
aikito show subagents
aikito show subagent verifier
aikito edit subagent verifier
```

Managed files are updated from the canonical definition. An existing file
without an Aikito marker is treated as unmanaged and will not be overwritten by
default.

Example `aikito show subagents` output from a configured workspace:

```text
┌───────────┬───────┬─────────────┬─────────────────┬──────────┬────────────────────┬──────────────────┐
│ Subagent  │ Codex │ Claude Code │ Antigravity CLI │ OpenCode │ GitHub Copilot CLI │ DeepSeek Harness │
├───────────┼───────┼─────────────┼─────────────────┼──────────┼────────────────────┼──────────────────┤
│ verifier  │ ✓     │ ✓           │ ✓               │ ✓        │ ✓                  │ ✓                │
└───────────┴───────┴─────────────┴─────────────────┴──────────┴────────────────────┴──────────────────┘
```

`✓` means the rendered definition is synchronized; `–` means that Agent does
not participate in subagent synchronization.

- **Antigravity CLI**: Definitions are rendered to
  `~/.gemini/config/agents/<name>/agent.md`, supporting `model` and `tools`.
- **OpenCode**: Definitions are rendered to
  `~/.config/opencode/agents/<name>.md` with `mode: subagent`; set the native
  model ID through `[subagents.<name>.opencode].model`.
- **Pi**: Participates only when its optional extension entry point exists at
  `~/.pi/agent/extensions/subagent/index.ts`. Definitions are rendered to
  `~/.pi/agent/agents/<name>.md`. Pi-specific configuration supports `model` and
  `tools`; without the extension, Pi remains skipped and Aikito writes nothing.

If a definition is removed, status may report a managed orphan. Review it
before using the command's explicit pruning or force options. Use
`aikito sync subagents --help` for the options supported by the installed
version.

See [Architecture](architecture.md) for Agent capability boundaries and
[Safety model](safety.md) before forcing any target.
