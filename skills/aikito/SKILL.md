---
name: aikito
description: Install, configure, and operate Aikito workspaces, including skills, memory, project resources, MCP servers, subagents, adoption, synchronization, and status verification.
---

# Aikito

## Purpose

Use Aikito as the canonical, Git-managed source for durable Agent resources and
synchronize those resources into supported coding agents and projects.

## Mental Model

Keep the CLI source and user workspace separate:

```text
~/aikito-src   CLI source checkout
~/aikito       canonical user workspace
```

The workspace contains the sources of truth:

```text
~/aikito/
├── agents.toml
├── skills.toml
├── mcps.toml
├── subagents.toml
├── global/AGENTS.md
├── skills/
├── memory/
└── projects/<name>/
```

Agent configuration directories and project `.agents/` directories are runtime
entry points, not independent sources. Modify canonical workspace files first,
then synchronize them with the CLI.

Use `AIKITO_DIR` when the workspace is not `~/aikito`. Use the installed
`aikito` command rather than assuming the CLI lives inside the workspace.

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
aikito init ~/aikito
aikito status
```

`aikito init` refuses the CLI source tree, another source checkout, and an
unrecognized non-empty directory. Do not try to bypass these guards. After
initialization, inspect the generated files before synchronizing anything.

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
~/aikito/global/AGENTS.md
~/aikito/skills.toml
~/aikito/skills/<skill-name>/
```

After reviewing changes:

```bash
aikito sync global
aikito status
aikito status skills
aikito show skill <target>
aikito edit skill <target>
```

Do not edit generated Agent-native instruction or skill entries as independent
sources.

When adding, renaming, or removing a global skill, treat the change as a
cross-runtime migration rather than a directory-only edit:

- Update `skills.toml`, the public export allowlist, and references to the old
  skill name.
- Verify that every supported Agent that loads global skills has the correct
  `skills_path` in `agents.toml` and in the `aikito init` template. Do not assume
  that an Agent CLI and its IDE use the same global skill directory.
- Run the relevant tests, then run `aikito sync global` and `aikito status`.
- Inspect each managed Agent target and confirm that the new skill is visible
  and any stale link for the old name is gone.
- Keep a global skill out of project `agent.toml` files unless the project
  intentionally overrides or copies it.

### Projects

Project configuration belongs under:

```text
~/aikito/projects/<name>/agent.toml
~/aikito/projects/<name>/AGENTS.md
~/aikito/projects/<name>/memory/
```

Synchronize and verify with:

```bash
aikito sync project <name>
aikito status
aikito status memory
```

Project instructions and memory remain linked to canonical sources. Project
skills follow `sync_mode`: `link` keeps symbolic links; `copy` generates managed
copies for project Git tracking. Before replacing a managed copy that contains
collaborator changes, compare it with the canonical skill and merge changes
that should survive.

The target `.agents/skills/` and `.agents/memory/` directories are exclusively
managed by Aikito. Do not store unrelated files there.

### MCP Servers

Canonical source:

```text
~/aikito/mcps.toml
```

Preview, apply, and verify:

```bash
aikito sync mcp --dry-run
aikito sync mcp
aikito status mcp
```

Use `aikito status mcp --live` only when live Agent checks are useful. Use
`aikito auth mcp <agent> <server>` for supported authentication flows. Never
print, persist, or commit captured credentials.

### Subagents

Canonical sources:

```text
~/aikito/subagents.toml
~/aikito/subagents/
```

Preview, apply, and verify:

```bash
aikito sync subagents --dry-run
aikito sync subagents
aikito status subagents
```

Review orphaned managed files before using `--prune`. Never force an unmanaged
target merely to make status green.

### Memory

Global memory belongs under `~/aikito/memory/`. Project-specific memory belongs
under `~/aikito/projects/<name>/memory/`.

```bash
aikito status memory
aikito show memory <target>
aikito edit memory <target>
```

Use the separate `durable-memory` skill to decide when knowledge is durable
enough to persist, which scope owns it, and how to version it.

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
