# Instruction Invariants

## Core Rules
### INV-INST-01: Instruction Link-Only Contract `[current]` {: #inv-inst-01 }

Instructions (both global and project scoped) operate exclusively in link mode (`mode="link"`). There is no copy mode, no baseline fingerprint record ($B$), no content state store, and no directory copy lifecycle. Ownership is derived exclusively from the live directory entry and canonical target verification.

### INV-INST-02: Exact Canonical Destination for Instruction Symlinks `[current]` {: #inv-inst-02 }

An instruction symlink target (global or project) is owned by Aikito if and only if its literal target resolves to the exact canonical instruction file (`<workspace>/global/AGENTS.md` or `<workspace>/projects/<project>/AGENTS.md`). Symlinks pointing to other workspaces, other projects, or external paths evaluate to `FOREIGN` / `UNKNOWN` and cause `CONFLICT`.

### INV-INST-03: Rejection of Content-Matching Pseudo-Ownership `[current]` {: #inv-inst-03 }

Pre-existing regular files or directories at instruction targets evaluate to `CONFLICT` and are strictly preserved, even if their byte content matches canonical instructions. Aikito never overwrites, adopts, or unlinks regular files based on matching content.

### INV-INST-04: Shared Instruction Target Deduplication `[current]` {: #inv-inst-04 }

Multiple Agent platforms specifying identical instruction target paths within a scope (e.g. multiple agents referencing `AGENTS.md` in a checkout or host path) are deduplicated into a single physical `Target`. The target is inspected, planned, and executed exactly once per synchronization run.

### INV-INST-05: Same-Object Disposition (`SHARED_PATH`) `[current]` {: #inv-inst-05 }

When an Agent instruction target resolves to the same physical object as canonical instructions (`Target.is_same_object`), it receives read-only disposition `SHARED_PATH`. It is excluded from Executor write operations and creates no filesystem mutations.

### INV-INST-06: Global Instruction Symlink Conflict Protection (Breaking Change) `[current]` {: #inv-inst-06 }

If a global instruction target exists as a symlink pointing to an unexpected destination or external path, Aikito halts with `CONFLICT` and preserves the target. Automatic unlinking and relinking (`[RELINK]`) is eliminated.

### INV-INST-07: Project Instruction Enabled State Transitions `[current]` {: #inv-inst-07 }

When project canonical `AGENTS.md` is non-empty, instructions are enabled. Missing targets transition to `CREATE`, exact symlinks to `NOOP`, wrong/external symlinks or regular files to `CONFLICT`.

### INV-INST-08: Project Empty Canonical Owned Link Cleanup `[current]` {: #inv-inst-08 }

When project canonical `AGENTS.md` is empty, instructions are disabled. Aikito plans `UNLINK` only for symlinks that prove exact ownership to the project's canonical `AGENTS.md` (including broken symlinks pointing to it). All foreign symlinks, unmanaged links, regular files, and directories are strictly preserved.

### INV-INST-09: Legacy Instruction Stale Entry Cleanup `[current]` {: #inv-inst-09 }

Legacy paths (such as `~/.grok/AGENTS.md` or `<checkout>/.agents/AGENTS.md`) are planned for `UNLINK` if and only if they are symlinks pointing specifically to current canonical instructions. If currently configured by an Agent in `agents/<name>.toml`, they are treated as formal targets and not stale cleanup.

### INV-INST-10: Project-Owned File Preservation Under Empty Canonical `[current]` {: #inv-inst-10 }

If a project checkout contains a regular file at the instruction target when canonical is empty, the file is strictly preserved (`PRESERVE`) and a diagnostic notice is emitted. Aikito never deletes project-owned instruction files.

### INV-INST-11: Multi-Checkout and Offline Instruction Scope `[current]` {: #inv-inst-11 }

In multi-checkout projects, active checkouts generate distinct physical instruction targets frozen at Plan build time. Offline candidate paths are planned as `OFFLINE` / `SKIP` and generate zero filesystem operations. Explicit checkout paths follow CAS validation.

### INV-INST-12: Idempotent Convergence for Instructions `[current]` {: #inv-inst-12 }

In a stable configuration where all instruction links point to their canonical targets (or are cleanly absent for empty canonicals), subsequent synchronizations evaluate completely to `NOOP` or `SHARED_PATH`. Symlink destinations, `mtime_ns`, and inode numbers remain unmodified.

### INV-INST-13: Dry-Run Zero Mutation Guarantee for Instructions `[current]` {: #inv-inst-13 }

`aikito sync global --dry-run` and `aikito sync project --dry-run` guarantee zero filesystem mutations across home, checkout, workspace, and config files for instruction targets.

### INV-INST-14: Instruction Result Segmentation `[current]` {: #inv-inst-14 }

Instruction synchronization outcomes are isolated into structured execution results (`instruction_result`). Instruction failure or conflicts cannot invalidate or downgrade committed skill results, and subsequent memory failures cannot invalidate committed instruction results.

### INV-INST-15: `Project.prepare` Instruction Contract `[current]` {: #inv-inst-15 }

`Project.prepare(agent, path=None)` prepares project instructions using the unified instruction engine without expanding permissions. It does not accept `--force`, does not synchronize global instructions, and emits `ProjectPrepareConflictError` on instruction conflicts.

## State Transitions

### Global Instructions

Canonical source: `<workspace>/global/AGENTS.md` (mandatory; missing source causes preflight abort `INV-TR-02`).

| Current Target State | Availability | Planned Action | Rule ID | Handling |
| --- | --- | --- | --- | --- |
| `same_object` | Any | `SHARED_PATH` | `INV-INST-05` | Read-only disposition; excluded from Executor write set |
| Parent missing | All consumers `not_installed` | `SKIP` | `INV-INST-07` | Skips linking; does not create parent directory |
| Parent missing | Any consumer `installed` | `CREATE_PARENT` + `CREATE_LINK` | `INV-INST-07` | Creates parent directory and creates symlink |
| Parent missing | `unknown` | `SKIP` + Diagnostic | `INV-INST-07` | Refuses to guess installation; diagnostic finding emitted |
| Target missing, parent exists | `installed` / `unknown` | `CREATE_LINK` | `INV-INST-07` | Creates symlink pointing to canonical global instructions |
| Target correct symlink | Any | `NOOP` | `INV-INST-12` | No filesystem mutation; preserves mtime_ns and inode |
| Target wrong / external symlink | Any | `CONFLICT` | `INV-INST-06` | Breaking change: halts with conflict; automatic relinking eliminated |
| Target regular file | Any | `CONFLICT` | `INV-INST-03` | Rejects content matching; preserves regular file; reports conflict |
| Target directory / unsupported | Any | `CONFLICT` | `INV-INST-03` | Preserves existing entry; reports conflict |
| Broken symlink to canonical | Any | `CONFLICT` | `INV-INST-02` | Refuses to treat broken link as empty slot |
| Legacy Grok exact owned symlink | Any | `UNLINK` | `INV-INST-09` | Proves workspace ownership; safely unlinks legacy entry |
| Legacy Grok wrong / foreign entry | Any | `PRESERVE` | `INV-INST-09` | Foreign / unmanaged entry preserved |

### Project Instructions — Canonical Enabled

Canonical source: `<workspace>/projects/<project>/AGENTS.md` (non-empty; instructions enabled).

| Current Target State | Availability | Planned Action | Rule ID | Handling |
| --- | --- | --- | --- | --- |
| `same_object` | Any | `SHARED_PATH` | `INV-INST-05` | Read-only disposition; excluded from Executor write set |
| Target missing, parent exists | `installed` / `unknown` | `CREATE_LINK` | `INV-INST-07` | Creates symlink pointing to canonical project instructions |
| Target correct symlink | Any | `NOOP` | `INV-INST-12` | Stable link preserved without filesystem mutation |
| Target wrong project/workspace link | Any | `CONFLICT` | `INV-INST-02` | Refuses cross-project/workspace links; reports conflict |
| Target external symlink | Any | `CONFLICT` | `INV-INST-02` | Preserves external symlink; reports conflict |
| Target regular file | Any | `CONFLICT` | `INV-INST-03` | Project-owned instruction file preserved; reports conflict |
| Target directory / unsupported | Any | `CONFLICT` | `INV-INST-03` | Preserves existing entry; reports conflict |
| Parent missing | All `not_installed` | `SKIP` | `INV-INST-07` | Skips uninstalled agent target |
| Parent missing | Any `installed` | `CREATE_PARENT` + `CREATE_LINK` | `INV-INST-07` | Creates parent directory and creates symlink |
| Parent missing | `unknown` | `SKIP` + Diagnostic | `INV-INST-07` | Refuses to guess installation state |
| Legacy `.agents/AGENTS.md` exact owned | Any | `UNLINK` | `INV-INST-09` | Unlinks legacy exact symlink if not formally configured |
| Legacy `.agents/AGENTS.md` unmanaged | Any | `PRESERVE` | `INV-INST-09` | Foreign symlink / regular file preserved |

### Project Instructions — Canonical Empty

Canonical source: `<workspace>/projects/<project>/AGENTS.md` (empty; instructions disabled).

| Current Target State | Planned Action | Rule ID | Handling |
| --- | --- | --- | --- |
| Symlink exact to project canonical | `UNLINK` | `INV-INST-08` | Proves canonical ownership; unlinks link entry only |
| Broken symlink exact to canonical | `UNLINK` | `INV-INST-08` | Proves prior ownership; safely cleans up stale broken link |
| Symlink to other project / workspace | `PRESERVE` | `INV-INST-08` | Foreign link preserved; diagnostic finding emitted |
| External symlink | `PRESERVE` | `INV-INST-08` | Unmanaged link preserved untouched |
| Regular file (matching or differing) | `PRESERVE` | `INV-INST-10` | Project-owned file strictly preserved; diagnostic notice emitted |
| Directory / unsupported | `PRESERVE` | `INV-INST-08` | Unmanaged entry preserved untouched |
| Target missing | `NOOP` | `INV-INST-12` | Desired absent state already satisfied |
| `same_object` | `NO_ACTION` | `INV-INST-05` | Does not delete canonical file itself |
| Legacy `.agents/AGENTS.md` exact owned | `UNLINK` | `INV-INST-09` | Unlinks legacy exact symlink |
| Legacy `.agents/AGENTS.md` unmanaged | `PRESERVE` | `INV-INST-09` | Foreign symlink / regular file preserved |

---

