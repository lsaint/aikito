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

### INV-OWN-02: Evidence Evaluation Order `[planned]` {: #inv-own-02 }

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

### INV-OWN-03: Symlink Ownership Semantics `[planned]` (Current: Root-Level Scope Heuristic) {: #inv-own-03 }

- **Target Model (`[planned]`)**: A symlink entry is owned if and only if its literal target resolves to the canonical path of *this specific resource* (`canonical_root / resource_name`). A symlink pointing to an arbitrary workspace location or a different workspace skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`). Deletion acts strictly on the link entry, never on referenced content.
- **Current Behavior (`[compat-gap]`)**: `_symlink_points_within()` (`src/aikito/project.py:451`) checks `any(target.is_relative_to(root.resolve()) for root in roots)`. Because `roots` is the parent canonical directory `(workspace / "skills",)`, any symlink whose target resolves anywhere within the workspace skills directory (even pointing to another skill) is currently classified as `owned`. If that skill name is deselected, current cleanup unlinks it.
- **Broken Links**: A broken symlink is owned if and only if its literal target points specifically to the canonical path of *this specific resource* (`canonical_root / resource_name`), retaining proof of prior Aikito management and rendering it eligible for cleanup upon deselection. A broken symlink pointing to an arbitrary workspace location or another skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`).
- **Inspection Safety**: Canonical roots and intermediate parent directories must be real directories, not symlinks, junctions, or reparse points escaping the trusted workspace boundary. Symlinks inside copy sources or targets are rejected.

### INV-OWN-04: Copy Lifecycle and State Record Invariants `[planned]` {: #inv-own-04 }

Copy management records follow an explicit two-state lifecycle (`active` vs. `inactive`):

- **`active`**: The copy was successfully synchronized by Aikito and is currently selected. Eligible for automated updates when $R = B$. Baseline fingerprint $B$ reflects the last successful write or verified convergence, recording an origin of `write`, `reconcile`, `claim`, or `reactivate`.
- **`inactive`**: The copy was previously synchronized but is currently deselected. Historical baseline $B$ is retained solely for origin auditing and does not grant automated update or deletion rights.
- **Link Separation**: Symlink ownership is derived solely from live directory entry and symlink target facts; symlinks do not create or maintain active copy management records.
- **Deselection Protection**: Deselected copy skills are **never deleted or unlinked** by Aikito. Deselection marks the management record `inactive` and leaves target files completely untouched.
- **Corrupt / Missing Record**: Any corrupt, mismatched, or unreadable management record reverts to `UNKNOWN`. Aikito halts destructive actions and refuses automated updates; `--force` cannot bypass record corruption.

### INV-OWN-05: Directory Fingerprint Specification `[planned]` {: #inv-own-05 }

Directory fingerprints must capture the complete directory tree: file relative paths, file types (regular file, symlink, directory), byte contents, empty directories, and POSIX executable permission bits. File timestamps (`mtime`) and filesystem inode numbers are excluded from content equality comparisons, but may be used during preflight to detect concurrent entry replacement.

Any unsupported filesystem entry (FIFO, socket, device node) or internal symlink inside a copy source or target causes fingerprinting/inspection to fail and blocks synchronization.

---

## 3. State Transition Table

The following transition table governs synchronization planning for project skills.

| Rule ID | Status | Selection & Mode | Target & Evidence | Classification & Default Action | Post-State or Handling | Fixture ID |
| --- | --- | --- | --- | --- | --- | --- |
| `INV-TR-01` | `[current]` | Selected, link / copy | Target missing; canonical valid; state missing or present | `MISSING` &rarr; `CREATE` | Creates link or copy. For copy mode, writes active record $B := C$ (origin `write`). Link mode creates symlink without state record. | `FIX-TR-01` |
| `INV-TR-02` | `[planned]` | Selected, any | Canonical missing or unreadable | `BLOCK` (Planned) / Unlink-then-fail (Current) | Planned preflight halts execution without modifying target or state. Current link mode does not preflight canonical readability and deletes existing target before linking; copy mode fails during file read/copy. | `FIX-TR-02` |
| `INV-TR-03` | `[planned]` | Selected, link | Symlink accurately points to canonical resource | `OWNED` &rarr; `NOOP` (Planned) / Recreate (Current) | Planned behavior NOOPs. Current `sync_resource()` (`sync.py:46-63`) unconditionally removes and recreates the symlink. | `FIX-TR-03` |
| `INV-TR-04` | `[current]` | Selected, link | Broken symlink pointing to canonical (source missing) | `OWNED` + source missing &rarr; `BLOCK` | Refuses to treat broken link as an empty slot. | `FIX-TR-04` |
| `INV-TR-05` | `[current]` | Selected, link | Points to external/wrong resource; state missing | `FOREIGN` / `UNKNOWN` &rarr; `CONFLICT` | Refuses automatic relink. Sync aborted. | `FIX-TR-05` |
| `INV-TR-06` | `[planned]` | Selected, link | Previously managed, but link redirected elsewhere | `DRIFT` / `UNKNOWN` &rarr; `CONFLICT` | Stale record cannot override current directory entry fact. | `FIX-TR-06` |
| `INV-TR-07` | `[planned]` | Selected, copy | Valid active state, $R = B = C$ | `OWNED_UNCHANGED` &rarr; `NOOP` | No file or state writes. | `FIX-TR-07` |
| `INV-TR-08` | `[planned]` | Selected, copy | Valid active state, $R = B$, $C$ changed | `OWNED_UNCHANGED` &rarr; `UPDATE` | Copies new $C$; updates baseline $B := C$ (origin `write`). | `FIX-TR-08` |
| `INV-TR-09` | `[planned]` | Selected, copy | Valid active state, $R \ne B$ and $R \ne C$ | `OWNED_DRIFTED` &rarr; `CONFLICT` | Blocks sync. Explicit `--force` replaces target; updates $B := C$ (origin `write`). | `FIX-TR-09` |
| `INV-TR-10` | `[planned]` | Selected, copy | Valid active state, $R \ne B$ but $R = C$ | `RECONCILE_STATE` &rarr; `NOOP` on files | Verifies existing ownership; atomically updates $B := C$ (origin `reconcile`). | `FIX-TR-10` |
| `INV-TR-11` | `[planned]` | Selected, copy | No state record, $R = C$ | `UNKNOWN` &rarr; `NOOP` + Diagnostic | Does not manufacture ownership. `--force` authorizes `CLAIM_STATE` ($B := C$, origin `claim`). | `FIX-TR-11` |
| `INV-TR-12` | `[planned]` | Selected, copy | No state record, $R \ne C$, safe normal directory | `UNKNOWN` &rarr; `CONFLICT` (Default); `--force` Overwrites | Current `--force` replaces directory without writing state. Planned `--force` creates active state $B := C$ (origin `write`). | `FIX-TR-12` |
| `INV-TR-13` | `[planned]` | Selected, copy | State corrupt/mismatched, unreadable, or unsafe entry | `UNKNOWN` &rarr; `CONFLICT` | Current checks directory readability and entry type. Planned behavior checks state corruption; `--force` cannot bypass entry or record corruption. | `FIX-TR-13` |
| `INV-TR-14` | `[current]` | Deselected, actual link | Symlink points to workspace canonical resource | `OWNED` &rarr; `UNLINK` | Unlinks symlink entry only. | `FIX-TR-14` |
| `INV-TR-15` | `[current]` | Deselected, actual link | External symlink or insufficient evidence | `FOREIGN` / `UNKNOWN` &rarr; Preserve + Info | Leaves link untouched; informs user. | `FIX-TR-15` |
| `INV-TR-16` | `[planned]` | Deselected, actual copy | Valid active state, any $R$ / $B$ / $C$ relation | `DEACTIVATE_STATE` &rarr; Preserve target | Sets state to `inactive`; target directory preserved untouched. | `FIX-TR-16` |
| `INV-TR-17` | `[current]` | Deselected, actual copy | Inactive state or no state record | Preserve target &rarr; `NOOP` | Never deletes copy. Does not alter state. | `FIX-TR-17` |
| `INV-TR-18` | `[planned]` | Re-selected, copy | Inactive state, $R = C$ | `UNKNOWN` &rarr; `NOOP` + Diagnostic | `--force` authorizes `REACTIVATE_STATE` ($B := C$, origin `reactivate`). | `FIX-TR-18` |
| `INV-TR-19` | `[planned]` | Re-selected, copy | Inactive state, $R \ne C$ | `UNKNOWN` &rarr; `CONFLICT` | `--force` authorizes replacement and active baseline $B := C$ (origin `write`). | `FIX-TR-19` |
| `INV-TR-20` | `[planned]` | Selected, mode switch | Representation differs from desired mode | Mode switch plan | Link to copy: managed link replaced by copy ($B := C$, active). Copy to link: active copy with $R = B$ replaced by symlink (state inactive). Drifted, unmanaged, or inactive copy to link is blocked (`CONFLICT`); `--force` does not bypass. | `FIX-TR-20` |

### Detailed Lifecycle Rules

1. **Deselection Invariant**: Deselected copy skills are **always** preserved in the project checkout. Aikito never acquires deletion rights over copies through content matching or legacy selection records.
2. **State Transition Commitment**: State transitions occur atomically upon successful application. If writing target files succeeds but writing management state fails, sync reports failure and reverts to safe unmanaged evaluation.
3. **Reconciliation vs. Claim**:
   - `RECONCILE_STATE` applies strictly to pre-existing, valid, active management records where local contents converged with upstream canonical ($R = C$). It atomically advances $B := C$ without modifying runtime files.
   - `CLAIM_STATE` applies to legacy unmanaged directories where $R = C$. Ownership is not assumed; `--force` is required to claim active management.
   - Subsequent sync after reconciliation must be completely `NOOP`. If canonical subsequently changes to $C_2$, sync performs normal `UPDATE`, not a drift conflict.

---

## 4. `--force` Authorization Contract

### INV-AUTH-01: Project Skill Force Semantics `[planned]` {: #inv-auth-01 }

The `--force` option in `aikito sync project <name> [project_path] --force` authorizes:
1. Overwriting drifted copies where valid active state exists ($R \ne B$).
2. Overwriting inactive copies or unmanaged normal skill directories ($R \ne C$).
3. Explicitly claiming (`CLAIM_STATE`) or reactivating (`REACTIVATE_STATE`) management when $R = C$.

### INV-AUTH-02: Scoping Boundaries `[planned]` (Current: Project-Level Boolean Force) {: #inv-auth-02 }

- **Current Behavior (`[current]`)**: `--force` on `aikito sync project <name> [project_path] --force` is a single command-level boolean flag that permits overwriting all drifted copied skills within the target project checkout, without generating individual per-skill authorization tokens.
- **Target Model (`[planned]`)**: Every selected skill generates an independent authorization item.
  - **Explicit Checkout Path**: When `project_path` is passed, `--force` authorization applies strictly to that physical checkout.
  - **Omitted Path**: When `project_path` is omitted, `--force` applies only to currently accessible active checkouts on this host. Offline or missing candidate paths are never authorized.
  - **Granular Authorization**: Every selected skill generates an independent authorization item. Authorization does not cover arbitrary subdirectories or sibling files.

### INV-AUTH-03: Authorization Target Binding `[planned]` {: #inv-auth-03 }

Each authorization item binds seven attributes:
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

### INV-AUTH-04: Authorization Invalidation and Explicit CAS `[planned]` {: #inv-auth-04 }

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

## 5. Public Python API Invariants

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

## 6. Appendix: Migration Inventory

Every subsystem scheduled for migration into the structured Plan / Executor engine is documented below.

| Subsystem / Entry | Reads | Writes | Ownership Evidence | State File | Dry-run Behavior | Recovery Boundary | Target Engine | Old Implementation Deletion Criteria |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `cli.py::cmd_sync_all` | Workspace config, `agents.toml`, all projects, global resources, agent runtimes | Runtime global symlinks, project checkouts, MCP configs, subagents | Symlink pointing within workspace canonical roots; copy matching | None (delegates to subsystem sync handlers) | First runs complete `--dry-run` preview pass; if safe (`plan.can_apply` and not dry-run), immediately executes without interactive prompt | Partial failure leaves processed items intact; no rollback across subsystems | Unified Coordinator | Replaced when all underlying resource subsystems (project, global, MCP, subagent) migrate to unified Plan/Executor |
| `cli.py::cmd_global_sync` | Workspace `skills/`, `global/AGENTS.md`, `agents.toml` | Agent runtime instruction symlinks and global skills | Symlinks resolving within `workspace/skills`; `allow_matching_copies=True` for global skills cleanup | None | Read-only simulation (`--dry-run`) | Per-entry unlink/symlink; no transaction log | Global Engine | Replaced after global Plan/Executor contract verified |
| `cli.py::cmd_project_sync` | Project `agent.toml`, `AGENTS.md`, workspace skills, memory | Project checkout `.agents/skills/`, `.agents/memory/`, instructions | Symlink resolving to canonical roots; `allow_matching_copies=False` for project skills | None (planned executor introduces copy management records) | Read-only preview (`[DRY RUN CLEANUP]`, `[DRY RUN LINK]`, `[DRY RUN COPY]`) | Preflights conflicts before writing; runtime write failure leaves partial checkout files | Project Plan/Executor | Replaced when structured Executor handles project sync |
| `sync.py::sync_resource`, `apply_runtime_cleanup`, `sync_global_entry`; `compat.py::safe_symlink` | Source filesystem item, target filesystem item | Target symlink or copied directory tree; unlinks stale entries | `_symlink_points_within` verifies target points inside canonical roots; `safe_symlink` creates link via `symlink_to()` without prior target inspection; `sync_resource` deletes existing target before recreating | None | `dry_run=True` checks existence/paths and prints preview without filesystem mutation | Direct filesystem operations; `safe_symlink` wraps `symlink_to()` with OS error handling; no tempfile atomic swap; no rollback | Core Primitives (Project & Global) | Primitives adapted or replaced by structured Executor operations |
| `project.py::classify_project_skill_state`, `plan_runtime_cleanup`, `find_selected_runtime_conflicts` | Canonical skill directory, runtime checkout skill entry | None (pure query/classification functions) | `_symlink_points_within` (checks if target resolves within any canonical root); `_directories_match` when `allow_matching_copies=True` | None | Purely functional / read-only | Non-destructive query | Planner Analysis | Retired when planner evaluates management records and whole-tree fingerprints |
| `project_runtime.py::Project.prepare`, `sync_project_path`, `_resolve_project_sync_inputs` | Workspace config, `agents.toml`, project `agent.toml`, checkout directories | Checkout instructions, skills, memory | Symlink targets, copy directory comparisons | None | Supported via `dry_run` parameter in internal helpers | Validates conflicts before modifying persistent resources; write failure raises `ProjectPrepareConflictError` | Project Executor | Replaced when `prepare` delegates to structured project Plan/Executor |
| `mcp.py::sync_mcp_configs`, `sync_remove_mcp_from_agents`, `remove.py::remove_mcp` | Workspace `mcps/*.toml`, agent configuration files, `.local/state/aikito/mcp-state.json` | Agent configuration files (merged blocks), workspace `mcps/*.toml` on remove, updates `.local/state/aikito/mcp-state.json`, creates backup files | Recorded server entries in `.local/state/aikito/mcp-state.json` | `.local/state/aikito/mcp-state.json` (tracks applied server hashes per agent config) | Full read-only merge simulation; prints diff/actions without touching files or state | Timestamped backups created prior to writing; atomic state promotion via temporary state file and `os.replace`; restores backup on failure | Phase 4 (MCP Engine) | Replaced when MCP engine adopts unified Plan/Executor model |
| `subagent.py::build_plan`, `sync_subagent_configs`, `remove.py::remove_subagent` | Workspace `subagents/<name>.md`, `subagents.toml`, agent configuration files / subagent directories | Agent subagent prompt files / configs, workspace `subagents/<name>.md` and `subagents.toml` on remove | Generated prompt Aikito header banner / managed comment markers | None | Previews generated subagent plan items and actions | File-level backups for modified configs; atomic write via tempfile (`_write_file_atomic`) | Phase 4 (Subagent Engine) | Replaced when subagent engine adopts unified Plan/Executor model |
| `adopt.py::build_adopt_plan`, `execute_adoption` | Agent native configuration files, skills, MCP definitions | Workspace definitions (`skills/`, `mcps/`, `subagents/`, `projects/`, `config.toml`) | Native config presence; user approval via interactive/explicit plan | Timestamped backup directory `~/.aikito/backups/adopt_<timestamp>` | Complete read-only plan preview (`--dry-run`); calculates all changes and conflicts | Full preflight validation before any write; timestamped backups created | Phase 5 (Adoption Engine) | Preserved; integrates with unified Planner validation |
| `doctor.py::run_doctor`, `run_doctor_fixes` | Host environment, workspace, checkouts, runtime links, orphaned configs, `mcps/*.toml`, `.local/state/aikito/mcp-state.json` | Missing symlinks, repairs broken pointers (only when `--fix` is passed) | Cross-references workspace definitions against runtime entries and MCP state | Reads `.local/state/aikito/mcp-state.json` for drift checks; does not write state directly | Read-only inspection by default; `--fix` required to apply changes | Individual issue repairs; idempotent execution | Phase 5 (Unified Diagnostics) | Doctor rules updated to query unified state store and Plan/Executor diagnostics |
