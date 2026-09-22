# Application Layer & Workspace Coordinator Invariants

### INV-APP-01: Read-Only Workspace Sync Planning `[planned]` {: #inv-app-01 }

- Building a `WorkspaceSyncPlan` via `build_workspace_sync_plan()` is strictly read-only.
- Inspects workspace configuration, `agents.toml`, projects, and agent runtimes.
- Does not mutate workspace files, host agent configurations, project checkouts, symlinks, or state files.
- Does not acquire or create persistent lock files (e.g. `SkillWriterLock`), temporary files, pending journals, or timestamped backups.
- Does not trigger recovery mutations during planning.
- `aikito sync --dry-run` must simply build and render `WorkspaceSyncPlan` without side effects.

### INV-APP-02: Same-Plan Execution Fidelity `[planned]` {: #inv-app-02 }

- `execute_workspace_sync_plan(plan)` executes the exact operations calculated during the plan phase.
- Applying a plan must not re-invoke resource planners (`build_skill_plan`, `build_subagent_plan`, `build_mcp_plan`, etc.).
- Precondition validation is performed by each underlying resource executor against the plan's frozen observed state.
- Authorizations granted to a plan cannot be implicitly transferred to a newly recomputed plan.

### INV-APP-03: Structured State Independence from Output Rendering `[planned]` {: #inv-app-03 }

- All synchronization statistics (`changes`, `unchanged`, `offline`, `warnings`, `conflicts`, `errors`, `can_apply`) are derived directly from structured domain objects:
  - Resource operation actions (`CREATE`, `UPDATE`, `UNLINK`, `REMOVE`, `NOOP`)
  - Resource plan `can_apply` flags
  - `Finding` objects (`Finding.status`, `Finding.code`)
  - Project binding and availability records
  - Structured execution results
- Synchronization planning and decision making must never parse stdout, stderr, or terminal formatting markers (such as `[CREATE]`, `[CONFLICT]`, `[WARN]`).
- Output rendering (terminal markers, tables, colors) is strictly presentation-tier and has zero influence on execution decisions or exit codes.

### INV-APP-04: Partial Failure and Segmented Results `[planned]` {: #inv-app-04 }

- `WorkspaceSyncExecutionResult` accurately records execution status per subsystem segment:
  - `global_result`: Global skills and instructions
  - `subagent_result`: Subagent configurations
  - `mcp_result`: MCP server configurations
  - `project_results`: Project-specific skills, instructions, and memory
- If a later segment (e.g., an MCP config write) fails, previously committed segments (e.g., global skills and subagents) remain committed and are recorded as succeeded.
- Aikito does not support or claim full cross-subsystem atomic rollback.
- A failure in one segment must never misreport completed segments as failed or uncommitted.

### INV-APP-05: Stale Precondition Plan Rejection (No Silent Re-plan) `[planned]` {: #inv-app-05 }

- If any underlying resource state changes between plan generation and execution (stale precondition, CAS mismatch, changed file fingerprint):
  - Execution of the affected resource or segment is aborted.
  - The executor must not silently recalculate a new plan and apply it in the same invocation.
  - The execution result indicates failure or replan requirement, requiring explicit re-invocation.

### INV-APP-06: Structured Project Binding and Offline Status `[planned]` {: #inv-app-06 }

- Configured projects are represented in `WorkspaceSyncPlan.project_entries` with explicit binding states: `active`, `offline`, `unbound`.
- Projects whose configured paths do not exist on the current host are marked `offline`:
  - They are counted as structured `offline` projects.
  - They do not trigger runtime inspection, symlink creation, or conflict checks.
  - They are not counted as errors or conflicts.
- Project offline status is determined from project path resolution on the host, never inferred from printed text lines.

### INV-APP-07: Bundled Skill Refresh Re-plan Boundary `[current]` {: #inv-app-07 }

- Bundled skill updates are represented as a structured `BundledSkillRefreshPlan`.
- If an outdated bundled skill is refreshed in the canonical workspace during sync:
  - Canonical workspace skill contents change, potentially invalidating dependent project skill plans that relied on pre-refresh canonical snapshots.
  - The workspace coordinator must mark `replan_required = True`.
  - Dependent project skill operations relying on stale canonical snapshots are deferred rather than executed with mismatched baselines.
  - The CLI informs the user to re-run `aikito sync` to synchronize projects against updated canonical skills.
- Verified by: `tests/test_global_sync_plan.py`, `tests/test_bundled_skills.py`.

### INV-APP-08: Sensitive Configuration Redaction Across Views `[planned]` {: #inv-app-08 }

- Sensitive data (MCP environment variables, header credentials, API tokens) must be redacted across all public and presentation interfaces:
  - CLI verbose output
  - Web Console JSON endpoints and inspection models
  - Public Python API inspection views
  - Doctor diagnostics
- Desensitization must use a shared redaction policy (`redact_mcp_entry`). Raw secret values are restricted to private execution payloads.

### INV-APP-09: Zero-Write Web Console Inspection `[planned]` {: #inv-app-09 }

- Read-only Web Console requests (`GET` endpoints) consume shared application and resource inspection views.
- Handling a Web Console request must never:
  - Acquire or release file locks
  - Create backup files or temporary files
  - Touch or modify file mtimes
  - Trigger pending journal recovery or adoption writes
  - Mutate workspace pointer or runtime configuration
