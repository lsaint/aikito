---
name: aikito
description: Install, configure, and operate Aikito workspaces, including memory, skills, project resources, MCP servers, subagents, adoption, synchronization, and status verification.
---

# Aikito

## Purpose

Use Aikito as the canonical, Git-managed source for durable Agent resources and
synchronize those resources into supported coding agents and projects.

## Mental Model

Keep the CLI source and user workspace separate:

```text
~/aikito-src   CLI source checkout
<workspace>    canonical user workspace (defaults to ~/aikito)
```

The workspace contains the sources of truth:

```text
<workspace>/
├── agents.toml
├── skills.toml
├── subagents.toml
├── mcps/
├── global/AGENTS.md
├── skills/
├── memory/
└── projects/<name>/
```

Agent configuration directories and project `.agents/` directories are runtime
entry points, not independent sources. Modify canonical workspace files first,
then synchronize them with the CLI.

Standard lifecycle for new canonical resources:

```text
add ↓
edit ↓
show ↓
sync ↓
status / diff / doctor
```

Existing external resources follow a separate import path:

```text
adopt ↓
canonical resource ↓
show / edit / sync / status
```

Resolve `<workspace>` with `aikito path workspace` before accessing canonical
files directly. `AIKITO_DIR` can temporarily override the persisted workspace.
Use the installed `aikito` command rather than assuming the CLI lives inside
the workspace.

## Bootstrap

When the CLI is not installed, use the official source repository and keep its
checkout separate from the future workspace:

```bash
git clone https://github.com/lsaint/aikito.git "$HOME/aikito-src"
export PATH="$HOME/aikito-src/bin:$PATH"
```

Before cloning, verify that the target does not contain unrelated user data.
Do not overwrite an existing checkout without the user's direction.

Initialize and inspect a workspace:

```bash
aikito init workspace ~/aikito
aikito status
```

`aikito init workspace` refuses the CLI source tree, another source checkout,
and an unrecognized non-empty directory. Do not try to bypass these guards.
Initialization detects locally installed Agents and writes only those entries to
the root `agents.toml`, which is the Agent source of truth for all projects.
After initialization, inspect the generated files before synchronizing
anything.

## Safety Protocol

- Start with read-only discovery: inspect relevant canonical files, run
  `aikito status`, and use resource-specific status commands.
- Run `aikito adopt` without `--apply` first. Show the user the adoption plan,
  conflicts, and credential handling before applying it.
- Use `--dry-run` for synchronization commands that support it.
- Treat unmanaged targets and conflicting instructions as decisions for the
  user. Do not silently overwrite them.
- Keep API keys, tokens, passwords, and OAuth material out of the Aikito
  workspace and Git. Store environment-variable references in canonical MCP
  configuration.
- Warn before any explicit force or prune operation and scope it to the reviewed
  target.
- Do not treat a local Git repository as safe to publish. Review memory and
  configuration for private data before adding a remote or pushing.
- Preserve unrelated user changes in both the workspace and target Agent
  configuration.

## Resource Routing

### Global Instructions and Skills

Canonical sources:

```text
<workspace>/global/AGENTS.md
<workspace>/skills.toml
<workspace>/skills/<skill-name>/
```

Create, review, and synchronize skills:

```bash
aikito add skill <name> [--description <desc>]
aikito edit skill <target>
aikito show skills
aikito show skill <target>
aikito sync global --dry-run
aikito sync global
aikito status
```

Do not edit generated Agent-native instruction or skill entries as independent
sources.

When adding, renaming, or removing a global skill, treat the change as a
cross-runtime migration rather than a directory-only edit:

- Update `skills.toml` and references to the old skill name. If the skill is a
  bundled CLI resource, update its source and init template in the CLI source
  checkout separately; the user workspace is never a release source.
- Verify that every supported Agent that loads global skills has the correct
  `skills_path` in `agents.toml` and in the `aikito init` template. Do not assume
  that an Agent CLI and its IDE use the same global skill directory.
- Run the relevant tests, then run `aikito sync global` and `aikito status`.
- Inspect each managed Agent target and confirm that the new skill is visible
  and any stale link for the old name is gone.
- Keep a global skill out of project `agent.toml` files unless the project
  intentionally overrides or copies it.

### Projects

A workspace is the central source of truth and normally exists once. A project
registration represents one code directory and its project-specific Agent
resources. When a requested instruction, skill selection, or memory is
project-specific and the current code directory is not registered, ask whether
the user wants to register it. Do not register a project for a global resource.

After confirmation, initialize the project from its code directory:

```bash
aikito init project
```

The directory name and current path are the defaults. Use explicit arguments
when needed:

```bash
aikito init project <name> <path>
```

This command creates the canonical project skeleton and synchronizes its
`.agents/` runtime. Treat existing unmanaged runtime resources or a project
name bound to another path as conflicts for the user; do not bypass them.

Project configuration belongs under:

```text
<workspace>/projects/<name>/agent.toml
<workspace>/projects/<name>/AGENTS.md
<workspace>/projects/<name>/memory/
```

Synchronize and verify with:

```bash
aikito show projects
aikito show project <name>
aikito sync project <name> --dry-run
aikito sync project <name>
aikito status
aikito diff
aikito show memory
```

Project instructions and memory remain linked to canonical sources. Project
skills follow `sync_mode`: `link` keeps symbolic links; `copy` generates managed
copies for project Git tracking. Use `aikito status` to detect copied-skill drift
and `aikito diff` to compare it with the canonical skill. Synchronization refuses
to replace drifted copies; merge changes that should survive, or use
`aikito sync project <name> --force` only after review.

Project instructions are linked for every Agent registered in the workspace root
`agents.toml`, using each configured `project_instruction_path`. Shared targets
are deduplicated, and an existing unmanaged file or link is always a conflict.
An empty canonical project `AGENTS.md` disables these links. Synchronization
cleans only links proven to target that canonical file, while `show project`
reports a repository-owned `AGENTS.md` as an informational notice.

Project `.agents/skills/` uses entry-level ownership. Preserve project-owned
skills not selected in `agent.toml` and report them only as notices; a selected
skill with the same name remains a conflict. Matching directory contents do not
prove Aikito ownership. `.agents/memory/` is exclusively managed by Aikito, and
unknown entries there remain conflicts.

### MCP Servers

Canonical source:

```text
<workspace>/mcps/*.toml
```

Unlike Skills (`skills.toml`) and Subagents (`subagents.toml`) which use central
registry files alongside definition files, MCP servers use a file-based
self-registering model (`mcps/<name>.toml`). Creating the TOML configuration
file automatically registers the server.

Create, preview, apply, and verify:

```bash
aikito add mcp <name> [--url <url> | --command <command>]
aikito edit mcp <target>
aikito sync mcp --dry-run
aikito sync mcp
aikito show mcp
```

Use `aikito show mcp --live` only when live Agent checks are useful. Use
`aikito auth mcp <agent> <server>` for supported authentication flows. Never
print, persist, or commit captured credentials.

### Subagents

Canonical sources:

```text
<workspace>/subagents.toml
<workspace>/subagents/
```

Create, preview, apply, and verify:

```bash
aikito add subagent <name> [--description <desc>]
aikito edit subagent <target>
aikito sync subagents --dry-run
aikito sync subagents
aikito show subagents
aikito show subagents --agent <agent>
aikito show subagent <target> [--agent]
```

Review orphaned managed files before using `--prune`. Never force an unmanaged
target merely to make status green.

### Memory

Global memory belongs under `<workspace>/memory/`. Project-specific memory
belongs under `<workspace>/projects/<name>/memory/`.

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

Preview existing Agent configuration before importing it:

```bash
aikito adopt
```

Only after the user reviews the plan and resolves instruction conflicts:

```bash
aikito adopt --apply
```

Application creates timestamped backups under
`~/.aikito/backups/adopt_<timestamp>`. Adoption imports resources into the
workspace; Agent-native configuration changes only during explicit
synchronization.

## Verification and Current Behavior

After every write, run the narrowest relevant status command and inspect the
reported targets. Use `aikito <command> --help` as the authority for options
supported by the installed version. When working from a source checkout,
consult its root README and relevant linked documentation for the current
architecture and safety model.
