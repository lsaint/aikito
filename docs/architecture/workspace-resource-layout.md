# Workspace resource layout decision

The implemented layout stores Agents as TOML files, subagents as Markdown,
notes/MCP definitions as files, skills as directories, and project resources
under `projects/<name>/`. Logical IDs are independent of physical paths;
collection fields in `skills.toml` and project configuration are lists.

## Canonical files

| Resource | Canonical location and identity |
| --- | --- |
| Agent | `agents/<name>.toml`, ID `agent:<name>`; exactly one `[agents.<name>]` table, including nested capabilities. Filename and table name must match. |
| Subagent | `subagents/<name>.md`, ID `subagent:<name>`; filename supplies the name, frontmatter supplies metadata, body supplies instructions. |

Agent files keep the existing table schema, so bundled
`templates/agents/<name>.toml` fragments work without field conversion:

```toml
# agents/codex.toml
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

[agents.codex.runner]
command = ["codex", "-C", "{workdir}", "{prompt}"]
```

Subagent frontmatter requires `description` and a nonempty `agents` list, with
optional platform tables. Inline JSON objects preserve nested TOML values
without a YAML dependency. Duplicate keys, unknown fields, invalid types and
unsupported YAML features are rejected.

```md
---
description: "Review code changes"
agents: ["codex", "claude-code"]
codex: {"model": "o3"}
---

Review the changes and report findings.
```

Instruction edits preserve frontmatter; metadata edits preserve the body and
unrecognized Markdown below the closing delimiter. Only migration reads legacy
`subagents.toml`; new definitions always use frontmatter.

## Required migration gate

Git-tracked `layout.toml` with `version = 2` marks a completed layout. Fresh
initialization writes only this layout. Missing/unsupported markers, remaining
legacy files or interrupted migration block normal commands before runtime
resource reads/writes. Loaders have no `agents.toml`/`subagents.toml` fallback.

The CLI prints the workspace path and explicit upgrade commands:

```text
This workspace needs migration. Run:
  aikito migrate workspace-resources --dry-run
  aikito migrate workspace-resources
```

Help, version, workspace path discovery, fresh initialization and migration stay
available. Connecting/reinitializing a legacy workspace remains blocked.
Installation and sync never migrate user workspaces automatically.

## Migration command

`aikito migrate workspace-resources --dry-run` reports creates/removals,
unsupported names and collisions without writing. Apply rechecks under the
workspace writer lock and uses the shared transaction journal:

1. Stage split Agent files, preserving table text and pre-table comments attached
   to each Agent; registry header comments may be discarded. Verify parsed
   definitions are unchanged.
2. Add subagent frontmatter without changing instruction bodies. Preserve legacy
   table comments in the resource's frontmatter and comments without a resource
   in `layout.toml`; verify metadata, body and platform options match.
3. Install staged files and remove legacy files in one transaction. Write
   `layout.toml` last, verify the result, then commit and remove the temporary
   recovery copy.

Migration is idempotent. Duplicate definitions across layouts are collisions,
never resolved by preference. Before planning, recover the migration's pending
transaction under the lock; external edits that prevent safe recovery keep it
blocked. Partial migration cannot enable normal commands. Legacy parsing exists
only inside migration.

## Implementation and verification boundary

Initialization, loaders, `add`/`rm`, `doctor`, status, runtime sync, registry
maintenance and snapshots use the same layout. Tests cover fresh workspaces,
legacy gating, collisions, metadata/comment round trips, dry-run zero writes,
interruption, recovery and idempotency. Ubuntu, macOS and Windows smoke jobs run
the real migration and assert new files/marker exist and legacy files are absent.

## Workspace import

`aikito import workspace <source> [--dry-run]` transfers inbox notes, memory,
skills/selections, Agents, subagents, MCP definitions, projects, configuration
fields and global/project instructions. Missing projects need no local checkout;
the source is never modified.

Selections and project paths/skills merge by member. Agent definitions,
configuration, instructions and project `sync_mode` compare against bundled
templates, including earlier shipped versions: an absent/default target adopts
a changed source; a customized target stays when the source is still a template;
independent edits conflict. TOML field merges preserve unrelated target values
and comments. `--keep-target` selects NOOP and `--take-source` selects UPDATE by
conflicting resource ID through the same planner/writer.

Only the reference-safe subset applies. Missing providers prune offending changes
and dependents to a fixed point; preexisting unresolved reference errors are
global findings. Recheck references after choices and render only selected shared
members, even for newly created project files. Preview reports managed-area
findings together and warns about possible plaintext credentials. Changing the
effective inbox path is blocked while the target inbox contains notes; report
their count/location rather than leaving them unmanaged.

`workspace.resource_write` carries source and expected-target fingerprints,
reuses locked-plan snapshots and captures physical versions before rendering.
`workspace.resources` supplies one storage classification for snapshots/writes.
Compose shared changes once per physical file; `workspace.transactions` stages
and installs the whole batch. Verify a fresh target snapshot against expected
logical resources, target-only preservation and valid references before commit.
Write or verification failure rolls back the batch.

Transaction journals v2 retain final destinations and the staging path policy,
including current/planned inbox prefixes. Recovery validates these paths and
caller-owned resource/state permissions; it never infers inbox location from
staged TOML. Version-1 journals use the caller's path policy.

## Internal Resource Reconciliation

`workspace.reconcile` is the internal resource-transfer API, separate from runtime
`aikito sync`. It supports global/project memory, skills, inbox notes,
global/project instructions, Agents, MCPs, subagents, skill selections, project
providers/fields/path candidates/selected skills, and shared configuration.
Bundled skill contents, `.local`, Git metadata, generated runtime entries and
scanner exclusions are omitted. Bundled skill selections are shared, but their
providers come from the installed package.

`config:inbox.path` stays host-local: replicas receive logical note names beneath
their own inbox prefix; the filesystem center uses `inbox/`. An external inbox
blocks all inbox reconciliation, including absence-based deletion. Shared
preferences such as `memory.stale_days`/`update.check` sync normally. Project paths,
including offline candidates, are set members; runtime sync decides availability.

`FilesystemRemote` is an initially empty center directory, not a workspace. Its
manifest is `.local/state/aikito/workspace-reconcile/remote.json`; replica identity,
confirmed revision and per-resource Base live in the replica's corresponding
`replica.json`. The center stores accepted fields/members with typed key components
and values rather than whole shared configuration/project files. Source content is
supplied by ID; physical locations are validated implementation details, not
comparison identities. Completed roots may move without changing identity;
unfinished transactions cannot move because journals bind destinations. Legacy
`baseline.json` is rejected without migration. Current schema, snapshot, receipt
and transaction rules are defined in [Remote Store Boundary](remote-store-boundary.md).

The plaintext engine initially unions missing resources using template baselines;
encrypted clients instead back up local differences and adopt an existing Vault.
Later rounds compare per-resource Base with `workspace.merge.compare`. Plans use
`CREATE`, `UPDATE`, `DELETE`, `NOOP`, `CONFLICT`, `BLOCKED` and budgeted `DEFERRED`,
targeting `local` or `remote` for writes.

Conflict choices apply only to currently conflicting IDs, including reference/TOML
conflicts. Selecting content restores missing providers from that side; selecting
deletion also removes dependent set members absent there. Standalone dependents
need their own choice. Inferred changes appear in preview and pass client checks;
contradictory choices remain conflicts. Choices survive budget deferral until
applied, then must not be reused after convergence.

Preserved resources participate in reference checks: selections need skills,
project notes/instructions/fields/paths/selections need their project, and MCPs or
subagents need Agents. Providers/dependents can arrive together; deletion cannot
orphan preserved content. Broken existing local references block the round;
changes that would break valid references become conflicts. Safe independent
resources advance while conflicts/policy-blocked uploads keep their Base.

Scalar/table overlaps are preview conflicts. Shared additions/deletions compose
once per file; sync rendering retains unrelated typed values and standalone
comments but may reformat tables or drop inline comments. Import keeps its own
renderer. Removing all project resources removes `agent.toml`; leftover empty
managed memory directories are ignored. Unmanaged entries/unsafe paths produce
findings. Plaintext credential policy blocks logical payloads individually, so a
blocked shared field need not stop safe fields. Encrypted sync permits credential
content while retaining path/reference/integrity checks.

Center and replica commits are independent atomic transactions: remote acceptance
precedes local resource/Base publication. Local failure preserves accepted uploads
and old local Base; receipt recovery confirms uploads before replanning current
files. NOOP does not increment remote revision. See
[application and recovery](remote-store-boundary.md#application-sequence) for the
full ordering and failure contract. Shared two-backend acceptance covers all
resource kinds, safe progress/conflicts, stale-plan zero writes, recovery, repeat
execution and completed-root relocation in all three OS smoke jobs.

## Source package

| Module | Owns |
| --- | --- |
| `aikito.workspace` / `api.py` | Public `Workspace` facade and result models; internal callers import owning submodules directly. |
| `paths.py` | Workspace resolution and persisted selection pointer. |
| `sync.py` / `inspection.py` | Agent runtime synchronization / one-command lazy inspection context. |
| `importing.py` / `reconcile.py` | Distinct transfer policies over shared classification, comparison, rendering and transactions. |
| `transactions.py` | Staging, journaling, rollback and crash recovery. |

Source organization does not change the canonical data layout.
