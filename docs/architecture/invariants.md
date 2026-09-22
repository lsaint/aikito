# Engineering Invariants

Internal engineering contract governing ownership evidence, state transitions,
authorization scopes, transactional boundaries, and API guarantees across Aikito.

This document is the entry point for all engineering invariants. For user-facing
safety documentation, see [Safety Model](../safety.md).

---

## Purpose

Every invariant is identified by a permanent Rule ID stable across refactors and
phases. Invariants serve as implementation and testing specifications; they are
not user documentation.

---

## Status Annotations

- `[current]`: Behavior verified by regression tests and currently active in the codebase.
- `[planned]`: Target contract for Plan / Executor / State Store implementation.
- `[compat-gap]`: Discrepancy between current implementation heuristic and target safety model, documented for alignment.

---

## Common Notation

- $C$: Content fingerprint of the canonical resource in the workspace (`skills/<name>/`).
- $R$: Content fingerprint of the runtime resource in the checkout (`.agents/skills/<name>/`).
- $B$: Baseline fingerprint recorded in the active copy management record at last successful sync.

> **Note**: $B$ is only used by copy-managed Project Skills. Global Skills, Instructions, and Memory runtime visibility are link-only and do not use a content baseline.

---

## Rule Registry

Each Rule ID is permanent. The table below maps every prefix to its canonical document.

| Prefix | Area | Canonical Document |
| --- | --- | --- |
| `INV-OWN-*` | Ownership evidence | [invariants-ownership.md](invariants-ownership.md) |
| `INV-BIND-*` | Binding identity | [invariants-ownership.md](invariants-ownership.md) |
| `INV-TR-*` | Project Skill transitions | [invariants-skills.md](invariants-skills.md) |
| `INV-GLB-*` | Global Skills | [invariants-skills.md](invariants-skills.md) |
| `INV-INST-*` | Instructions | [invariants-instructions.md](invariants-instructions.md) |
| `INV-MEM-*` | Memory runtime visibility | [invariants-memory.md](invariants-memory.md) |
| `INV-CFG-*` | Structured config & file aggregation | [invariants-config.md](invariants-config.md) |
| `INV-SUB-*` | Subagents runtime configuration | [invariants-subagents.md](invariants-subagents.md) |
| `INV-MCP-*` | MCP servers runtime configuration | [invariants-mcp.md](invariants-mcp.md) |
| `INV-AUTH-*` | Authorization | [invariants-execution.md](invariants-execution.md) |
| `INV-TX-*` | Selection transactions | [invariants-execution.md](invariants-execution.md) |
| `INV-PEND-*` | Pending journals | [invariants-execution.md](invariants-execution.md) |
| `INV-REC-*` | Recovery | [invariants-execution.md](invariants-execution.md) |
| `INV-LOCK-*` | Writer locking | [invariants-execution.md](invariants-execution.md) |
| `INV-RES-*` | Result segmentation | [invariants-execution.md](invariants-execution.md) |
| `INV-API-*` | Public Python API | [invariants-api.md](invariants-api.md) |
| `INV-APP-*` | Application coordination | [invariants-application.md](invariants-application.md) |
| `INV-ADOPT-*` | Adoption engine | [invariants-adopt.md](invariants-adopt.md) |

---

## Document Map

| Document | Responsibility |
| --- | --- |
| `invariants.md` (this file) | Overview, status annotations, notation, Rule Registry, document map |
| [invariants-ownership.md](invariants-ownership.md) | Cross-resource ownership evidence and binding identity |
| [invariants-skills.md](invariants-skills.md) | Project Skills and Global Skills contract and state tables |
| [invariants-instructions.md](invariants-instructions.md) | Global and Project Instructions rules and state transitions |
| [invariants-memory.md](invariants-memory.md) | Memory Runtime invariants and state table |
| [invariants-config.md](invariants-config.md) | Common structured configuration targets, file aggregation, pre-images, and stale plan invalidation |
| [invariants-subagents.md](invariants-subagents.md) | Subagent prompt rendering, ownership markers, explicit force, prune, and availability |
| [invariants-mcp.md](invariants-mcp.md) | MCP server state management, fingerprints, same-file merge, sensitive config, and transaction recovery |
| [invariants-execution.md](invariants-execution.md) | Authorization, transactions, journals, recovery, locks, results |
| [invariants-api.md](invariants-api.md) | Public Python API invariants |
| [invariants-application.md](invariants-application.md) | Application layer coordination and workspace synchronization |
| [invariants-adopt.md](invariants-adopt.md) | Adoption engine, discovery, conflict resolution, and backups |
| [archive/core-model-migration.md](archive/core-model-migration.md) | Historical Core Model migration tracker (Phase 8 completed) |

---

## Migration History

See [archive/core-model-migration.md](archive/core-model-migration.md) for the
historical Core Model migration inventory completed in Phase 8.
