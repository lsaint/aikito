# Architecture

Aikito separates a stateless CLI source checkout from a stateful, Git-managed
user workspace. This boundary allows the CLI to evolve without treating user
memory and configuration as application installation files.

## Source and Workspace

The two directories serve different purposes:

```text
~/aikito-src   CLI source checkout
~/aikito       canonical user workspace
```

They must remain separate. The default workspace is `~/aikito`; set
`AIKITO_DIR` to select another path.

## Canonical Source

The workspace is the source of truth:

```text
~/aikito
├── global/AGENTS.md
├── skills/
├── memory/
├── projects/
├── mcps/
├── agents.toml
├── skills.toml
└── subagents.toml
        |
        | aikito sync ...
        v
Agent-native configs + <project>/.agents/
```

Agent configuration directories and project-level `.agents/` directories are
runtime entry points. Do not maintain independent copies there when Aikito owns
the corresponding resource.

## Use or Adapt the Aikito Skill

The complete [Aikito skill](../skills/aikito/SKILL.md) teaches a coding agent
how to install, configure, and operate an Aikito workspace. Use it as provided
or adapt its workspace layout, Agent registry, synchronization policy, and
review requirements to match your environment.

## Resources

| Resource | Canonical source | Purpose |
| --- | --- | --- |
| Memory | `memory/`, `projects/<name>/memory/` | Durable global and project knowledge |
| Skills | `skills/<name>/` | Reusable Agent workflows |
| Instructions | `global/AGENTS.md`, `projects/<name>/AGENTS.md` | Global and project behavior |
| MCP servers | `mcps/*.toml` | Cross-Agent server definitions |
| Subagents | `subagents.toml`, `subagents/` | Cross-Agent specialist definitions |
| Agent registry | `agents.toml` | Integration paths and supported capabilities |

Aikito calls the resource “instructions” while retaining the ecosystem-standard
`AGENTS.md` filename for its canonical content.

Integrations are capability-based. An Agent may participate in instructions,
skills, MCP, or subagent synchronization independently. The default registry
contains Codex, Claude Code, Antigravity CLI (`agy`), OpenCode, GitHub Copilot CLI, and DeepSeek Harness (`dsh`).

## Project Runtime Directory

Project synchronization creates a managed `.agents/` directory in the target
project. Project instructions and memory use symbolic links so their canonical
content remains in the Aikito workspace. Skills use the project's configured
`sync_mode`, which can link or copy them.

### Project Skill Sync Modes

`sync_mode` applies only to project skills under `.agents/skills/`. Project
instructions and memory always remain linked to the Aikito workspace so that
their canonical content has a single source of truth. Selecting `copy` therefore
does not make every managed project resource independent of Aikito or eliminate
all symbolic links.

The two skill modes serve different collaboration models:

| Mode | Runtime representation | Design intent | Trade-off |
| --- | --- | --- | --- |
| `link` | Symbolic links to workspace skills | Keep one live canonical skill shared by projects | Skill content is not stored in the project repository and requires access to the Aikito workspace and symbolic-link support |
| `copy` | Managed copies inside the project | Make selected skill contents reviewable and versionable with the project | Copies can drift from their canonical skills and must be reconciled before synchronization replaces collaborator changes |

Use `link` when the workspace is the authoritative working environment and
projects should immediately see canonical skill updates. Use `copy` when a
project needs a self-contained, Git-trackable snapshot of its selected skills,
for example when collaborators or CI do not share the same Aikito workspace.
Treat copied skills as generated project artifacts. `aikito status` reports
drift and `aikito diff` compares runtime files with their canonical workspace
versions. Make lasting improvements in the canonical skill, or reconcile
project changes back into it before synchronizing again. Synchronization stops
on drift unless `--force` is supplied after review.

The `.agents/skills/` and `.agents/memory/` directories are exclusively managed
by Aikito. Entries not selected by the project configuration are unmanaged
conflicts unless ownership can be proven from an Aikito workspace symlink or an
unchanged canonical copy. Proven stale entries are removed; synchronization
stops without deleting everything else. Do not store unrelated files in those
directories.

## Synchronization Behavior

Aikito plans synchronization against the canonical workspace, identifies
managed and unmanaged targets, and stops on conflicts that require user
judgment. Managed-entry fingerprints expose drift rather than silently
replacing local changes.

Use `aikito status` for the aggregate view and the resource-specific status
commands for details. See [Safety model](safety.md) for write boundaries and
recovery expectations.
