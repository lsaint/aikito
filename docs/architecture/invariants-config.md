# Structured Configuration Invariants

Contracts governing configuration targets, logical node scoping, physical file aggregation,
atomic write-once mutation, and stale plan invalidation across Subagents and MCP servers.

---

## Core Rules

### INV-CFG-01: Separation of Logical Config Node and Physical File `[current]` {: #inv-cfg-01 }

Aikito explicitly distinguishes logical configuration nodes (e.g., individual subagents or MCP server blocks) from physical configuration files on disk. A logical operation (such as `CREATE`, `UPDATE`, `REMOVE`, `NOOP`, or `CONFLICT`) targets a logical node identity `(physical_target, key_path)` rather than assuming whole-file ownership.

*Targeted tests*: `tests/test_config_runtime.py`, `tests/test_config_characterization.py`, `tests/test_subagent.py`, `tests/test_mcp.py`

### INV-CFG-02: Single Pre-Image Multi-Operation File Aggregation and Write-Once Guarantee `[current]` {: #inv-cfg-02 }

When multiple logical operations target the same physical configuration file (identified by canonical physical identity via `compat.get_physical_path()`), they must be grouped into a single `FileMutationPlan`. All mutations for that physical file start from the same frozen pre-image snapshot, apply deterministic in-memory format transformations, and execute at most one physical file write. Successive logical operations targeting the same file must never perform repeated file reads or intermediate writes to disk.

*Targeted tests*: `tests/test_config_runtime.py`, `tests/test_config_characterization.py`, `tests/test_subagent.py`, `tests/test_mcp.py`

### INV-CFG-03: Duplicate and Colliding Logical Key Detection Prior to Write `[current]` {: #inv-cfg-03 }

If multiple logical resources resolve to identical or colliding `(physical_file, logical_key)` destinations—including name collisions resulting from agent-specific `mcp_name_style` normalization or conflicting operations (e.g., simultaneous `REMOVE` and `UPDATE` on the same key)—the planner must detect the collision during planning and halt execution before any disk write. Last-write-wins is strictly forbidden.

*Targeted tests*: `tests/test_config_runtime.py`, `tests/test_config_characterization.py`, `tests/test_mcp.py`

### INV-CFG-04: Pre-Image Mutation Stale Plan Invalidation `[current]` {: #inv-cfg-04 }

Every physical configuration target planned for mutation captures a frozen pre-image snapshot (content hash, existence state, and metadata) during Plan construction. Before applying mutations, the executor verifies that the on-disk file matches the plan's frozen pre-image. If the target file or its management state was modified externally between preview and apply, the plan is marked stale and execution halts immediately without silent replanning.

*Targeted tests*: `tests/test_config_runtime.py`, `tests/test_config_characterization.py`, `tests/test_subagent.py`, `tests/test_mcp.py`

### INV-CFG-05: Preservation of Unmanaged Configuration Content and Format Semantics `[current]` {: #inv-cfg-05 }

Format handlers must preserve all unmanaged sections, user comments, formatting whitespace, and unrelated configuration blocks within shared configuration files (such as YAML patches, JSON, JSONC, and TOML). Aikito only modifies nodes specifically owned and managed by Aikito. Whole-file replacement is restricted to dedicated, single-resource agent files where the entire file is proven to be owned by Aikito.

*Targeted tests*: `tests/test_config_characterization.py`, `tests/test_subagent.py`, `tests/test_mcp.py`

---

## Configuration Planning State Table

| Pre-Image State | Logical Key State | Ownership Evidence | Planned Action |
| --- | --- | --- | --- |
| Missing | Missing | N/A | `CREATE` |
| Present | Present | Managed (content matches) | `NOOP` |
| Present | Present | Managed (content differs) | `UPDATE` |
| Present | Present | Unmanaged / Drift | `CONFLICT` (preserve) |
| Present | Present | Managed, absent in desired | `REMOVE` (with prune / authorization) |
| Present | Missing | Absent in desired | `NOOP` |
| Tampered since plan | Any | Any | `STALE` (halt) |
