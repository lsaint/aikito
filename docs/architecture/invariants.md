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
| `INV-AUTH-*` | Authorization | [invariants-execution.md](invariants-execution.md) |
| `INV-TX-*` | Selection transactions | [invariants-execution.md](invariants-execution.md) |
| `INV-PEND-*` | Pending journals | [invariants-execution.md](invariants-execution.md) |
| `INV-REC-*` | Recovery | [invariants-execution.md](invariants-execution.md) |
| `INV-LOCK-*` | Writer locking | [invariants-execution.md](invariants-execution.md) |
| `INV-RES-*` | Result segmentation | [invariants-execution.md](invariants-execution.md) |
| `INV-API-*` | Public Python API | [invariants-api.md](invariants-api.md) |

Phase 7 will extend this registry with `INV-MCP-*` and `INV-SUB-*` prefixes.

---

## Document Map

| Document | Responsibility |
| --- | --- |
| `invariants.md` (this file) | Overview, status annotations, notation, Rule Registry, document map |
| [invariants-ownership.md](invariants-ownership.md) | Cross-resource ownership evidence and binding identity |
| [invariants-skills.md](invariants-skills.md) | Project Skills and Global Skills contract and state tables |
| [invariants-instructions.md](invariants-instructions.md) | Global and Project Instructions rules and state transitions |
| [invariants-memory.md](invariants-memory.md) | Memory Runtime invariants and state table |
| [invariants-execution.md](invariants-execution.md) | Authorization, transactions, journals, recovery, locks, results |
| [invariants-api.md](invariants-api.md) | Public Python API invariants |
| [migration-inventory.md](migration-inventory.md) | Temporary Core Model migration tracker (not a permanent contract) |

---

## Migration Inventory

See [migration-inventory.md](migration-inventory.md) for the current Core Model
migration tracker. This document tracks subsystem migration progress across
Phases 1–8 and will be archived after Phase 8 is complete.
