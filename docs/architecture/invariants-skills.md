# Skill Invariants

## Project Skills
### Transition Rules
The following transition table governs synchronization planning for project skills.

| Rule ID | Status | Selection & Mode | Target & Evidence | Classification & Default Action | Post-State or Handling | Fixture ID |
| --- | --- | --- | --- | --- | --- | --- |
| `INV-TR-01` | `[current]` | Selected, link / copy | Target missing; canonical valid; state missing or present | `MISSING` &rarr; `CREATE` | Creates link or copy. For copy mode, writes active record $B := C$ (origin `write`). Link mode creates symlink without state record. | `FIX-TR-01` |
| `INV-TR-02` | `[current]` | Selected, any | Canonical missing or unreadable | `BLOCK` &rarr; `CONFLICT` | Preflight halts execution without modifying target or state. Verified in `tests/test_skill_plan.py::test_canonical_missing_or_unreadable`. | `FIX-TR-02` |
| `INV-TR-03` | `[current]` | Selected, link | Symlink accurately points to canonical resource | `OWNED` &rarr; `NOOP` | Link already points to canonical resource; no filesystem mutation. Verified in `tests/test_skill_plan.py::test_link_mode_noop_when_owned`. | `FIX-TR-03` |
| `INV-TR-04` | `[current]` | Selected, link | Broken symlink pointing to canonical (source missing) | `OWNED` + source missing &rarr; `BLOCK` | Refuses to treat broken link as an empty slot. | `FIX-TR-04` |
| `INV-TR-05` | `[current]` | Selected, link | Points to external/wrong resource; state missing | `FOREIGN` / `UNKNOWN` &rarr; `CONFLICT` | Refuses automatic relink. Sync aborted. | `FIX-TR-05` |
| `INV-TR-06` | `[current]` | Selected, link | Previously managed, but link redirected elsewhere | `DRIFT` / `UNKNOWN` &rarr; `CONFLICT` | Stale record cannot override current directory entry fact. Verified in `tests/test_skill_plan.py`. | `FIX-TR-06` |
| `INV-TR-07` | `[current]` | Selected, copy | Valid active state, $R = B = C$ | `OWNED_UNCHANGED` &rarr; `NOOP` | No file or state writes. Verified in `tests/test_skill_plan.py::test_copy_mode_owned_unchanged_noop`. | `FIX-TR-07` |
| `INV-TR-08` | `[current]` | Selected, copy | Valid active state, $R = B$, $C$ changed | `OWNED_UNCHANGED` &rarr; `UPDATE` | Copies new $C$; updates baseline $B := C$ (origin `write`). Verified in `tests/test_skill_plan.py::test_copy_mode_upstream_updated`. | `FIX-TR-08` |
| `INV-TR-09` | `[current]` | Selected, copy | Valid active state, $R \ne B$ and $R \ne C$ | `OWNED_DRIFTED` &rarr; `CONFLICT` | Blocks sync. Explicit `--force` replaces target; updates $B := C$ (origin `write`). Verified in `tests/test_skill_plan.py::test_copy_mode_local_drift_conflict_and_force`. | `FIX-TR-09` |
| `INV-TR-10` | `[current]` | Selected, copy | Valid active state, $R \ne B$ but $R = C$ | `RECONCILE_STATE` &rarr; `NOOP` on files | Verifies existing ownership; atomically updates $B := C$ (origin `reconcile`). Verified in `tests/test_skill_plan.py::test_copy_mode_reconcile_state`. | `FIX-TR-10` |
| `INV-TR-11` | `[current]` | Selected, copy | No state record, $R = C$ | `UNKNOWN` &rarr; `NOOP` + Diagnostic | Does not manufacture ownership. `--force` authorizes `CLAIM_STATE` ($B := C$, origin `claim`). Verified in `tests/test_skill_plan.py::test_copy_mode_unmanaged_matching_requires_force`. | `FIX-TR-11` |
| `INV-TR-12` | `[current]` | Selected, copy | No state record, $R \ne C$, safe normal directory | `UNKNOWN` &rarr; `CONFLICT` (Default); `--force` Overwrites | Default blocks sync; `--force` creates active state $B := C$ (origin `write`). Verified in `tests/test_skill_plan.py::test_copy_mode_unmanaged_conflicting_requires_force`. | `FIX-TR-12` |
| `INV-TR-13` | `[current]` | Selected, copy | State corrupt/mismatched, unreadable, or unsafe entry | `UNKNOWN` &rarr; `CONFLICT` | Checks directory readability, entry type, and state corruption; `--force` cannot bypass corruption. Verified in `tests/test_skill_plan.py::test_copy_mode_corrupted_state_causes_conflict`. | `FIX-TR-13` |
| `INV-TR-14` | `[current]` | Deselected, actual link | Symlink points to workspace canonical resource | `OWNED` &rarr; `UNLINK` | Unlinks symlink entry only. | `FIX-TR-14` |
| `INV-TR-15` | `[current]` | Deselected, actual link | External symlink or insufficient evidence | `FOREIGN` / `UNKNOWN` &rarr; Preserve + Info | Leaves link untouched; informs user. | `FIX-TR-15` |
| `INV-TR-16` | `[current]` | Deselected, actual copy | Valid active state, any $R$ / $B$ / $C$ relation | `DEACTIVATE_STATE` &rarr; Preserve target | Sets state to `inactive`; target directory preserved untouched. Verified in `tests/test_skill_plan.py::test_deselected_copy_skill_deactivates_state_and_preserves_dir`. | `FIX-TR-16` |
| `INV-TR-17` | `[current]` | Deselected, actual copy | Inactive state or no state record | Preserve target &rarr; `NOOP` | Never deletes copy. Does not alter state. | `FIX-TR-17` |
| `INV-TR-18` | `[current]` | Re-selected, copy | Inactive state, $R = C$ | `UNKNOWN` &rarr; `NOOP` + Diagnostic | `--force` authorizes `REACTIVATE_STATE` ($B := C$, origin `reactivate`). Verified in `tests/test_skill_plan.py::test_reselected_inactive_copy_matches_canonical`. | `FIX-TR-18` |
| `INV-TR-19` | `[current]` | Re-selected, copy | Inactive state, $R \ne C$ | `UNKNOWN` &rarr; `CONFLICT` | `--force` authorizes replacement and active baseline $B := C$ (origin `write`). Verified in `tests/test_skill_plan.py::test_reselected_inactive_copy_differs_requires_force`. | `FIX-TR-19` |
| `INV-TR-20` | `[current]` | Selected, mode switch | Representation differs from desired mode | Mode switch plan | Link to copy: managed link replaced by copy ($B := C$, active). Copy to link: active copy with $R = B$ replaced by symlink (state inactive). Drifted, unmanaged, or inactive copy to link is blocked (`CONFLICT`); `--force` does not bypass. Verified in `tests/test_skill_plan.py::test_mode_switch_link_to_copy`, `tests/test_skill_plan.py::test_mode_switch_copy_to_link`. | `FIX-TR-20` |

### Detailed Lifecycle Rules
#### Project Skills State Transitions
The table above governs synchronization planning for project skills.

## Global Skills
### Rules
### INV-GLB-01: Global Skill Link-Only Contract `[current]` {: #inv-glb-01 }

Global skills operate exclusively in link mode. There is no copy mode, no baseline fingerprint record ($B$), no content state store, and no directory copy lifecycle. Ownership and state transitions are determined entirely from live filesystem directory entries and symlink destination verification.

### INV-GLB-02: Exact Canonical Symlink for Managed Entries `[current]` {: #inv-glb-02 }

A managed skill entry (`~/.agents/skills/<name>`) is owned if and only if it is a symbolic link whose literal target resolves to `<active_workspace>/skills/<name>`. Symlinks pointing to other skills in the workspace, other workspaces, or external locations evaluate to `FOREIGN` / `UNKNOWN` and cause `CONFLICT`. Normal directories (even if content exactly matches canonical) and regular files evaluate to `CONFLICT`.

### INV-GLB-03: Stale Entry Cleanup Invariant `[current]` {: #inv-glb-03 }

A deselected entry in `~/.agents/skills/` is unlinked if and only if it is a symbolic link pointing specifically to `<active_workspace>/skills/<same-name>` (or a broken symlink whose raw link target points to that canonical path). Unmanaged symlinks, external links, cross-skill links, and ordinary directories are strictly preserved and reported as conflicts. Content-matching copy cleanup (`allow_matching_copies=True`) is eliminated for global skills.

### INV-GLB-04: Managed Container Preservation and Migration `[current]` {: #inv-glb-04 }

`~/.agents/skills` is a managed container hosting individual managed entries; it is never deleted when skills become empty. If `~/.agents/skills` is a legacy symlink pointing specifically to the root of `<active_workspace>/skills`, it is safely migrated to a real directory (`MIGRATE_CONTAINER`) prior to entry writes. Symlinks pointing to subdirectories, other workspaces, or external targets evaluate to `CONFLICT`.

### INV-GLB-05: Consumer Link Non-Relink Invariant (Breaking Change) `[current]` {: #inv-glb-05 }

Agent consumer skill targets (e.g. `~/.claude/skills`) must resolve to the managed container `~/.agents/skills`. If a consumer target exists as a symlink pointing to an unexpected destination or external path, Aikito halts with `CONFLICT` and preserves the target; automatic unlinking and relinking (`[RELINK]`) is eliminated.

### INV-GLB-06: Same-Object Disposition (`SHARED_PATH`) `[current]` {: #inv-glb-06 }

When an Agent consumer target path resolves to the same physical object as the managed container (`Target.is_same_object`), the target is assigned the read-only disposition `SHARED_PATH`. It does not enter the Executor write operations set and creates no filesystem mutation.

### INV-GLB-07: Shared Consumer Target Deduplication `[current]` {: #inv-glb-07 }

Multiple Agent consumers pointing to identical physical targets (e.g. 6 bundled agents sharing `~/.agents/skills`) are deduplicated into a single physical `Target`. The target is inspected, planned, and applied exactly once per synchronization run.

### INV-GLB-08: Idempotent Convergence `[current]` {: #inv-glb-08 }

In a stable configuration where all managed entries and consumer links point to their canonical targets, subsequent synchronization runs evaluate completely to `NOOP` or `SHARED_PATH`. Zero filesystem mutations occur, preserving file metadata, symlink destination, `mtime_ns`, and inode numbers without unlink/recreate cycles.

### INV-GLB-09: Dry-Run Zero Mutation Guarantee `[current]` {: #inv-glb-09 }

`aikito sync global --dry-run` guarantees zero filesystem writes across `$HOME`, workspace, state storage, and temporary directories. No `writer.lock` is created.

### INV-GLB-10: Bundled Refresh and Runtime Apply Serialization `[current]` {: #inv-glb-10 }

Bundled skill refresh and global skill runtime link application share the outermost `WorkspaceWriterLock(home)`. Refresh outcomes must match plan expectations before runtime application proceeds; preflight failure halts execution without silent replanning.

### INV-GLB-11: Global Result Segmentation `[current]` {: #inv-glb-11 }
 
Global synchronization results are strictly segmented. Failure during subsequent global instructions linking cannot corrupt, downgrade, or rewrite the recorded success of applied global skills.

### State Transitions
#### Selected Managed Entry
| Current State | Planned Action | Rule ID / Evidence | Handling |
| --- | --- | --- | --- |
| Canonical missing or unreadable | `CONFLICT` | `INV-GLB-02` | Preflight halts execution; target untouched |
| Target missing | `CREATE` | `INV-GLB-02` | Creates symlink pointing to canonical `<workspace>/skills/<name>` |
| Target symlink exact to canonical | `NOOP` | `INV-GLB-08` | No filesystem mutation, preserves mtime_ns and inode |
| Target symlink points to other skill in workspace | `CONFLICT` | `INV-GLB-02` | Preserved untouched; reports unmanaged conflict |
| Target symlink points to other workspace | `CONFLICT` | `INV-BIND-02` | Preserved untouched; no automatic takeover |
| Target symlink points to external path | `CONFLICT` | `INV-GLB-02` | Preserved untouched; reports unmanaged conflict |
| Target is normal directory (identical or differing content) | `CONFLICT` | `INV-GLB-01` | Matching content rejected as ownership evidence; preserved |
| Target is regular file or unsupported entry | `CONFLICT` | `INV-GLB-02` | Refuses to overwrite; reports conflict |

#### Deselected Stale Managed Entry (`~/.agents/skills/<name>`)

| Current State | Planned Action | Rule ID / Evidence | Handling |
| --- | --- | --- | --- |
| Symlink exact to `<workspace>/skills/<same-name>` | `UNLINK` | `INV-GLB-03` | Proves workspace ownership; unlinks link entry only |
| Broken symlink, raw target exact to canonical | `UNLINK` | `INV-GLB-03` | Proves prior ownership; unlinks stale broken link entry |
| Symlink points to another skill | `PRESERVE` | `INV-GLB-03` | Preserves cross-skill link untouched |
| Symlink points to other workspace / external | `PRESERVE` | `INV-BIND-02` | Preserves foreign link untouched |
| Normal directory (identical or differing content) | `PRESERVE` | `INV-GLB-03` | Content matching eliminated; directory preserved |
| Regular file or unsupported entry | `PRESERVE` | `INV-GLB-03` | Preserves unmanaged entry untouched |

#### Consumer Link (`<agent>/skills`)

| Current State | Availability | Planned Action | Rule ID | Handling |
| --- | --- | --- | --- | --- |
| `same_object` | Any | `SHARED_PATH` | `INV-GLB-06` | Read-only disposition; excluded from Executor write operations |
| Parent missing | All consumers `not_installed` | `SKIP` | `INV-GLB-05` | Skips linking; does not create parent directory |
| Parent missing | Any consumer `installed` | `CREATE_PARENT` + `CREATE_LINK` | `INV-GLB-05` | Creates parent directory and creates symlink to `~/.agents/skills` |
| Parent missing | `unknown` | `SKIP` + Diagnostic | `INV-GLB-05` | Refuses to guess installation; diagnostic finding emitted |
| Target missing, parent exists | `installed` / `unknown` | `CREATE_LINK` | `INV-GLB-05` | Creates symlink pointing to `~/.agents/skills` |
| Target correct symlink to `~/.agents/skills` | Any | `NOOP` | `INV-GLB-08` | No filesystem mutation, preserves mtime_ns and inode |
| Target wrong / external symlink | Any | `CONFLICT` | `INV-GLB-05` | Breaking change: halts with conflict; automatic relinking eliminated |
| Target regular file / directory | Any | `CONFLICT` | `INV-GLB-05` | Preserves existing entry; reports conflict |

#### Legacy Top-Level Container Migration (`~/.agents/skills`)

| Current State | Planned Action | Rule ID | Handling |
| --- | --- | --- | --- |
| Exact symlink to `<workspace>/skills` root | `MIGRATE_CONTAINER` | `INV-GLB-04` | Safely unlinks legacy link and creates real directory before entry writes |
| Symlink to subfolder of `skills/` | `CONFLICT` | `INV-GLB-04` | Ambiguous / invalid container target; sync aborted |
| Symlink to other workspace | `CONFLICT` | `INV-BIND-02` | Foreign workspace container; preserved; sync aborted |
| Symlink to external path | `CONFLICT` | `INV-GLB-04` | External path container; preserved; sync aborted |

