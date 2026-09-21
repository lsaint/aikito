# Engineering Invariants

Internal engineering contract governing ownership evidence, state transitions,
authorization scopes, transactional boundaries, and API guarantees across Aikito.

This document serves as the implementation and testing specification for Aikito
developers. For user-facing safety documentation, see [Safety Model](../safety.md).

---

## 1. Conventions and Status Annotations

Every invariant is identified by a permanent rule ID. Status tags denote current
implementation status versus planned future phases:

- `[current]`: Behavior verified by regression tests and currently active in the codebase.
- `[planned]`: Target contract for Plan / Executor / State Store implementation.
- `[compat-gap]`: Discrepancy between current implementation heuristic and target safety model, documented for alignment.

Fingerprint notation:

- $C$: Content fingerprint of the canonical resource in the workspace (`skills/<name>/`).
- $R$: Content fingerprint of the runtime resource in the checkout (`.agents/skills/<name>/`).
- $B$: Baseline fingerprint recorded in the active copy management record at last successful sync.

---

## 2. Ownership Dimensions and Evidence Hierarchy

### INV-OWN-01: Dimensional Independence `[current]` {: #inv-own-01 }

Existence, ownership, desired content matching, and selection state are orthogonal dimensions:

- An entry may be `OWNED` without being eligible for deletion (e.g. deselected copy skill).
- An entry may match desired content ($R = C$) without establishing ownership (e.g. unmanaged pre-existing directory).
- An operation returning `NOOP` does not imply ownership was established or recorded.

### INV-OWN-02: Evidence Evaluation Order `[current]` {: #inv-own-02 }

Evidence must be evaluated in strict priority order. Path traversal failures, escaping symlinks, or conflicting evidence cannot be superseded by matching content:

1. **Target Directory Entry Verification**: Verify workspace, project binding, resource identity, target path, and directory entry type using `lstat` and `readlink` semantics. Broken symlinks must be identified as symlink entries *before* resolving target paths. Deletion must never follow the target of a symlink.
2. **Symlink Target Inspection**: For symlinks, evaluate the literal link target. A symlink constitutes ownership evidence if and only if it resolves into the canonical path of this resource in the active workspace. A symlink pointing to arbitrary workspace locations or external paths is unmanaged (`FOREIGN`/`UNKNOWN`). Deletion acts strictly on the link entry, never on referenced content.
3. **Active Copy Management Record**: For directory copies, inspect valid active management records. A valid record requires:
   - Matching workspace identity and project binding;
   - Matching physical checkout path and resource identity;
   - Matching normalized target entry path and representation type (`copy`);
   - Baseline fingerprint $B$ recorded at last successful sync.
   Records from other checkouts, other resources, or other tools cannot be reused.
4. **Fingerprint Comparison**: Compare current runtime fingerprint $R$ against baseline $B$ to detect local drift. Compare runtime fingerprint $R$ against canonical fingerprint $C$ to detect upstream updates. Baseline origin must distinguish recorded write success from explicit state reconciliation.
5. **Rejection of Pseudo-Ownership**: Identical directory contents, matching directory names, or inclusion in project `skills` configuration never establish ownership on their own.

### INV-OWN-03: Symlink Ownership Semantics `[current]` {: #inv-own-03 }

- **Target Model (`[current]`)**: A symlink entry is owned if and only if its literal target resolves to the canonical path of *this specific resource* (`canonical_root / resource_name`). A symlink pointing to an arbitrary workspace location or a different workspace skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`). Deletion acts strictly on the link entry, never on referenced content.
- **Intentional Tightening**: Previously, `_symlink_points_within()` evaluated root-level parent directories, allowing any symlink resolving inside `workspace/skills` (even pointing to another skill) to be unlinked upon deselection. This has been corrected to exact canonical targets (`canonical_root / resource_name`). Deselected links pointing to other workspace resources are preserved rather than removed, and reported as unmanaged/conflicts.
- **Broken Links**: A broken symlink is owned if and only if its literal target points specifically to the canonical path of *this specific resource* (`canonical_root / resource_name`), retaining proof of prior Aikito management and rendering it eligible for cleanup upon deselection. A broken symlink pointing to an arbitrary workspace location or another skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`).
- **Inspection Safety**: Canonical roots and intermediate parent directories must be real directories, not symlinks, junctions, or reparse points escaping the trusted workspace boundary. Symlinks inside copy sources or targets are rejected.

### INV-OWN-04: Copy Lifecycle and State Record Invariants `[current]` {: #inv-own-04 }

Copy management records follow an explicit two-state lifecycle (`active` vs. `inactive`):

- **`active`**: The copy was successfully synchronized by Aikito and is currently selected. Eligible for automated updates when $R = B$. Baseline fingerprint $B$ reflects the last successful write or verified convergence, recording an origin of `write`, `reconcile`, `claim`, or `reactivate`.
- **`inactive`**: The copy was previously synchronized but is currently deselected. Historical baseline $B$ is retained solely for origin auditing and does not grant automated update or deletion rights.
- **Link Separation**: Symlink ownership is derived solely from live directory entry and symlink target facts; symlinks do not create or maintain active copy management records.
- **Deselection Protection**: Deselected copy skills are **never deleted or unlinked** by Aikito. Deselection marks the management record `inactive` and leaves target files completely untouched.
- **Corrupt / Missing Record**: Any corrupt, mismatched, or unreadable management record reverts to `UNKNOWN`. Aikito halts destructive actions and refuses automated updates; `--force` cannot bypass record corruption.

### INV-OWN-05: Directory Fingerprint Specification `[current]` {: #inv-own-05 }

Directory fingerprints must capture the complete directory tree: file relative paths, file types (regular file, symlink, directory), byte contents, empty directories, and POSIX executable permission bits. File timestamps (`mtime`) and filesystem inode numbers are excluded from content equality comparisons, but may be used during preflight to detect concurrent entry replacement.

Any unsupported filesystem entry (FIFO, socket, device node) or internal symlink inside a copy source or target causes fingerprinting/inspection to fail and blocks synchronization.

### INV-BIND-01: Global Binding Identity Definition `[current]` {: #inv-bind-01 }

Global resources lack project checkouts and reside in host-level paths (e.g. `~/.agents/`, `~/.claude/`). Their binding identity tuple is defined as:

```text
(workspace_identity, resource_identity, normalized_target_path)
```

For consumer links, the binding also associates the set of consumer agent platforms. This replaces project-scoped `(workspace, project, physical_checkout)` keys without introducing duplicate metadata stores.

### INV-BIND-02: Multi-Workspace Global Ownership and Conflict Resolution `[current]` {: #inv-bind-02 }

When multiple independent Aikito workspaces configure the same host agent target (e.g. `~/.agents/skills/`), ownership cannot be assumed by the active workspace unless the target link explicitly resolves to canonical resources within the active workspace. Targets pointing to other workspaces or external locations evaluate to `UNKNOWN` / `FOREIGN`. Aikito preserves these entries, emits diagnostic findings, and refuses automatic takeover or deletion.

### INV-BIND-03: Scope Reduction for Link-Only Resources `[current]` {: #inv-bind-03 }

Global skills and instructions operate strictly in `link` mode (`mode="link"`); copy mode is intentionally not supported. Therefore, global resources require NO content baseline records ($B$), NO directory fingerprint tracking in state documents, and NO copy reconciliation lifecycle. Their ownership and state transitions depend exclusively on live filesystem directory entries and symlink destination verification ([INV-OWN-02](#inv-own-02), [INV-OWN-03](#inv-own-03)).

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

Bundled skill refresh and global skill runtime link application share the outermost `SkillWriterLock(home)`. Refresh outcomes must match plan expectations before runtime application proceeds; preflight failure halts execution without silent replanning.

### INV-GLB-11: Global Result Segmentation `[current]` {: #inv-glb-11 }

Global synchronization results are strictly segmented. Failure during subsequent legacy global instructions linking cannot corrupt, downgrade, or rewrite the recorded success of applied global skills.

### INV-INST-01: Instruction Link-Only Contract `[planned]` {: #inv-inst-01 }

Instructions (both global and project scoped) operate exclusively in link mode (`mode="link"`). There is no copy mode, no baseline fingerprint record ($B$), no content state store, and no directory copy lifecycle. Ownership is derived exclusively from the live directory entry and canonical target verification.

### INV-INST-02: Exact Canonical Destination for Instruction Symlinks `[planned]` {: #inv-inst-02 }

An instruction symlink target (global or project) is owned by Aikito if and only if its literal target resolves to the exact canonical instruction file (`<workspace>/global/AGENTS.md` or `<workspace>/projects/<project>/AGENTS.md`). Symlinks pointing to other workspaces, other projects, or external paths evaluate to `FOREIGN` / `UNKNOWN` and cause `CONFLICT`.

### INV-INST-03: Rejection of Content-Matching Pseudo-Ownership `[planned]` {: #inv-inst-03 }

Pre-existing regular files or directories at instruction targets evaluate to `CONFLICT` and are strictly preserved, even if their byte content matches canonical instructions. Aikito never overwrites, adopts, or unlinks regular files based on matching content.

### INV-INST-04: Shared Instruction Target Deduplication `[planned]` {: #inv-inst-04 }

Multiple Agent platforms specifying identical instruction target paths within a scope (e.g. multiple agents referencing `AGENTS.md` in a checkout or host path) are deduplicated into a single physical `Target`. The target is inspected, planned, and executed exactly once per synchronization run.

### INV-INST-05: Same-Object Disposition (`SHARED_PATH`) `[planned]` {: #inv-inst-05 }

When an Agent instruction target resolves to the same physical object as canonical instructions (`Target.is_same_object`), it receives read-only disposition `SHARED_PATH`. It is excluded from Executor write operations and creates no filesystem mutations.

### INV-INST-06: Global Instruction Symlink Conflict Protection (Breaking Change) `[planned]` {: #inv-inst-06 }

If a global instruction target exists as a symlink pointing to an unexpected destination or external path, Aikito halts with `CONFLICT` and preserves the target. Automatic unlinking and relinking (`[RELINK]`) is eliminated.

### INV-INST-07: Project Instruction Enabled State Transitions `[planned]` {: #inv-inst-07 }

When project canonical `AGENTS.md` is non-empty, instructions are enabled. Missing targets transition to `CREATE`, exact symlinks to `NOOP`, wrong/external symlinks or regular files to `CONFLICT`.

### INV-INST-08: Project Empty Canonical Owned Link Cleanup `[planned]` {: #inv-inst-08 }

When project canonical `AGENTS.md` is empty, instructions are disabled. Aikito plans `UNLINK` only for symlinks that prove exact ownership to the project's canonical `AGENTS.md` (including broken symlinks pointing to it). All foreign symlinks, unmanaged links, regular files, and directories are strictly preserved.

### INV-INST-09: Legacy Instruction Stale Entry Cleanup `[planned]` {: #inv-inst-09 }

Legacy paths (such as `~/.grok/AGENTS.md` or `<checkout>/.agents/AGENTS.md`) are planned for `UNLINK` if and only if they are symlinks pointing specifically to current canonical instructions. If currently configured by an Agent in `agents.toml`, they are treated as formal targets and not stale cleanup.

### INV-INST-10: Project-Owned File Preservation Under Empty Canonical `[planned]` {: #inv-inst-10 }

If a project checkout contains a regular file at the instruction target when canonical is empty, the file is strictly preserved (`PRESERVE`) and a diagnostic notice is emitted. Aikito never deletes project-owned instruction files.

### INV-INST-11: Multi-Checkout and Offline Instruction Scope `[planned]` {: #inv-inst-11 }

In multi-checkout projects, active checkouts generate distinct physical instruction targets frozen at Plan build time. Offline candidate paths are planned as `OFFLINE` / `SKIP` and generate zero filesystem operations. Explicit checkout paths follow CAS validation.

### INV-INST-12: Idempotent Convergence for Instructions `[planned]` {: #inv-inst-12 }

In a stable configuration where all instruction links point to their canonical targets (or are cleanly absent for empty canonicals), subsequent synchronizations evaluate completely to `NOOP` or `SHARED_PATH`. Symlink destinations, `mtime_ns`, and inode numbers remain unmodified.

### INV-INST-13: Dry-Run Zero Mutation Guarantee for Instructions `[planned]` {: #inv-inst-13 }

`aikito sync global --dry-run` and `aikito sync project --dry-run` guarantee zero filesystem mutations across home, checkout, workspace, and config files for instruction targets.

### INV-INST-14: Instruction Result Segmentation `[planned]` {: #inv-inst-14 }

Instruction synchronization outcomes are isolated into structured execution results (`instruction_result`). Instruction failure or conflicts cannot invalidate or downgrade committed skill results, and subsequent memory failures cannot invalidate committed instruction results.

### INV-INST-15: `Project.prepare` Instruction Contract `[planned]` {: #inv-inst-15 }

`Project.prepare(agent, path=None)` prepares project instructions using the unified instruction engine without expanding permissions. It does not accept `--force`, does not synchronize global instructions, and emits `ProjectPrepareConflictError` on instruction conflicts.

---

## 3. State Transition Table

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

### 3.1 Project Skills State Transitions

The table above governs synchronization planning for project skills.

### 3.2 Global Skills State Transitions

The following tables govern synchronization planning for global skills across managed entries, consumer links, and legacy containers.

#### Selected Managed Entry (`~/.agents/skills/<name>`)

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

### 3.3 Instruction State Transitions

The following tables govern synchronization planning for global and project instructions.

#### Global Instructions Target Table

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

#### Project Instructions Target Table (Canonical Non-Empty)

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

#### Project Instructions Target Table (Canonical Empty)

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

## 4. `--force` Authorization Contract

### INV-AUTH-01: Project Skill Force Semantics `[current]` {: #inv-auth-01 }

The `--force` option in `aikito sync project <name> [project_path] --force` authorizes:
1. Overwriting drifted copies where valid active state exists ($R \ne B$).
2. Overwriting inactive copies or unmanaged normal skill directories ($R \ne C$).
3. Explicitly claiming (`CLAIM_STATE`) or reactivating (`REACTIVATE_STATE`) management when $R = C$.

### INV-AUTH-02: Scoping Boundaries `[current]` {: #inv-auth-02 }

Every selected skill generates an independent authorization item.
- **Explicit Checkout Path**: When `project_path` is passed, `--force` authorization applies strictly to that physical checkout.
- **Omitted Path**: When `project_path` is omitted, `--force` applies only to currently accessible active checkouts on this host. Offline or missing candidate paths are never authorized.
- **Granular Authorization**: Every selected skill generates an independent authorization item. Authorization does not cover arbitrary subdirectories or sibling files.

### INV-AUTH-03: Authorization Target Binding `[current]` {: #inv-auth-03 }

Each authorization item binds seven attributes (`format_authorization_token` in `src/aikito/skill_plan.py`):
```text
plan identity / lifetime
operation kind
workspace + project + physical checkout + resource identity
normalized target entry path
expected current fingerprint + entry type + link identity
desired representation + source/desired fingerprint
expected management record version + lifecycle state
```
Target directory inspection evaluates the immediate directory entry, not the destination of any secondary symlink. Parent directory path sanity is validated against directory traversal and symlink escapes.

### INV-AUTH-04: Authorization Invalidation and Explicit CAS `[current]` {: #inv-auth-04 }

Any divergence in source content, target directory entry, project selection, or checkout path invalidates the plan and all associated authorizations. An invalid plan halts execution; authorizations are not automatically transferred to a regenerated plan. A dry-run displays planned authorizations but persists nothing.

**Explicit Candidate Path CAS Contract**: When an explicit project path is passed to `aikito sync project <name> <path>`, path registration executes as a deterministic Compare-And-Swap (CAS) step within the Plan. The Plan binds exact pre-image bytes/hash and post-image bytes/hash of `agent.toml`. Before writing, Aikito verifies that `agent.toml` matches the expected pre-image; if external modifications occurred, the Plan halts. After successful atomic CAS write, only the resulting post-image is accepted as the authorized configuration baseline for runtime synchronization.

### INV-AUTH-05: Non-Bypassable Boundaries `[current]` {: #inv-auth-05 }

`--force` does **not** authorize:
- Deleting deselected copy skills;
- Reconnecting external or unmanaged symlinks;
- Converting copy to link by deleting drifted or uninspected directories;
- Overwriting instructions (`AGENTS.md`) or memory files;
- Deleting canonical resources;
- Bypassing syntax or schema errors in configuration files.

### INV-AUTH-06: Inventory of `--force` Across Aikito Commands `[current]` {: #inv-auth-06 }

The `--force` flag is scoped per command; boolean parameters do not constitute engine-wide global authorizations. The table below catalogs all existing commands that accept `--force`:

| Command | `--force` Syntax | Effect | Guardrails / Invariants |
| --- | --- | --- | --- |
| `aikito init workspace --force` | `--force` (flag) | Overwrites template files if they already exist in recognized workspace. | Refuses non-workspace directories, CLI source checkout (`~/aikito-src`), or unrecognized paths. |
| `aikito add skill <name> --from <path> --force` | `--force` (flag) | Overwrites existing canonical skill in workspace `skills/<name>/`. | Requires `--from <path>`; refuses if `--from` is omitted. |
| `aikito add subagent <name> --from <path> --force` | `--force` (flag) | Overwrites existing canonical subagent instructions in workspace `subagents/<name>.md` and updates `subagents.toml`. | Requires `--from <path>`; refuses if `--from` is omitted. |
| `aikito add mcp <name> --force` | `--force` (flag) | Overwrites existing canonical MCP config in workspace `mcps/<name>.toml`. | Requires `--from` or server definition parameters. |
| `aikito rm skill <name> --force` | `--force` (flag) | Forces global deletion of canonical skill by automatically unregistering it from all referencing projects. | Without `--force`, refuses deletion if any project references the skill. `rm skill --project <P>` unregisters without `--force`. |
| `aikito rm mcp <name> [--sync] --force` | `--force` (flag) | When used with `--sync`, forces removal from target agent configuration files even if config was modified outside Aikito. Without `--sync`, `--force` has no effect on agent runtimes (only deletes canonical `mcps/<name>.toml`). | Requires `--sync` to affect agent configs; purges server blocks despite external modification. |
| `aikito sync project <name> [path] --force` | `--force` (flag) | Overwrites drifted copied project skills in `.agents/skills/`. | Bound strictly to project skills; never deletes deselected copies or overwrites `AGENTS.md`. |
| `aikito sync mcp --force` | `--force` (flag) | Replaces conflicting managed MCP entries in agent configs after review. | Restricted to managed MCP server blocks; updates `mcp-state.json`. |
| `aikito sync subagents --force [targets...]` | `--force [targets...]` (`nargs="*"`) | Overwrites specific conflict targets (e.g. `--force claude-code/verifier`). | Requires specific `<agent>/<subagent>` target syntax. |
| `aikito version [-c\|--check] [--force]` | `--force` (flag) | Bypasses local update check cache and queries remote GitHub releases. | Network only; no filesystem mutation. |

**Commands with NO `--force` option:**
- `aikito init project`: Has no `--force` parameter. Project name binding is immutable once registered.
- `aikito sync global`: Has no `--force` parameter. Only supports `--dry-run`.
- `aikito sync` (workspace sync): Has no `--force` parameter. Only supports `--dry-run` and `--verbose`.
- `aikito rm subagent`: Has no `--force` parameter. Supports `name` and optional `--sync`.
- `aikito rm memory` / `aikito rm inbox`: Have no `--force` parameter.

---

## 5. Transactional Execution and Recovery Invariants

### INV-TX-01: Selection Transaction Boundary `[current]` {: #inv-tx-01 }

A selection mutation (e.g. `add skill --project`, `rm skill --project`, or `Project.prepare` with selection changes) executes inside an isolated transaction governed by `execute_selection_transaction()`. Participating files include project configuration (`agent.toml`), canonical skills (if affected), checkout skill runtimes (`.agents/skills/*`), and project skill state documents (`.aikito/state/project-skills/`).
Verified by: `tests/test_skill_runtime.py::test_selection_transaction_success_and_rollback`.

### INV-TX-02: Pre-Image and Post-Image CAS Verification `[current]` {: #inv-tx-02 }

Every mutated file within a selection transaction records exact pre-image bytes/hash and expected post-image bytes/hash. Before applying mutations, Aikito verifies that current disk state matches the pre-image. Any divergence aborts the transaction before committing. On rollback, Aikito only reverts files if current bytes still match post-image or expected interim state, preventing clobbering concurrent modifications.
Verified by: `tests/test_skill_runtime.py::test_selection_transaction_pre_image_cas_mismatch`, `tests/test_skill_runtime.py::test_selection_transaction_concurrent_modification_avoids_clobber_on_rollback`.

### INV-TX-03: Two-Phase Commit Marker `[current]` {: #inv-tx-03 }

Transactions follow a strict two-phase commit protocol recorded in the persistent journal (`phase="pending"` vs. `phase="committed"`). A transaction is only considered committed once the journal's `phase` field is atomically updated to `"committed"` via temporary file rename. If a process terminates prior to the commit marker, the transaction is strictly rolled back to pre-images during recovery; once marked committed, recovery rolls forward or cleans up staging/temporary artifacts.
Verified by: `tests/test_skill_state.py::test_pending_transaction_rolled_back`, `tests/test_skill_state.py::test_committed_transaction_finalized`.

### INV-TX-04: Rollback and Recovery Required Flag `[current]` {: #inv-tx-04 }

If any operation within a selection transaction fails before completion, the transaction automatically rolls back all applied file writes, directory changes, and state transitions. If an unrecoverable failure or external corruption prevents clean rollback, the journal remains on disk and the execution result sets `recovery_required=True`, halting subsequent mutating operations until resolved.
Verified by: `tests/test_skill_runtime.py::test_execute_selection_transaction_committed_cleanup_failure_retains_journal`, `tests/test_skill_state.py::test_recovery_aborts_and_retains_journal_if_state_externally_modified`.

### INV-PEND-01: Journal Storage Location and Safe Identifiers `[current]` {: #inv-pend-01 }

Transaction journals are stored under `.aikito/state/project-skills/transactions/<tx_id>/journal.json` relative to the state root (`$HOME` or specified `home`). Each transaction is assigned a cryptographically random, safe alphanumeric/dash/underscore identifier validated by `_is_safe_tx_id()`. Journal storage is strictly per-host and local; journals are never tracked in Git or exported to user workspaces.
Verified by: `tests/test_skill_state.py::test_save_and_load_roundtrip`, `tests/test_skill_state.py::test_forged_journal_rejected`.

### INV-PEND-02: Trusted Boundaries and Path Derivation `[current]` {: #inv-pend-02 }

Journals must strictly reference paths within trusted boundaries: workspace root, authorized checkouts, and runtime staging directories (`.aikito-tx/<tx_id>`). Any journal attempting path traversal, symlink/reparse point escaping, or arbitrary filesystem mutation outside authorized roots is rejected as forged/corrupt (`_is_valid_file_path`, `_is_valid_target_path`, `_is_valid_staging_or_recovery_dir`).
Verified by: `tests/test_skill_state.py::test_forged_journal_rejected`, `tests/test_skill_state.py::test_forged_journal_files_cannot_authorize_checkout`.

### INV-PEND-03: Journal File and Directory Permissions `[current]` {: #inv-pend-03 }

Transaction directories and journal files are created with restricted POSIX permissions (`0700` for directories, `0600` for journal files) via `secure_directory_permissions` and `secure_file_permissions` (or equivalent restricted ACLs on Windows). Journals must be real regular files; symlinks, junctions, or reparse points as journal paths cause immediate abort.
Verified by: `tests/test_skill_state.py::test_forged_journal_rejected`.

### INV-REC-01: Recovery Pass Trigger Order and Exclusivity `[current]` {: #inv-rec-01 }

Whenever a mutating entrypoint (`Project.prepare`, `sync_project_path`, `execute_selection_transaction`, `cmd_project_sync`) acquires the writer lock, it executes `run_recovery_pass()` before executing any planned operation. Recovery scans all transaction journals affecting the active workspace, projects, or checkouts.
Verified by: `tests/test_skill_runtime.py::test_selection_transaction_stops_after_recovery`, `tests/test_skill_state.py::test_pending_transaction_rolled_back`.

### INV-REC-02: Halting Request Upon Recovery `[current]` {: #inv-rec-02 }

If `run_recovery_pass()` performs any state rollback or cleanup (`recovery_occurred=True`), it stops the current mutating request immediately (`recovery_required=True` / abort). The current request does not proceed with stale planning; the user or caller must re-issue the command with fresh state inspection.
Verified by: `tests/test_skill_runtime.py::test_selection_transaction_stops_after_recovery`.

### INV-REC-03: Read-Only Operations Exemption `[current]` {: #inv-rec-03 }

Purely read-only inspection operations (`classify_project_skill_state`, `plan_project_skills`, `inspect_skill_target`, `aikito status`, `aikito doctor` without `--fix`) do not trigger `run_recovery_pass()` and do not mutate or delete pending journals. They observe existing on-disk state safely without side effects.
Verified by: `tests/test_project.py::test_classify_project_skill_state_read_only_purity`, `tests/test_project.py::test_classify_project_skill_state_convergence`.

### INV-REC-04: Non-Blocking Cleanup and Loop Prevention `[current]` {: #inv-rec-04 }

Corrupt, unparseable, or externally altered journals halt automatic recovery and retain the journal file on disk for diagnostic audit. Aikito refuses to loop indefinitely or repeatedly overwrite unverified state, requiring explicit administrative intervention or diagnostics (`aikito doctor`).
Verified by: `tests/test_skill_state.py::test_recovery_aborts_and_retains_journal_if_copy_target_externally_modified`, `tests/test_skill_state.py::test_recovery_aborts_and_retains_journal_if_file_externally_deleted`.

### INV-LOCK-01: Canonical Skill Mutator Lock Coverage `[current]` {: #inv-lock-01 }

All commands and API functions that mutate canonical skills, bundled skills, project skills runtime, or their persistent state documents MUST hold `SkillWriterLock(home)` across their entire operation. This includes: `aikito add skill --from` (`add.py`), `aikito rm skill` (`skill_runtime.py`), `SkillPlan` execution (`skill_runtime.py`), bundled skill refresh (`cli.py`), and init template refresh (`init.py`).
Verified by: `tests/test_skill_state.py::test_writer_lock_reentrancy`, `tests/test_bundled_skills.py::test_cli_sync_global_holds_writer_lock_when_applying`, `tests/test_bundled_skills.py::test_writer_lock_serializes_threads`.

### INV-LOCK-02: Lock Re-Entrancy and Outermost Hold `[current]` {: #inv-lock-02 }

`SkillWriterLock` is reentrant for the owning thread and serializes other threads and processes. Composite workflows (e.g. `aikito add skill <name> --from <path> --sync`) acquire the lock at the outermost command entrypoint and hold it continuously across canonical import, template refresh, and project sync without releasing or deadlocking.
Verified by: `tests/test_skill_state.py::test_writer_lock_reentrancy`, `tests/test_bundled_skills.py::test_writer_lock_serializes_threads`, `tests/test_add.py::test_add_skill_with_sync_holds_outer_writer_lock`.

### INV-LOCK-03: Dry-Run Exclusion `[current]` {: #inv-lock-03 }

Dry-run commands (`aikito sync --dry-run`, `aikito sync project --dry-run`, `aikito sync global --dry-run`) MUST NOT acquire or create the `writer.lock` file. Dry-run runs purely in read-only analysis mode and leaves the lock file and filesystem completely unmutated.
Verified by: `tests/test_bundled_skills.py::test_cli_sync_global_dry_run_does_not_acquire_or_create_lock`, `tests/test_cli.py::test_global_dry_run_zero_write_filesystem_snapshot`.

### INV-RES-01: Segment Boundaries Match Commit Units `[current]` {: #inv-res-01 }

Batch synchronization results are partitioned into explicit segments (`skills`, `legacy_compat`) corresponding directly to independent atomic commit units. Failure in one segment (e.g. memory or instructions sync in legacy compat) does NOT overwrite, mask, or downgrade the committed success of another segment (e.g. skills sync).
Verified by: `tests/test_project_sync.py::test_segmented_execution_result_isolates_memory_failure`, `tests/test_project_sync.py::test_segmented_execution_result_isolates_instruction_failure`.

### INV-RES-02: Overall Success Conjunction `[current]` {: #inv-res-02 }

Overall execution result `is_success` is the logical conjunction of all active segments. If any segment fails or reports conflicts, `is_success` is False, but per-segment applied operations, conflict lists, and state progressions remain accurately preserved for caller inspection and reporting.
Verified by: `tests/test_project_sync.py::test_segmented_execution_result_isolates_memory_failure`, `tests/test_project_sync.py::test_segmented_execution_result_isolates_instruction_failure`.

---

## 6. Public Python API Invariants

### INV-API-01: Exported Symbols `[current]` {: #inv-api-01 }

The public API is strictly defined by `src/aikito/__init__.py::__all__`:
```python
__all__ = [
    "AmbiguousProjectPathError",
    "InvalidProjectConfigError",
    "NoAvailableProjectPathError",
    "PreparedProject",
    "Project",
    "ProjectError",
    "ProjectNotFoundError",
    "ProjectPrepareConflictError",
    "UnsupportedProjectAgentError",
    "__version__",
]
```
Internal modules (`aikito.project`, `aikito.sync`, etc.) are private and must not be imported by external consumers.

### INV-API-02: `Project.load` Contract `[current]` {: #inv-api-02 }

- Signature: `Project.load(name: str, workspace: Path | str | None = None, home: Path | str | None = None) -> Project`
- Strictly read-only: Loads project definition from workspace `projects/<name>/agent.toml`.
- Does not modify workspace pointer (`~/.config/aikito/workspace`) or workspace files.
- Raises `ProjectNotFoundError` if `projects/<name>/agent.toml` does not exist.
- Raises `InvalidProjectConfigError` if name is invalid, workspace path is relative, or TOML is malformed.

### INV-API-03: Path Resolution Contract `[current]` {: #inv-api-03 }

- `Project.paths`: Property returning `tuple[Path, ...]`, containing all configured candidate paths resolved against the host. Does not filter by existence.
- `Project.resolve_path()`: Returns the unique active checkout `Path`.
- Raises `NoAvailableProjectPathError` if no configured path exists on the current host.
- Raises `AmbiguousProjectPathError` if more than one configured path exists on the current host.

### INV-API-04: `Project.add_path` Contract `[current]` {: #inv-api-04 }

- Signature: `Project.add_path(path: Path | str) -> Project`
- Preserves caller immutability: Original `Project` instance remains unchanged; returns a freshly loaded `Project` reflecting the appended path.
- Appends candidate path to project's `agent.toml`. Does not prepare resources, create directories, or launch agents.

### INV-API-05: `Project.prepare` Contract `[current]` {: #inv-api-05 }

- Signature: `Project.prepare(agent: str, path: Path | str | None = None) -> PreparedProject`
- **Scope**: Prepares project-scoped instructions (`AGENTS.md`), selected skills (`.agents/skills/`), and memory (`.agents/memory/`).
- **Boundaries**:
  - Does **not** launch the agent subprocess.
  - Does **not** synchronize global instructions, global skills, MCP servers, or subagents.
  - Does **not** modify workspace pointer or update project configuration.
  - When explicit `path` is passed, it is used directly without writing it into `agent.toml`.
- **Exception Contract**:
  - `UnsupportedProjectAgentError`: `agent` is not registered in workspace `agents.toml`.
  - `ProjectPrepareConflictError`: Managed resources cannot be safely synchronized, symlink capability is missing, or conflict markers are detected.
  - `NoAvailableProjectPathError`: Raised when `path` is omitted and no configured path exists on this host, **or** when explicit `path` does not exist.
  - `InvalidProjectConfigError`: Raised when explicit `path` is empty, of invalid type, or exists but is not a directory.
- **No Force Parameter**: `prepare()` has no `force` parameter and does not adopt CLI force authorization.

### INV-API-06: `PreparedProject` Contract `[current]` {: #inv-api-06 }

`PreparedProject` is a frozen dataclass exposing:
- `name: str`: Project name.
- `agent: str`: Agent identifier.
- `cwd: Path`: Resolved working directory for the agent.
- `env_overrides: Mapping[str, str]`: Read-only mapping of environment variable overrides.

### INV-API-07: Exception Hierarchy `[current]` {: #inv-api-07 }

All public exceptions inherit from `ProjectError -> RuntimeError`.
- `AmbiguousProjectPathError`: Exposes `project_name: str`, `paths: tuple[Path, ...]`.
- `ProjectPrepareConflictError`: Exposes `project_name: str`, `conflicts: tuple[str, ...]`.

---

## 7. Appendix: Migration Inventory

Every subsystem scheduled for migration into the structured Plan / Executor engine is documented below.

| Subsystem / Entry | Reads | Writes | Ownership Evidence | State File | Dry-run Behavior | Recovery Boundary | Target Engine | Old Implementation Deletion Criteria |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cli.py::cmd_sync_all` | Workspace config, `agents.toml`, all projects, global resources, agent runtimes | Runtime global symlinks, project checkouts, MCP configs, subagents | Symlink pointing within workspace canonical roots; copy matching | None (delegates to subsystem sync handlers) | First runs complete `--dry-run` preview pass; if safe (`plan.can_apply` and not dry-run), immediately executes without interactive prompt | Partial failure leaves processed items intact; no rollback across subsystems | Unified Coordinator | Replaced when all underlying resource subsystems (project, global, MCP, subagent) migrate to unified Plan/Executor |
| `cli.py::cmd_global_sync` | Workspace `skills/`, `global/AGENTS.md`, `agents.toml` | Agent runtime instruction symlinks and global skills | Symlinks resolving to exact canonical paths (`canonical_root / name`); shared target deduplication via `resolve_targets` | None (link-only, no baseline tracking) | Read-only simulation (`--dry-run`); zero writes to home directory, workspace, or lock files | Structured Plan/Executor validation; outermost `SkillWriterLock` | Global Skill Plan/Executor (Phase 4) | Replaced in Phase 4 for global skills; global instructions migrated in Phase 5 |
| `cli.py::cmd_project_sync` | Project `agent.toml`, `AGENTS.md`, workspace skills, memory | Project checkout `.agents/skills/`, `.agents/memory/`, instructions | Symlink resolving to exact canonical roots; copy management records | `.aikito/state/project-skills/` records | Read-only preview (`[DRY RUN CLEANUP]`, `[DRY RUN LINK]`, `[DRY RUN COPY]`) | Atomic compare-and-swap (CAS) plan preflight; rollback and crash recovery journals | Project Plan/Executor | Replaced when structured Executor handles project sync |
| `sync.py::sync_resource`, `apply_runtime_cleanup`, `sync_global_entry`; `compat.py::safe_symlink` | Source filesystem item, target filesystem item | Target symlink or copied directory tree; unlinks stale entries | `is_symlink_pointing_to` verifies exact canonical target path (`canonical_root / name`); `safe_symlink` creates link via `symlink_to()` with rollback and OS error handling | None | `dry_run=True` checks existence/paths and prints preview without filesystem mutation | Direct filesystem operations; rollback on safe link creation; no tempfile atomic swap | Core Primitives (Project & Global) | Global skills ceased in Phase 4; instructions migrated to unified Link Plan/Executor in Phase 5; remaining caller: project memory (Phase 6) |
| `project.py::classify_project_skill_state` | Canonical skill directory, runtime checkout skill entry, project skill state records | None (pure query/classification functions, zero state mutation, no recovery pass) | Exact canonical destination check via `inspect_skill_target` + `plan_single_skill` | Reads `.aikito/state/project-skills/` documents via `inspect_skill_target`; writes nothing | Purely functional / read-only | Non-destructive query; exempt from recovery pass | Thin Planner Wrapper (`skill_plan.py`) | Legacy heuristic eliminated in Phase 3; delegates directly to `inspect_skill_target` and `plan_single_skill` with explicit state mapping table |
| `project_runtime.py::Project.prepare`, `sync_project_path`, `_resolve_project_sync_inputs` | Workspace config, `agents.toml`, project `agent.toml`, checkout directories | Checkout instructions, skills, memory | Symlink targets, copy directory comparisons | None | Supported via `dry_run` parameter in internal helpers | Validates conflicts before modifying persistent resources; write failure raises `ProjectPrepareConflictError` | Project Executor | Replaced when `prepare` delegates to structured project Plan/Executor |
| `mcp.py::sync_mcp_configs`, `sync_remove_mcp_from_agents`, `remove.py::remove_mcp` | Workspace `mcps/*.toml`, agent configuration files, `.local/state/aikito/mcp-state.json` | Agent configuration files (merged blocks), workspace `mcps/*.toml` on remove, updates `.local/state/aikito/mcp-state.json`, creates backup files | Recorded server entries in `.local/state/aikito/mcp-state.json` | `.local/state/aikito/mcp-state.json` (tracks applied server hashes per agent config) | Full read-only merge simulation; prints diff/actions without touching files or state | Timestamped backups created prior to writing; atomic state promotion via temporary state file and `os.replace`; restores backup on failure | Phase 7 (MCP Engine) | Replaced when MCP engine adopts unified Plan/Executor model |
| `subagent.py::build_plan`, `sync_subagent_configs`, `remove.py::remove_subagent` | Workspace `subagents/<name>.md`, `subagents.toml`, agent configuration files / subagent directories | Agent subagent prompt files / configs, workspace `subagents/<name>.md` and `subagents.toml` on remove | Generated prompt Aikito header banner / managed comment markers | None | Previews generated subagent plan items and actions | File-level backups for modified configs; atomic write via tempfile (`_write_file_atomic`) | Phase 7 (Subagent Engine) | Replaced when subagent engine adopts unified Plan/Executor model |
| `adopt.py::build_adopt_plan`, `execute_adoption` | Agent native configuration files, skills, MCP definitions | Workspace definitions (`skills/`, `mcps/`, `subagents/`, `projects/`, `config.toml`) | Native config presence; user approval via interactive/explicit plan | Timestamped backup directory `~/.aikito/backups/adopt_<timestamp>` | Complete read-only plan preview (`--dry-run`); calculates all changes and conflicts | Full preflight validation before any write; timestamped backups created | Phase 8 (Adoption Engine) | Preserved; integrates with unified Planner validation |
| `doctor.py::run_doctor`, `run_doctor_fixes` | Host environment, workspace, checkouts, runtime links, orphaned configs, `mcps/*.toml`, `.local/state/aikito/mcp-state.json` | Missing symlinks, repairs broken pointers (only when `--fix` is passed) | Cross-references workspace definitions against runtime entries and MCP state | Reads `.local/state/aikito/mcp-state.json` for drift checks; does not write state directly | Read-only inspection by default; `--fix` required to apply changes | Individual issue repairs; idempotent execution | Phase 8 (Unified Diagnostics) | Doctor rules updated to query unified state store and Plan/Executor diagnostics |
| Canonical Skill Mutators (`add skill --from`, global `rm skill`, bundled refresh, init refresh, `SkillPlan` apply) | Canonical workspace skills, bundled skills, import sources | Canonical `workspace/skills/<name>`, state files, transaction journals | Canonical workspace ownership | `.aikito/state/` (`writer.lock`, state records, journal) | `--dry-run` does not acquire or create `writer.lock`, leaves filesystem and state untouched | Outermost re-entrant host writer lock (`SkillWriterLock`), automatic recovery pass on `recovery_required` | Transactional Skill Engine (`skill_runtime.py`) | All canonical skill mutating paths hold `SkillWriterLock(home)` at their outermost entrypoint (`add.py:768/947`, `skill_runtime.py:1156`, `cli.py:291`, `init.py:190`, `skill_runtime.py:291`) |
