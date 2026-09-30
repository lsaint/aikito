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
A customized target is kept when the source is still at a template. Other
differing resources conflict and remain unchanged while the reference-safe
subset applies. Missing references prune the offending logical changes and
their dependents to a fixed point; unresolved preexisting reference errors are
global findings. `--keep-target` maps a conflict to NOOP and `--take-source`
maps it to UPDATE, both by resource ID and through the same plan and writer.
Reference checks run again after choices. Shared TOML rendering includes only
selected members, including when creating a previously absent project file. Preview validates references against the
whole result, reports managed-area findings together, and treats possible
plaintext credentials as warnings. Changing the effective inbox path is blocked
when the target inbox contains notes; the preview reports their count and
location because relocating the path would leave those notes unmanaged. The source workspace is never modified.
Agent definitions, workspace configuration, global instructions, project
instructions, and the project `sync_mode` use their bundled templates as the
comparison baseline, including every earlier shipped version of a template. A target still at the template
adopts a changed source; a customized target is retained when the source still
matches the template. Independently changed values conflict. TOML field merges
retain unrelated target fields and comments.


Import execution uses `workspace.resource_write`: each logical resource carries
its source and expected target fingerprint. Execution reuses the snapshots from
the locked plan recheck, captures physical file versions before rendering, and
scans the target again for final verification. A single storage contract in
`workspace.resources` classifies both snapshots and writes. Shared TOML changes
are composed into one replacement per physical file, then `workspace.transactions`
stages and installs the complete batch. A fresh snapshot must match the
expected logical resources, preserve target-only resources, and have valid
references before the transaction commits. Verification failures roll back the
whole batch, just like write failures.

Version 2 transaction journals record each final destination and the path
policy used during staging, including the current and planned inbox prefixes.
Recovery validates these paths and retains caller-owned resource/state
permissions; it does not infer the inbox location from staged TOML. Version 1
journals still use the caller's path policy.

## Internal Resource Reconciliation

`workspace.reconcile` connects one workspace replica to a `FilesystemRemote`.
This remains an internal API and is separate from runtime `aikito sync`.
Supported kinds are `memory`, `project-memory`, `skill`, `inbox`,
`global-instructions`, `project-instructions`, `agent`, `mcp`, `subagent`,
`skill-selection`, `project`, `project-field`, `project-path`, `project-skill`,
and `config`. Bundled skill contents, `.local`, Git metadata, generated runtime
entries, and scanner exclusions stay outside reconciliation. Selections of
bundled skills are shared; their providers come from the installed package.

The host-local configuration exclusion is `config:inbox.path`. Each replica
keeps its own inbox prefix and receives notes by logical name beneath that
prefix; the center uses `inbox/`. An inbox outside the workspace blocks inbox
reconciliation, including absence-based deletion. Shared preferences such as
`memory.stale_days` and `update.check` participate. Project path candidates,
including offline candidates, are set members; runtime sync still decides
which path is available locally.

The resource center is an initially empty directory, not another workspace.
It stores canonical resource content and a manifest at
`.local/state/aikito/workspace-reconcile/remote.json`. The manifest contains
`sync_id`, a monotonic `revision`, and fingerprints and references keyed by
logical resource ID. Version 2 also stores typed TOML field payloads, retaining
actual key components and values through a TOML encoding. Shared config and
project files are not copied to the center: only accepted fields and collection
members enter its manifest. Version 1 centers remain readable and upgrade on
an accepted write; their project notes require a project provider before that
upgrade can commit. Content is supplied to the writer by ID, so the source
content location does not need to match a workspace layout. Physical storage paths are validated
implementation details rather than persisted comparison identities.

Each replica keeps `.local/state/aikito/workspace-reconcile/replica.json` with
its `sync_id`, `replica_id`, confirmed revision, and a per-resource base.
Replica and center root locations are not comparison identities; project path
candidates remain logical resource data. Completed replicas and centers may be
moved without changing identity. Moving an unfinished transaction is
not supported because recovery journals bind their original destinations.
The old internal `baseline.json` is rejected explicitly; it is not migrated.

First pairing compares against bundled template fingerprints, unions missing
resources, and never propagates deletions. Later rounds use the replica's base
with `workspace.merge.compare`. Plans use `CREATE`, `UPDATE`, `DELETE`, `NOOP`,
`CONFLICT`, and `BLOCKED`, with `local` or `remote` as the write target. Conflict
choices are accepted only for resources currently reported as `CONFLICT`,
including conflicts found by reference or TOML checks; choices for other IDs
raise an error. They select the local or remote version and become ordinary
writes or deletions. Selecting content restores missing referenced providers
from the selected side. Selecting deletion also removes dependent set members
absent on that side; standalone dependent content requires its own choice.
These inferred changes appear in the preview and pass reference and credential
checks. Contradictory explicit choices remain conflicts. Choices apply to one
round and must not be reused after those conflicts have converged.

The safe subset commits while conflicts and credential-blocked uploads retain
their old base. Reference checks include preserved local resources: a skill
cannot be deleted if a preserved selection would lose its provider. Project
memory and instructions reference their project; MCP and subagent definitions
reference Agents. New projects and their dependents can arrive together.
Deleting a project cannot orphan preserved instructions, notes, fields, path
candidates, or selected skills. Broken existing local references block the
round; changes that would break otherwise valid references become conflicts.

Scalar/table field overlaps are reported as preview conflicts rather than
producing an invalid merged document. Shared TOML fields and collection
additions/deletions are composed into one physical write per file. Rendering
retains unrelated typed values and standalone comment lines, but may reformat tables and discard inline comments. Import
continues using its existing renderer. Deleting every project resource removes
its `agent.toml`; empty managed memory directories left behind are ignored.
Unmanaged entries and unsafe paths still produce findings.

Center writers share a cross-process lock. A batch is accepted only if its
expected center identity, revision, and resource snapshot still match.
All resource replacements, deletions, and the incremented revision use one
transaction, verified before confirmation. Possible plaintext credentials are
blocked per logical payload before upload, including content supplied outside
a workspace. A secret in one shared field does not prevent safe fields in the
same file from advancing; blocked values never enter the center or replica base.

Center and replica commits are separate atomic transactions. Center uploads
commit first, then the replica commits downloads and its new base together.
If the replica fails after a successful upload, the center keeps that accepted
batch and the replica retains its previous base. Recovery rolls back each
unfinished batch; the next round recognizes converged uploads and retries any
remaining downloads. This protocol does not claim an atomic transaction across
both stores. Conflicting resources never advance their base, and an unchanged
round does not increment the center revision.


The end-to-end acceptance scenario lives in
`tests/workspace_reconcile_acceptance.py`. It runs as a real Python invocation
in Ubuntu, macOS, and Windows smoke jobs; focused behavior remains in pytest.
The same two replicas exercise every admitted resource kind, matching
and conflicting shared-field edits, safe progress during conflicts, stale local
and center plans with no-write assertions, shared-file recovery, repeated
execution, and relocation of completed roots. Final snapshots match the center
by logical resource fingerprint while each replica retains its own inbox path.

## Source package

The `aikito.workspace` package exports the public `Workspace` facade and its
result models. `api.py` implements that facade; `paths.py` owns workspace
resolution and the persisted workspace pointer. Internal callers import the
owning submodule directly rather than importing implementation helpers from
the package facade.

`sync.py` coordinates agent runtime synchronization, while `inspection.py`
provides a shared, lazy inspection context for one command. `importing.py` and
`reconcile.py` implement distinct workspace transfer policies over shared
resource classification, comparison, rendering, and filesystem transactions.
`transactions.py` owns staged writes, journaling, rollback, and crash recovery.
This source organization does not change the canonical workspace data layout.
