# Workspace resource layout decision

Status: implemented.

The canonical workspace stores each Agent definition in one TOML file and
each subagent definition in one Markdown file. The logical resource identities
are `agent:<name>` and `subagent:<name>`. Memory and inbox notes are files,
skills are directories, MCP definitions are files, and project resources are
grouped under each project directory. Collection fields in `skills.toml` and
project configuration are lists.

## Canonical files

An Agent lives at `agents/<name>.toml`. Each file contains exactly one existing
`[agents.<name>]` table, including its nested capability tables. The filename
and table name must match. Keeping the current table schema allows the bundled
`templates/agents/<name>.toml` fragments to serve as workspace files without
rewriting their fields.

```toml
# agents/codex.toml
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

[agents.codex.runner]
command = ["codex", "-C", "{workdir}", "{prompt}"]
```

A subagent lives at `subagents/<name>.md`. Its filename supplies its name; YAML
frontmatter contains `description`, a nonempty `agents` list, and optional
platform tables. The Markdown body contains the instructions. Platform tables
use inline JSON objects where nested values are needed, so their TOML values
round-trip without a YAML dependency. The parser rejects duplicate keys,
unknown fields, invalid types, and unsupported YAML features.

```md
---
description: "Review code changes"
agents: ["codex", "claude-code"]
codex: {"model": "o3"}
---

Review the changes and report findings.
```

The existing `subagents.toml` is read only by the migration command. New
subagents write their metadata into Markdown frontmatter. Editing
instructions preserves frontmatter; changing metadata preserves the body and
unrecognized Markdown content below the closing delimiter.

## Required migration gate

Use a Git-tracked `layout.toml` with `version = 2` as the completion marker.
New initialization writes the marker and only the new layout. A workspace
without this marker, with an unsupported marker version, or with legacy files
still present is unavailable to normal commands. Runtime loaders read only
`agents/*.toml` and subagent Markdown frontmatter; they contain no fallback for
`agents.toml` or `subagents.toml`.

The installation step does not modify a user workspace. On the first normal
command targeting a legacy workspace, the CLI exits before reading or writing
runtime resources and prints the workspace path plus the exact command:

```text
This workspace needs migration. Run:
  aikito migrate workspace-resources --dry-run
  aikito migrate workspace-resources
```

Help, version, workspace path discovery, fresh workspace initialization, and
the migration command remain available. Connecting or reinitializing an
existing legacy workspace is blocked with the same guidance. A migration that
was interrupted also keeps normal commands blocked until recovery completes.
This is an explicit one-time upgrade, with no compatibility adapter in normal
operation and no automatic migration during package installation or sync.

## Migration command

The dedicated `aikito migrate workspace-resources --dry-run` command previews
the change; without `--dry-run`, it applies the change under the workspace
writer lock and shared transaction journal. A preview reports every file to
create or remove, unsupported names, and collisions before any write. Applying
the migration follows these steps:

1. Recheck the preview under the lock and stage all new files.
2. Split each legacy Agent table into a file while preserving its table text
   and attaching pre-table comments to that Agent. Registry header comments
   can be discarded. Verify that the parsed definition is equal.
3. Add subagent frontmatter without changing its instruction body. Move
   standalone comments from each legacy table into that resource's frontmatter;
   preserve comments without a resource in `layout.toml`. Verify the parsed
   metadata, body, and platform options are equal to the legacy pair.
4. Install staged files and remove legacy files in one journaled transaction.
   Write `layout.toml` last, then verify the new layout before committing the
   transaction. Retain a recovery copy until the transaction is committed.
   Interrupted runs recover or stop on external edits.

The command is idempotent. It never resolves conflicting duplicate definitions
by preference. If both layouts contain the same resource before migration,
the plan reports a collision and requires the user to resolve it. The
migration-only parser can read old files; normal resource loaders cannot.
Before building a new plan, the command recovers its own pending transaction
under the lock; an external edit that prevents safe recovery remains blocked.
Comments from `subagents.toml` remain in a resource's frontmatter or, when the
registry has no resources, in `layout.toml`; the transaction recovery copy is
temporary and is removed after a successful migration. A partially migrated
workspace cannot run normal commands because the completion marker has not
been written.

## Implementation and verification boundary

The migration updates initialization, Agent and subagent loaders,
`add`/`rm`, `doctor`, status, runtime synchronization, registry maintenance,
and logical resource snapshots together. Legacy fixtures exercise the
migration gate and migration-only parser. Tests cover fresh workspaces,
collisions, metadata round trips, dry-run zero writes, interrupted migration,
recovery, and idempotency. They also verify that normal commands fail with an
actionable prompt before migration and work afterward.

Because migration adds a CLI command and changes initialized files, Ubuntu,
macOS, and Windows CI each execute the real command and assert the new
files exist, the legacy files are absent, and the completion marker is present.
`aikito import workspace <source> [--dry-run]` exposes workspace import. It
imports inbox notes, global skill selections, subagents, MCP definitions,
projects, memory, skills, Agent definitions, workspace configuration fields,
and global instructions. Missing projects are
created in the target workspace without requiring a local code checkout. Skill
selections and project paths/skills merge by member. Project configuration
adopts source fields when the target field is absent or still at its init
default; an unmodified project instructions template is replaced by the source.
Other differing resources conflict. Preview validates references against the
whole result, reports managed-area findings together, and treats possible
plaintext credentials as warnings. The source workspace is never modified.
Agent definitions, workspace configuration, and global instructions use their
bundled templates as the comparison baseline. A target still at the template
adopts a changed source; a customized target is retained when the source still
matches the template. Independently changed values conflict. TOML field merges
retain unrelated target fields and comments.
