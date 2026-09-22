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
default. To overwrite a specific unmanaged target after review, specify its target key:

```bash
aikito sync subagents --force <agent>/<subagent>
```

Passing `--force` with a specific `<agent>/<subagent>` authorizes overwriting only that target, preventing accidental overwrite of other conflicting files.

Shared agent configuration files (such as DeepSeek Harness `cordis.patch.yml`) merge multiple subagent definitions in memory and perform a single write, preserving other unmanaged sections and custom tool definitions.

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

## Add or Import Subagents

Create a new canonical subagent skeleton:

```bash
aikito add subagent reviewer --description "Performs automated code reviews"
```

Import from an existing markdown prompt or Copilot agent file:

```bash
aikito add subagent --from ./prompts/reviewer.md --sync
aikito add subagent --from .github/agents/reviewer.agent.md --force
```

- `--from <path>`: Points to a local markdown prompt file (e.g. `.md`, `.agent.md`) or directory containing instructions. Name and description are inferred from frontmatter or file stem when omitted.
- `--sync`: Immediately renders and synchronizes the subagent into configured agent runtimes.
- `--force`: Atomically replaces an existing subagent definition in `subagents/<name>.md` and updates `subagents.toml` with the imported snapshot.

To unregister and remove a subagent from the workspace, run `aikito rm subagent <name>`.
Add `--sync` to immediately prune the rendered subagent definition from all
configured Agent runtimes:

```bash
aikito rm subagent reviewer --sync
```

If a definition was removed without `--sync`, `aikito status` may report a managed orphan.
Run `aikito sync subagents --prune` to clean up orphaned definitions across agents. Pruning strictly deletes only definitions containing an Aikito ownership marker; pre-existing unmanaged definitions are never deleted.

See [Architecture](architecture.md) for Agent capability boundaries and
[Safety model](safety.md) before forcing any target.
