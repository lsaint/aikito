# Memory Runtime Invariants

## Core Rules
### INV-MEM-01: Memory Link-Only Contract `[current]` {: #inv-mem-01 }

Memory runtime visibility operates exclusively in link mode (`mode="link"`). There is no copy mode, no baseline fingerprint record ($B$), no content state store, and no directory copy lifecycle. Ownership and state transitions depend exclusively on live directory entry and symlink destination verification.

### INV-MEM-02: Workspace Memory Reference Identity `[current]` {: #inv-mem-02 }

Each reference in project `agent.toml` `memory = [...]` declares a relative workspace path. Canonical source is `<workspace>/memory/<reference>` and runtime target is `<checkout>/.agents/memory/<reference>`. References must be safe relative paths that do not escape the workspace memory root via traversal (`..`) or unsafe symlinks.

### INV-MEM-03: Project Notes Canonical Source Priority `[current]` {: #inv-mem-03 }

Project memory notes canonical source follows strict precedence: if `<workspace>/projects/<project>/memory` exists, the canonical notes source is `<workspace>/projects/<project>/memory/notes`; otherwise legacy fallback `<workspace>/memory/<project>/notes` is used. If `<workspace>/projects/<project>/memory` exists as a directory, the legacy fallback is completely disabled even if the `notes/` subdirectory is absent.

### INV-MEM-04: Project Notes Directory-Only Scoping `[current]` {: #inv-mem-04 }

Only the resolved `notes/` directory under canonical project memory root is automatically exposed to `<checkout>/.agents/memory/notes`. Non-notes files and directories within project memory are not automatically scanned or exposed to runtime.

### INV-MEM-05: Exact Canonical Symlink for Memory Ownership `[current]` {: #inv-mem-05 }

A memory runtime entry in `<checkout>/.agents/memory/` is owned if and only if it is a symbolic link whose literal target resolves to the exact canonical path of that specific memory resource. Symlinks pointing to other memory entries, other projects, or external paths evaluate to `FOREIGN` / `UNKNOWN` and cause `CONFLICT`. Matching file or directory contents never establishes ownership.

### INV-MEM-06: Deselected Memory Stale Entry Cleanup `[current]` {: #inv-mem-06 }

When a workspace memory reference is removed from `memory = [...]` or project notes become unavailable, a stale entry in `<checkout>/.agents/memory/` is planned for `UNLINK` if and only if it is a symlink proving exact ownership to an allowed historical canonical candidate for that specific entry name. Symlinks pointing to other memory items, external paths, and regular files/directories are strictly preserved and reported as conflicts/findings.

### INV-MEM-07: Memory Conflict and Preservation Guarantee `[current]` {: #inv-mem-07 }

Pre-existing regular files or directories, external symlinks, or cross-resource symlinks at memory runtime targets evaluate to `CONFLICT` and are strictly preserved. Aikito never overwrites, unlinks, or adopts regular files or unexpected symlinks for memory visibility.

In the unified link planning engine, any pre-existing unmanaged file, directory, or external symlink at `.agents/memory/<name>` evaluates to `CONFLICT`, strictly preserving the target and emitting a diagnostic finding.

### INV-MEM-08: Multi-Checkout and Offline Memory Scope `[current]` {: #inv-mem-08 }

In multi-checkout projects, active checkouts generate distinct physical memory targets frozen at Plan build time. Offline checkouts are planned as `OFFLINE` / `SKIP` with zero filesystem operations and without blocking active checkouts. Explicit checkout paths follow CAS validation.

### INV-MEM-09: Stale Plan Invalidation on Source or Target Mutation `[current]` {: #inv-mem-09 }

Any mutation to `agent.toml memory = [...]`, project memory existence (triggering source priority change), canonical source deletion/recreation, or target entry tampering after Plan creation invalidates the Plan. Execution halts without silent replanning.

### INV-MEM-10: Idempotent Convergence and Dry-Run Zero Mutation for Memory `[current]` {: #inv-mem-10 }

In a stable configuration where memory links accurately point to canonical sources, subsequent synchronizations evaluate completely to `NOOP`. Zero filesystem mutations occur: symlink destinations, `mtime_ns`, and inode numbers remain unmodified. `aikito sync project --dry-run` guarantees zero writes across checkouts, workspace, and config.

### INV-MEM-11: Memory Result Segmentation `[current]` {: #inv-mem-11 }

Project memory execution outcomes are partitioned into a dedicated `MemoryExecutionResult`. Upstream skill and instruction successes are never downgraded or invalidated by memory failures or conflicts, and overall success is the conjunction of all active segments.

### INV-MEM-12: `Project.prepare` Memory Contract `[current]` {: #inv-mem-12 }

`Project.prepare(agent, path=None)` synchronizes memory runtime visibility using the unified memory engine without expanding permissions. It does not accept `--force`, does not touch global resources, and emits `ProjectPrepareConflictError` when memory conflicts or missing sources occur.

---

## Summary State Table

The following table summarizes the planning outcome for each memory runtime entry based on INV-MEM-01 through INV-MEM-12. No new rule IDs are introduced.

| Target State | Planned Action |
| --- | --- |
| Missing target | `CREATE` |
| Exact canonical symlink | `NOOP` |
| Wrong memory link (cross-resource) | `CONFLICT` / preserve |
| External symlink | `CONFLICT` / preserve |
| Regular file or directory | `CONFLICT` / preserve |
| Exact stale owned symlink | `UNLINK` |
| Foreign stale entry | preserve + finding |
| Offline checkout | `SKIP` / `OFFLINE` |

## Project Notes Source Priority

Source selection follows strict precedence (INV-MEM-03):

| Condition | Canonical Notes Source |
| --- | --- |
| `projects/<P>/memory` exists | `projects/<P>/memory/notes` only; no fallback |
| `projects/<P>/memory` missing | `memory/<P>/notes` legacy fallback allowed |
