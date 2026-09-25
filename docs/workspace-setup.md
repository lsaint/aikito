# 2. Adopt Your Existing Agent Setup

Your Agent setup already contains useful decisions. This step creates the Aikito
workspace, discovers the instructions, MCP servers, and subagents you already
use, and imports them into one canonical source. It assumes
[the CLI is installed](installation.md).

## Ask your agent

> Initialize my Aikito workspace at ~/aikito, or inspect it if it already exists.
> Inspect the Agent configuration I already use and run `aikito adopt` to bring
> supported resources into the workspace. Adoption preflights everything before
> writing and leaves the original Agent configuration unchanged. If it stops,
> explain each finding and ask before skipping a resource or resolving a
> conflict. Do not synchronize or register a project yet.

## Create and connect manually

```bash
aikito init workspace ~/aikito
aikito path workspace
```

The explicit path becomes the remembered workspace location. Initialization
creates the canonical workspace and detects installed supported agents.
If you already have a workspace, use its path instead.
For a cloned workspace on another machine, follow
[Connect a workspace on another machine](workspace-portability.md).

Adopt the configuration those Agents already use:

```bash
aikito adopt  # use --dry-run to preview, --verbose for full paths
```

Adoption validates every detected resource before creating a backup or writing.
It imports supported instructions, MCP servers, and subagents into the workspace
but leaves the original Agent configuration unchanged. If sources disagree or a
resource is invalid, the entire operation stops and reports the source, reason,
and available action. Repair resources you want to keep; use the displayed
resource-level `--skip` only when you intentionally do not want to adopt one.
Agent-native default servers configured in `agents/<name>.toml` under `builtin_mcps`
(such as `openaiDeveloperDocs` for Codex) are automatically excluded from adoption.

Existing skill directories are not imported automatically. After this onboarding
flow, review them separately, use `aikito add skill` to create a canonical
destination, and move in the content you want Aikito to govern.

| Workspace path | What it holds |
| --- | --- |
| `global/AGENTS.md` | Instructions shared across projects |
| `agents/<name>.toml` | Agent integration paths and capabilities |
| `skills/` and `skills.toml` | Reusable skills and global selections |
| `subagents/<name>.md` | Subagent personas configured across agents |
| `mcps/` | Model Context Protocol configurations |
| `projects/` | Each registered project's configuration and memory |

## Upgrade an existing workspace

Workspaces created before the per-resource layout require an explicit migration.
Installing the new Aikito version does not edit the workspace. Normal commands
show the migration command until the layout is upgraded:

For a workspace shared through Git, upgrade Aikito to version 1.53.0 or newer
on every machine **before** migrating it. After every machine is upgraded,
preview and migrate on one machine, commit and push the workspace changes,
then pull them on the others. An older CLI such as 1.52.1 cannot use the
migrated workspace: its commands report `Agents config not found`, and `init`
cannot restore the old layout.

```bash
aikito migrate workspace-resources --dry-run
aikito migrate workspace-resources
```

Review any collisions or unsupported entries reported by the preview before
applying. The migration writes `agents/<name>.toml` and frontmatter in
`subagents/<name>.md`, removes the two legacy registry files, and records
`version = 2` in `layout.toml` after the resource changes succeed. Repeating
the command on a migrated workspace is safe.

Inspect what became canonical:

```bash
aikito show instructions global
aikito show mcp
aikito show subagents
```

If nothing is detected, adoption completes as a no-op. Continue with an empty
workspace and create resources later. See the
[adoption safety model](safety.md#adoption) for source, backup, credential, and
write boundaries.

## Verify

`aikito path workspace` should print your selected location. The `show` commands
should display the adopted canonical resources without changing the original
Agent-native files.

Optionally run `aikito web` to browse resources and status in the read-only local
Web Console.

Next: [Synchronize and verify the existing setup](sync-existing-setup.md).
