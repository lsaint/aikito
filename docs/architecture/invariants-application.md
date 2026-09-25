# Application Layer & Workspace Coordinator Invariants

### INV-APP-01: Read-Only Workspace Sync Planning `[current]` {: #inv-app-01 }

- Building a `WorkspaceSyncPlan` via `build_workspace_sync_plan()` is strictly read-only.
- Inspects workspace configuration, `agents/<name>.toml`, projects, and agent runtimes.
- Does not mutate workspace files, host agent configurations, project checkouts, symlinks, or state files.
- Does not acquire or create persistent lock files (e.g. `WorkspaceWriterLock`), temporary files, pending journals, or timestamped backups.
- Does not trigger recovery mutations during planning.
- `aikito sync --dry-run` must simply build and render `WorkspaceSyncPlan` without side effects.
- Verified by: `tests/test_workspace_sync_plan.py`.

### INV-APP-02: Same-Plan Execution Fidelity `[current]` {: #inv-app-02 }

- `execute_workspace_sync_plan(plan)` executes the exact operations calculated during the plan phase.
- Applying a plan must not re-invoke resource planners (`build_skill_plan`, `build_subagent_plan`, `build_mcp_plan`, etc.).
- Precondition validation is performed by each underlying resource executor against the plan's frozen observed state.
- Authorizations granted to a plan cannot be implicitly transferred to a newly recomputed plan.
- Verified by: `tests/test_workspace_sync_plan.py`.

### INV-APP-03: Structured State Independence from Output Rendering `[current]` {: #inv-app-03 }

- All synchronization statistics (`changes`, `unchanged`, `offline`, `warnings`, `conflicts`, `errors`, `can_apply`) are derived directly from structured domain objects:
  - Resource operation actions (`CREATE`, `UPDATE`, `UNLINK`, `REMOVE`, `NOOP`)
  - Resource plan `can_apply` flags
  - `Finding` objects (`Finding.status`, `Finding.code`)
  - Project binding and availability records
  - Structured execution results
- Synchronization planning and decision making must never parse stdout, stderr, or terminal formatting markers (such as `[CREATE]`, `[CONFLICT]`, `[WARN]`).
- Output rendering (terminal markers, tables, colors) is strictly presentation-tier and has zero influence on execution decisions or exit codes.
- Verified by: `tests/test_sync_plan.py`.

### INV-APP-04: Partial Failure and Segmented Results `[current]` {: #inv-app-04 }

- `WorkspaceSyncExecutionResult` accurately records execution status per subsystem segment:
  - `global_result`: Global skills and instructions
  - `subagent_result`: Subagent configurations
  - `mcp_result`: MCP server configurations
  - `project_results`: Project-specific skills, instructions, and memory
- If a later segment (e.g., an MCP config write) fails, previously committed segments (e.g., global skills and subagents) remain committed and are recorded as succeeded.
- Aikito does not support or claim full cross-subsystem atomic rollback.
- A failure in one segment must never misreport completed segments as failed or uncommitted.
- Verified by: `tests/test_workspace_sync_plan.py`.

### INV-APP-05: Stale Precondition Plan Rejection (No Silent Re-plan) `[current]` {: #inv-app-05 }

- If any underlying resource state changes between plan generation and execution (stale precondition, CAS mismatch, changed file fingerprint):
  - Execution of the affected resource or segment is aborted.
  - The executor must not silently recalculate a new plan and apply it in the same invocation.
  - The execution result indicates failure or replan requirement, requiring explicit re-invocation.
- Verified by: `tests/test_workspace_sync_plan.py`.

### INV-APP-06: Structured Project Binding and Offline Status `[current]` {: #inv-app-06 }

- Configured projects are represented in `WorkspaceSyncPlan.project_entries` with explicit binding states: `active`, `offline`, `unbound`.
- Projects whose configured paths do not exist on the current host are marked `offline`:
  - They are counted as structured `offline` projects.
  - They do not trigger runtime inspection, symlink creation, or conflict checks.
  - They are not counted as errors or conflicts.
- Project offline status is determined from project path resolution on the host, never inferred from printed text lines.
- Verified by: `tests/test_workspace_sync_plan.py`.

### INV-APP-07: Bundled Skill Refresh Re-plan Boundary `[current]` {: #inv-app-07 }

- Bundled skill updates are represented as a structured `BundledSkillRefreshPlan`.
- If an outdated bundled skill is refreshed in the canonical workspace during sync:
  - Canonical workspace skill contents change, potentially invalidating dependent project skill plans that relied on pre-refresh canonical snapshots.
  - The workspace coordinator must mark `replan_required = True`.
  - Dependent project skill operations relying on stale canonical snapshots are deferred rather than executed with mismatched baselines.
  - The CLI informs the user to re-run `aikito sync` to synchronize projects against updated canonical skills.
- Verified by: `tests/test_global_sync_plan.py`, `tests/test_bundled_skills.py`.

### INV-APP-08: Sensitive Configuration Redaction Across Views `[current]` {: #inv-app-08 }

- Sensitive data (MCP environment variables, header credentials, API tokens) must be redacted across all public and presentation interfaces:
  - CLI verbose output
  - Web Console JSON endpoints and inspection models
  - Public Python API inspection views
  - Doctor diagnostics
- Desensitization must use a shared redaction policy (`redact_mcp_entry`). Raw secret values are restricted to private execution payloads.

### INV-APP-09: Zero-Write Web Console Inspection `[current]` {: #inv-app-09 }

- Read-only Web Console requests (`GET` endpoints) consume shared application and resource inspection views.
- Handling a Web Console request must never:
  - Acquire or release file locks
  - Create backup files or temporary files
  - Touch or modify file mtimes
  - Trigger pending journal recovery or adoption writes
  - Mutate workspace pointer or runtime configuration

### INV-APP-10: Coordinators Consume Plan Observations `[current]` {: #inv-app-10 }

- Workspace-level and public-API coordinators MUST consume cross-resource plan semantics through stable plan observations and MUST NOT derive summary, diagnostic severity, or authorization semantics from resource-specific action strings.
- All resource plans provide `.observe() -> PlanObservation` mapping domain operations to canonical `OperationEffect` and structured `Finding` objects.
- Workspace summary properties (`changes`, `unchanged`) are computed strictly from observation summary counts.
- Verified by: `tests/test_plan_observation.py`, `tests/test_plan_observation_architecture.py`.

### INV-APP-11: Plan Observation Is Pure `[current]` {: #inv-app-11 }

- Observing an immutable plan MUST NOT inspect mutable external state, re-plan resources, access the network, execute subprocess commands, or modify files.
- `plan.observe()` is deterministic: calling `observe()` multiple times on the same plan produces identical results.
- Verified by: `tests/test_plan_observation_architecture.py`.

### INV-APP-12: Observation Semantics Are Domain-Owned `[current]` {: #inv-app-12 }

- Mapping a domain-specific operation into generic effect and diagnostic semantics MUST be implemented by the owning domain or an adjacent operation adapter, never by a Workspace coordinator.
- Diagnostic findings are single-source: each diagnostic is generated once by its owning domain and composed without duplicate reproduction.
- Verified by: `tests/test_plan_observation.py`.

### INV-APP-13: Unknown Observation Actions Fail Closed `[current]` {: #inv-app-13 }

- A domain action without an observation mapping MUST produce an explicit internal failure during development/testing (via exhaustive action test suites) and MUST NOT silently default to NOOP, SKIP, or NONE.
- In production observation paths, it MUST produce an `ERROR` finding and set `can_apply = False` to prevent uncontrolled runtime crashes while blocking execution.
- Verified by: `tests/test_plan_observation.py`, `tests/test_plan_observation_architecture.py`.

