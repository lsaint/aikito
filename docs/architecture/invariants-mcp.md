# MCP Server Invariants

Contracts governing Model Context Protocol (MCP) server configuration sync, state management,
fingerprinting, multi-server file aggregation, sensitive credentials protection, and transactional rollback.

---

## Core Rules

### INV-MCP-01: Managed Fingerprint as Server Node Ownership and Drift Evidence `[current]` {: #inv-mcp-01 }

MCP server ownership and drift detection rely on logical server configuration fingerprints recorded in `.local/state/aikito/mcp-state.json`. If an existing runtime entry matches the recorded managed fingerprint, changes to the desired configuration evaluate to `UPDATE`. If an existing entry differs from the managed fingerprint, it is classified as external drift and evaluates to `CONFLICT`. Passing `--force` explicitly authorizes overwriting the drifted server entry.

*Targeted tests*: `tests/test_config_characterization.py`, `tests/test_mcp_plan.py`, `tests/test_mcp.py`

### INV-MCP-02: Same-File Multi-Server Chained Aggregation Without Overwrite `[current]` {: #inv-mcp-02 }

When multiple MCP servers target the same physical agent configuration file (e.g., `~/.claude.json` or `.config/opencode/opencode.jsonc`), their mutations must be chained and merged in memory from the same frozen pre-image. The final merged content is written exactly once to the target file. Unmanaged server entries, comments, and non-MCP settings must be preserved intact.

*Targeted tests*: `tests/test_config_characterization.py`, `tests/test_mcp_plan.py`, `tests/test_mcp.py`

### INV-MCP-03: Transactional Consistency Between Runtime Config and State Store Commit `[planned]` {: #inv-mcp-03 }

MCP configuration updates and state store updates form a single transactional consistency unit. Backups of all eligible runtime targets are prepared before any writes. Runtime configuration files are updated first; if any write fails, previously written runtime files are restored from backup. If all runtime writes succeed, the temporary state file is atomically promoted via `os.replace`. If state promotion fails, runtime files are rolled back to their pre-mutation states.

*Targeted tests*: `tests/test_mcp.py`

### INV-MCP-04: Stale Plan Invalidation on Runtime File or State Store Pre-Image Mutation `[current]` {: #inv-mcp-04 }

An `MCPPlan` captures pre-image fingerprints of both targeted runtime configuration files and `.local/state/aikito/mcp-state.json`. If either a runtime configuration file or the state store is mutated externally after plan construction, the plan is marked stale and execution halts immediately before any file or state modification.

*Targeted tests*: `tests/test_config_characterization.py`, `tests/test_mcp_plan.py`, `tests/test_mcp.py`

### INV-MCP-05: Structured and Plaintext Display Redaction of Sensitive Environment Secrets `[current]` {: #inv-mcp-05 }

MCP server environment variables containing sensitive keys or credential tokens must be redacted in all plan summaries, CLI outputs, error messages, and public structured views. Raw secrets must never appear in `repr`, `asdict()`, or serialized outputs.

*Targeted tests*: `tests/test_config_characterization.py`, `tests/test_mcp_plan.py`, `tests/test_mcp.py`

### INV-MCP-06: Backup Suppression and Secure File Permissions on Sensitive Configuration Targets `[planned]` {: #inv-mcp-06 }

Configuration files containing sensitive credentials or marked as sensitive (e.g. `claude_json`, `agy_json`) suppress standard whole-file backups to prevent plaintext secret leaks into backup directories. Any configuration files created or updated with sensitive credentials must enforce secure filesystem permissions (mode `0o600`).

*Targeted tests*: `tests/test_mcp.py`

### INV-MCP-07: Unified Desired-Absent Model for MCP Removal via Shared Executor `[planned]` {: #inv-mcp-07 }

`aikito rm mcp <name> --sync` operates by planning the target server as `Desired Absent` (`REMOVE` operation) through the unified MCP Planner and Executor. Removal reuses the same file aggregation, backup, and state commit transaction engine rather than maintaining a separate runtime deletion code path.

*Targeted tests*: `tests/test_mcp.py`

### INV-MCP-08: Preservation of Recovery Materials and Explicit recovery_required on Rollback Failure `[planned]` {: #inv-mcp-08 }

If an error occurs during runtime file rollback or state recovery, created backups must be strictly preserved on disk, and the execution result must report `recovery_required=True` along with exact recovery guidance for the user.

*Targeted tests*: `tests/test_mcp.py`

---

## MCP Planning State Table

| Runtime Entry State | State Store Record | Desired Canonical | Force Authorized | Planned Action |
| --- | --- | --- | --- | --- |
| Missing | Absent | Present | N/A | `CREATE` |
| Matches Desired | Matches Desired | Present | N/A | `NOOP` |
| Differs from Desired | Matches State ($R = S \ne C$) | Present | N/A | `UPDATE` |
| Differs from State ($R \ne S$) | Present | Present | No | `CONFLICT` (drift) |
| Differs from State ($R \ne S$) | Present | Present | Yes (`--force`) | `UPDATE` (authorized) |
| Present (Managed) | Present ($R = S$) | Absent | Yes (`rm --sync`) | `REMOVE` |
| Present (Unmanaged) | Absent | Absent | Any | `NOOP` (preserve) |
