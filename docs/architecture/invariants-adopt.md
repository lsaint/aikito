# Adoption Engine Invariants

### INV-ADOPT-01: Adoption and Runtime Sync Separation `[planned]` {: #inv-adopt-01 }

- The Adoption Engine discovers unmanaged agent configurations, skills, and MCP definitions in the host environment and imports them into the canonical Aikito workspace.
- Adoption is not "reverse sync"; it has distinct semantic rules:
  - Multi-agent source discovery and merging
  - Canonical registration under `skills/`, `subagents/`, `mcps/`, `global/AGENTS.md`
  - Canonical format conversion and deduplication
  - Sensitive credential preservation in private payloads with redacted public plans
  - Native agent source configuration backups
- Adoption decisions do not use or alter runtime synchronization state files (`.local/state/aikito/`).

### INV-ADOPT-02: Multi-Source Instruction Conflict Resolution `[planned]` {: #inv-adopt-02 }

- When discovering instructions from multiple installed agents:
  - If multiple sources provide identical instruction content, they are deduplicated into a single canonical `global/AGENTS.md`.
  - If sources contain conflicting content, the planner reports a `Finding` with conflict status.
  - Conflicts prevent adoption of conflicting sections unless explicitly resolved or overridden.

### INV-ADOPT-03: Explicit Skip Scoping `[planned]` {: #inv-adopt-03 }

- The `--skip` flag specifies resources or resource kinds to omit from adoption (e.g., `--skip claude-code`, `--skip mcp:fetch`).
- Skipped resources are excluded during `AdoptPlan` construction or request handling:
  - They are recorded in `AdoptPlan.skipped`.
  - They generate no mutation entries in `AdoptFilePlan`.
  - They do not trigger source backups or target writes.

### INV-ADOPT-04: Target Pre-Image and Idempotence `[planned]` {: #inv-adopt-04 }

- Each canonical file mutation in `AdoptPlan` records:
  - The exact physical destination path in the workspace
  - The expected pre-image state (either non-existent or matching a specific fingerprint)
  - The complete desired content payload
- If the canonical destination file already exists with identical content, the planned action is `NOOP`.
- If the destination file already exists with differing content not originating from the adoption preview, the plan records a collision conflict.

### INV-ADOPT-05: Local Agent Source Backup Boundary `[planned]` {: #inv-adopt-05 }

- Prior to modifying or importing existing local agent configurations, timestamped backups of the original agent source files are created under `~/.aikito/backups/adopt_<timestamp>`.
- Backups protect original agent configuration files against data loss.
- If backup creation fails, adoption execution immediately aborts with zero canonical workspace writes.

### INV-ADOPT-06: Stale Adopt Plan Zero-Write Guarantee `[planned]` {: #inv-adopt-06 }

- `execute_adopt_plan(plan)` verifies target file pre-images and source file availability before performing any workspace mutation.
- If a target workspace file was created or modified between plan creation and execution:
  - The plan is marked stale.
  - Zero workspace writes are executed.
  - An explicit stale error or replan requirement is returned.
- Adoption execution never silently re-scans or re-merges during execution.

### INV-ADOPT-07: Credential Isolation and Redaction `[planned]` {: #inv-adopt-07 }

- Discovered MCP configurations containing environment variables, API keys, or header credentials retain sensitive values in private execution payloads for writing valid TOML files into `mcps/`.
- Public plan representations, summary strings, verbose logs, and Doctor diagnostics must redact all credentials.
- Backups of native configuration files retain their original permissions and credentials under the user's home directory.

### INV-ADOPT-08: Structured Adopt Execution Result `[planned]` {: #inv-adopt-08 }

- `execute_adopt_plan()` returns an `AdoptExecutionResult` detailing:
  - `instructions`: List of adopted instruction targets
  - `mcps`: List of adopted MCP definitions
  - `subagents`: List of adopted subagent prompt files and registry entries
  - `backups`: List of created backup files
  - `skipped`: List of intentionally skipped items
  - `failed`: List of operations that failed execution
  - `success`: Boolean indicating whether all planned operations succeeded
- If a failure occurs during execution, the result accurately reflects files actually written versus uncommitted files.
