---
name: aikito
description: Install, configure, and operate Aikito workspaces, including memory, skills, project resources, MCP servers, subagents, adoption, synchronization, and status verification.
---

# Aikito

## Purpose

Use Aikito as the canonical, Git-managed source for durable Agent resources and
synchronize those resources into supported coding agents and projects.

## Mental Model

Keep the CLI source checkout (`~/aikito-src`) separate from the canonical user
workspace (`<workspace>`, defaults to `~/aikito`). The workspace holds every
source of truth:

```text
<workspace>/
├── agents.toml                 # detected Agents; Agent source of truth for all projects
├── skills.toml                 # global skill selection
├── skills/<skill-name>/
├── subagents.toml + subagents/
├── mcps/<name>.toml            # file-based self-registration, no central registry
├── global/AGENTS.md
├── memory/                     # global memory
└── projects/<name>/            # agent.toml, AGENTS.md, memory/
```

Agent configuration directories and project `.agents/` directories are runtime
entry points, never independent sources: modify canonical files first, then
synchronize, and never edit or treat a generated Agent-native instruction, skill,
subagent, or MCP entry as a source.

New canonical resources follow `add → edit → show → sync → status / diff /
doctor`; existing external resources enter through `adopt` and then rejoin that
path at `show`. Every resource type supports `aikito add|edit|show|rm <type>
<target>` and a scoped `sync` with `--dry-run`. Run bare `aikito sync
[--dry-run]` to orchestrate the entire workspace (global resources, host-gated
subagents, tolerant MCP configs, and active projects).

Resolve `<workspace>` with `aikito path workspace` before touching canonical
files directly; `AIKITO_DIR` temporarily overrides the persisted workspace. Use
the installed `aikito` command rather than assuming the CLI lives inside the
workspace. Treat `aikito <command> --help` as the authority for options in the
installed version, and when working from a source checkout consult its README
and linked documentation for the current architecture and safety model.

## Bootstrap

When the CLI is not installed, clone the official source outside the future
workspace, after verifying the target holds no unrelated user data and without
overwriting an existing checkout unprompted:

```bash
git clone https://github.com/lsaint/aikito.git "$HOME/aikito-src"
export PATH="$HOME/aikito-src/bin:$PATH"
```

For a fresh workspace:

```bash
aikito init workspace ~/aikito
aikito sync
aikito status
```

For an existing workspace on a new machine:

```bash
# Download or clone your workspace repo to ~/aikito
aikito init workspace ~/aikito
aikito sync
```

`aikito init workspace` refuses the CLI source tree, another source checkout,
and an unrecognized non-empty directory. On an existing workspace, it binds the
local pointer without modifying runtime configs. Uninstalled Agents and
candidate paths on other machines are treated as `offline on this host` without
failures.

## Safety Protocol

- Start read-only: inspect the relevant canonical files and run `aikito status`
  or the narrowest resource-specific status command. Do the same after every
  write, inspecting the reported targets.
- Preview before applying: `aikito adopt` without `--apply`, and `--dry-run`
  wherever synchronization supports it. Show the user the plan, conflicts, and
  credential handling first.
- Treat unmanaged targets, drifted copies, and conflicting instructions as
  user decisions. Never silently overwrite them, and never force or prune
  merely to make status green. Warn first, scope the operation to the reviewed
  target, and review orphaned managed files before `--prune`.
- Preserve unrelated user changes in both the workspace and target Agent
  configuration.
- Keep API keys, tokens, passwords, and OAuth material out of the workspace and
  Git; store environment-variable references in canonical MCP configuration and
  never print, persist, or commit captured credentials.
- A local Git repository is not safe to publish. Review memory and configuration
  for private data before adding a remote or pushing.

## Resource Routing

### Global Instructions and Skills

```bash
aikito add skill <name> [--description <desc>]
aikito show skills | aikito show skill <target>
aikito sync global --dry-run
aikito sync global
```

Adding, renaming, or removing a global skill is a cross-runtime migration, not a
directory-only edit:

- Update `skills.toml` and every reference to the old name. If the skill is a
  bundled CLI resource, update its source and init template in the CLI source
  checkout separately; the user workspace is never a release source.
- Verify that each supported Agent loading global skills has the correct
  `skills_path` in `agents.toml` and in the `aikito init` template. An Agent CLI
  and its IDE may use different global skill directories.
- Run the relevant tests, then `aikito sync global` and `aikito status`.
- Inspect each managed Agent target: the new skill is visible and any stale link
  for the old name is gone.
- Keep a global skill out of project `agent.toml` unless the project
  intentionally overrides or copies it.

### Projects

A workspace normally exists once; a project registration represents one code
directory and its project-specific resources. When a requested instruction,
skill selection, or memory is project-specific and the current code directory is
unregistered, ask whether to register it. Never register a project for a global
resource.

```bash
aikito init project [<name> <path>]      # defaults to directory name and cwd
aikito show projects | aikito show project <name>
aikito sync project <name> [--dry-run|--force]
aikito status | aikito diff
```

`init project` creates the canonical skeleton
(`projects/<name>/agent.toml`, `AGENTS.md`, `memory/`) and synchronizes its
`.agents/` runtime. Existing unmanaged runtime resources, or a project name
bound to another path, are conflicts for the user. Projects support candidate
paths (`[paths]` table or `paths` array); non-existent paths on the host are
marked `offline on this host` and skipped cleanly during sync.

Project instructions and memory stay linked to canonical sources. Project skills
follow `sync_mode`: `link` keeps symbolic links; `copy` generates managed copies
for project Git tracking. `aikito status` detects copied-skill drift and
`aikito diff` compares it with the canonical skill; synchronization refuses to
replace drifted copies, so merge changes that should survive before using
`--force`.

Project instructions are linked for every Agent in the root `agents.toml` using
each configured `project_instruction_path`. Shared targets are deduplicated, an
existing unmanaged file or link is always a conflict, and an empty canonical
project `AGENTS.md` disables these links. Synchronization cleans only links
proven to target that canonical file, while `show project` reports a
repository-owned `AGENTS.md` as an informational notice.

`.agents/skills/` uses entry-level ownership: preserve project-owned skills not
selected in `agent.toml` and report them only as notices, while a selected skill
with the same name remains a conflict. Matching directory contents never prove
Aikito ownership. `.agents/memory/` is exclusively Aikito-managed, so unknown
entries there remain conflicts.

### MCP Servers

Creating `mcps/<name>.toml` registers the server automatically.

```bash
aikito add mcp <name> [--url <url> | --command <command>]
aikito sync mcp --dry-run
aikito sync mcp
aikito show mcp [--live]
aikito auth mcp <agent> <server>
```

Use `--live` only when live Agent checks are useful. Missing credential
environment variables emit a warning and skip the server gracefully without
blocking synchronization of other MCP configurations.

### Subagents

```bash
aikito add subagent <name> [--description <desc>]
aikito sync subagents [--dry-run|--prune]
aikito show subagents [--agent <agent>]
aikito show subagent <target> [--agent]
```

Subagent synchronization automatically host-gates against locally installed
agents; `--prune` skips offline agents to protect definitions across hosts.

### Memory

Global memory lives in `<workspace>/memory/`, project memory in
`<workspace>/projects/<name>/memory/`.

```bash
aikito show memory [target]
aikito edit memory <target>
aikito rename memory <target> <new-name>
aikito rm memory <target>
aikito doctor --fix
```

Use the separate `durable-memory` skill to decide when knowledge is durable
enough to persist, which scope owns it, and how to version it.

### Inbox

Staging notes for review:

```bash
aikito show inbox [target]
aikito edit inbox <target>
aikito rm inbox <target>
```

## Adoption

`aikito adopt` previews existing Agent configuration; `aikito adopt --apply`
imports it only after the user reviews the plan and resolves instruction
conflicts. Application creates timestamped backups under
`~/.aikito/backups/adopt_<timestamp>`. Adoption writes into the workspace only;
Agent-native configuration changes during explicit synchronization.
