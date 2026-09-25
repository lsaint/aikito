---
name: aikito
description: Operate Aikito workspaces and resources, including reading inbox notes, managing memory, skills, projects, MCP servers, subagents, and synchronization.
---

# Aikito

## Purpose

Use Aikito as the canonical, Git-managed source for Agent resources shared
across tools, projects, and machines. Existing Agent setups normally enter the
workspace through adoption; newly created resources start in the workspace.

## Core Model

Keep the CLI installation separate from the canonical user workspace
(`<workspace>`, normally `~/aikito`). When developing Aikito itself, keep its
source checkout separate from the workspace too:

```text
<workspace>/
├── layout.toml + skills.toml
├── agents/*.toml
├── subagents/*.md
├── global/AGENTS.md
├── skills/ + subagents/ + mcps/
├── memory/
└── projects/<name>/
```

Agent configuration directories and project `.agents/` directories are runtime
targets, not independent sources. Modify canonical resources first and then
synchronize them. Never treat generated Agent-native instructions, skills,
subagents, or MCP entries as sources.

The bundled `aikito` and `durable-memory` skills are system-managed snapshots,
not user-customizable canonical resources. Aikito refreshes them from the
installed package during workspace initialization and global synchronization,
backing up divergent contents first. Put lasting custom behavior in global or
project instructions, or in a separately named skill.

Resolve `<workspace>` with `aikito path workspace` before editing canonical
files directly. `AIKITO_DIR` temporarily overrides the persisted workspace.
Use the installed `aikito` command and treat `aikito <command> --help` as the
authority for the installed version's options.

## Choose a Workflow

Read [references/installation.md](references/installation.md) when the CLI is
not installed or the user asks to install or upgrade Aikito.

Bring an existing local Agent setup under management:

```bash
aikito init workspace ~/aikito
aikito adopt
aikito sync
aikito status
```

Skip `adopt` when there are no supported external resources. On another
machine, clone or download the existing workspace, bind it with
`aikito init workspace <path>`, and run `aikito sync`.

New resources follow `add → edit → show → sync → status / diff / doctor`.
Existing resources follow `adopt → show → sync → status`. Bare `aikito sync`
orchestrates the entire workspace.

## Core Rules

- Inspect relevant canonical files and current status before changing anything;
  run `aikito status` or the narrowest resource-specific check afterward.
- Use `--dry-run` when the user requests a preview. Adoption and synchronization
  already preflight their complete plans before writing by default.
- Never silently replace unmanaged targets, drifted copies, or conflicting
  instructions. Do not use `--force` or `--prune` merely to make status green.
- Preserve unrelated changes in both the workspace and runtime targets.
- Keep credentials out of the workspace and Git. Canonical MCP definitions
  should reference environment variables; never print or persist their values.
- Before publishing a workspace repository, review memory and configuration for
  private data. A local Git repository is not automatically safe to publish.

## Resource Routing

Use `aikito <command> --help` for exact arguments.

- Global instructions and skills: `aikito add skill` (supports `--from <path>` to
  import, `--from <path> --force` to refresh an imported snapshot, and
  `--project <A,B>` / `--sync` to distribute), `aikito rm skill` (supports
  unregistering via `--project <A,B>` and global deletion via `--force`),
  `aikito show skill`, and `aikito sync global`.
- Projects: `aikito init project`, `aikito show project`,
  `aikito sync project`, `aikito status`, and `aikito diff`.
- MCP servers: `aikito add mcp` (supports `--from <source>`, `--sync`, and `--force`),
  `aikito rm mcp` (supports `--sync`), `aikito show mcp`, `aikito sync mcp`, and
  `aikito auth mcp`.
- Subagents: `aikito add subagent`, `aikito rm subagent` (supports `--sync`),
  `aikito show subagent`, and `aikito sync subagents`.
- Memory: `aikito show|edit|rename|rm memory`; use the `durable-memory` skill to
  decide scope, content, and versioning.
- Inbox notes: use `aikito show inbox <name>.md` to read a named note, or
  `aikito show inbox` to list notes. Use `aikito edit|rm inbox` for changes.

Read [references/projects.md](references/projects.md) when registering or
synchronizing projects, choosing `sync_mode`, handling multi-machine paths, or
using the public project API.

Read [references/adoption.md](references/adoption.md) when importing an existing
Agent setup or resolving adoption diagnostics.
