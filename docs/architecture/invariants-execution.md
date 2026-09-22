# Execution Invariants

## Authorization
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

## Selection Transactions
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

## Pending Journals
### INV-PEND-01: Journal Storage Location and Safe Identifiers `[current]` {: #inv-pend-01 }

Transaction journals are stored under `.aikito/state/project-skills/transactions/<tx_id>/journal.json` relative to the state root (`$HOME` or specified `home`). Each transaction is assigned a cryptographically random, safe alphanumeric/dash/underscore identifier validated by `_is_safe_tx_id()`. Journal storage is strictly per-host and local; journals are never tracked in Git or exported to user workspaces.
Verified by: `tests/test_skill_state.py::test_save_and_load_roundtrip`, `tests/test_skill_state.py::test_forged_journal_rejected`.

### INV-PEND-02: Trusted Boundaries and Path Derivation `[current]` {: #inv-pend-02 }

Journals must strictly reference paths within trusted boundaries: workspace root, authorized checkouts, and runtime staging directories (`.aikito-tx/<tx_id>`). Any journal attempting path traversal, symlink/reparse point escaping, or arbitrary filesystem mutation outside authorized roots is rejected as forged/corrupt (`_is_valid_file_path`, `_is_valid_target_path`, `_is_valid_staging_or_recovery_dir`).
Verified by: `tests/test_skill_state.py::test_forged_journal_rejected`, `tests/test_skill_state.py::test_forged_journal_files_cannot_authorize_checkout`.

### INV-PEND-03: Journal File and Directory Permissions `[current]` {: #inv-pend-03 }

Transaction directories and journal files are created with restricted POSIX permissions (`0700` for directories, `0600` for journal files) via `secure_directory_permissions` and `secure_file_permissions` (or equivalent restricted ACLs on Windows). Journals must be real regular files; symlinks, junctions, or reparse points as journal paths cause immediate abort.
Verified by: `tests/test_skill_state.py::test_forged_journal_rejected`.

## Recovery
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

## Writer Lock
### INV-LOCK-01: Canonical Skill Mutator Lock Coverage `[current]` {: #inv-lock-01 }

All commands and API functions that mutate canonical skills, bundled skills, project skills runtime, or their persistent state documents MUST hold `SkillWriterLock(home)` across their entire operation. This includes: `aikito add skill --from` (`add.py`), `aikito rm skill` (`skill_runtime.py`), `SkillPlan` execution (`skill_runtime.py`), bundled skill refresh (`cli.py`), and init template refresh (`init.py`).
Verified by: `tests/test_skill_state.py::test_writer_lock_reentrancy`, `tests/test_bundled_skills.py::test_cli_sync_global_holds_writer_lock_when_applying`, `tests/test_bundled_skills.py::test_writer_lock_serializes_threads`.

### INV-LOCK-02: Lock Re-Entrancy and Outermost Hold `[current]` {: #inv-lock-02 }

`SkillWriterLock` is reentrant for the owning thread and serializes other threads and processes. Composite workflows (e.g. `aikito add skill <name> --from <path> --sync`) acquire the lock at the outermost command entrypoint and hold it continuously across canonical import, template refresh, and project sync without releasing or deadlocking.
Verified by: `tests/test_skill_state.py::test_writer_lock_reentrancy`, `tests/test_bundled_skills.py::test_writer_lock_serializes_threads`, `tests/test_add.py::test_add_skill_with_sync_holds_outer_writer_lock`.

### INV-LOCK-03: Dry-Run Exclusion `[current]` {: #inv-lock-03 }

Dry-run commands (`aikito sync --dry-run`, `aikito sync project --dry-run`, `aikito sync global --dry-run`) MUST NOT acquire or create the `writer.lock` file. Dry-run runs purely in read-only analysis mode and leaves the lock file and filesystem completely unmutated.
Verified by: `tests/test_bundled_skills.py::test_cli_sync_global_dry_run_does_not_acquire_or_create_lock`, `tests/test_cli.py::test_global_dry_run_zero_write_filesystem_snapshot`.

## Segmented Results
### INV-RES-01: Segment Boundaries Match Commit Units `[current]` {: #inv-res-01 }

Batch synchronization results are partitioned into explicit segments (`skills`, `instructions`, `memory`) corresponding directly to independent atomic commit units. Failure in one segment (e.g. memory sync) does NOT overwrite, mask, or downgrade the committed success of another segment (e.g. skills or instructions sync).
Verified by: `tests/test_project_sync.py::test_segmented_execution_result_isolates_memory_failure`, `tests/test_project_sync.py::test_segmented_execution_result_isolates_instruction_failure`.

### INV-RES-02: Overall Success Conjunction `[current]` {: #inv-res-02 }

Overall execution result `is_success` is the logical conjunction of all active segments. If any segment fails or reports conflicts, `is_success` is False, but per-segment applied operations, conflict lists, and state progressions remain accurately preserved for caller inspection and reporting.
The three active segments in the current implementation are `SkillExecutionResult`, `InstructionExecutionResult`, and `MemoryExecutionResult`.
Verified by: `tests/test_project_sync.py::test_segmented_execution_result_isolates_memory_failure`, `tests/test_project_sync.py::test_segmented_execution_result_isolates_instruction_failure`.

---

