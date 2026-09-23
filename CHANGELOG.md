# Changelog

All notable changes to Aikito will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Changed

- Internal: moved `AgentDefinition` and the canonical Agent loader (`load_agent_definitions`) from `aikito.mcp` to `aikito.agents`; `aikito.mcp.load_agents` and Agent-symbol re-exports from `aikito.mcp` were removed. User-visible CLI behavior is unchanged.
- Internal: `AgentDefinition` now models MCP support as a composed `MCPCapability` (`AgentDefinition.mcp`) instead of flat `mcp_*` fields; `supports_mcp` was removed. User-visible CLI behavior is unchanged.

## [1.50.0] - 2026-09-23

### Added

- Context-aware project detection across CLI commands: Running `aikito sync project`, `aikito edit instructions`, `aikito show project`, `aikito diff`, `aikito show memory`, `aikito maintain memory`, or `aikito add skill` from within any registered project path automatically targets that project without requiring explicit project arguments.
- Unified project status dashboard: Aligned table columns across `aikito status` and `aikito show projects` (`Project`, `Instr`, `Skills`, `Memory`, `Paths`, `Mode`, `Status`) and introduced a structured 3-line footer summarizing global resources, agent consumers, and workspace configuration.
- Added `Updated` timestamp column to `aikito show memory` indicating when memory files were last modified.

### Changed

- Updated Bash, Zsh, and PowerShell completion scripts to support context-aware command invocations.
- Refined project health status evaluation and instruction line calculation across status reports.

## [1.49.1] - 2026-09-23

### Fixed

- Normalized line endings (CRLF and LF) in bundled skill digest calculation (`directory_digest`), preventing false divergent notices and unexpected refresh prompts on Windows or environments where markdown templates use CRLF line endings.

### Changed

- Updated bundled `aikito` skill documentation to clarify reading named inbox notes via `aikito show inbox <name>.md` and listing notes via `aikito show inbox`.

## [1.49.0] - 2026-09-22

### Added

- Migrated workspace synchronization coordination and adoption engines to the structured application and coordination model (Core Model Phase 8), introducing `WorkspaceSyncPlan`, `WorkspaceSyncRequest`, `WorkspaceSyncExecutionResult`, `GlobalSyncPlan`, `BundledSkillRefreshPlan`, `AdoptRequest`, `AdoptPlan`, `AdoptFilePlan`, and `AdoptExecutionResult`.
- Formalized application architecture invariants `INV-APP-01` through `INV-APP-09`, adoption engine invariants `INV-ADOPT-01` through `INV-ADOPT-08`, and public API invariants `INV-API-08` through `INV-API-11`.
- Formal public Python API facade: Introduced strictly read-only `Workspace.load()`, `Workspace.inspect()`, and `Workspace.plan_sync()` with frozen models `WorkspaceInspection`, `WorkspaceSyncPreview`, and exception hierarchy `WorkspaceError`, `WorkspaceNotFoundError`, and `InvalidWorkspaceError`.
- Structured Adoption Engine: Adoption plans now evaluate explicit `AdoptFilePlan` entries with pre-image validation and zero-write guarantees against stale target or source files (`INV-ADOPT-04`, `INV-ADOPT-06`), structured backup reporting (`INV-ADOPT-05`, `INV-ADOPT-08`), and purely functional summary evaluation.
- Multi-Resource Workspace Coordinator: Unified `aikito sync` orchestration via `build_workspace_sync_plan` and `execute_workspace_sync_plan`, executing bundled skills refresh, global instructions, global skills, subagents, MCP servers, and project checkouts through a single coherent coordinator (`INV-APP-01`, `INV-APP-02`).
- Presentation Stream Independence: Completely eliminated stdout marker parsing (`_CHANGE_MARKERS`, `_WARNING_MARKERS`, etc.) for synchronization decisions; all plan decisions and dry-run summaries derive directly from structured plan properties (`INV-APP-03`).
- Unified Read-only Web Console: Connected Web Console inspection endpoints to shared application views and unified sensitive credential desensitization to `<redacted>` across console endpoints, Doctor diagnostics, and plan previews (`INV-APP-08`, `INV-APP-09`).

### Changed

- Retired legacy Subagent compatibility view (`PlanItem`, `build_plan()`) in `subagent.py`; aligned `doctor.py`, `diff.py`, and status matrix to evaluate subagents directly through structured `SubagentPlan`.
- Completed and archived Core Model Migration Inventory (`docs/architecture/archive/core-model-migration.md`) marking all planned subsystems as migrated.


## [1.48.0] - 2026-09-21

### Added

- Migrated subagent configuration and MCP server configuration engines to the structured configuration model (Core Model Phase 7), introducing `SubagentPlan`, `SubagentFilePlan`, `SubagentExecutionResult`, `MCPPlan`, `MCPFilePlan`, and `MCPExecutionResult`.
- Formalized structured configuration invariants `INV-CFG-01` through `INV-CFG-05`, subagent invariants `INV-SUB-01` through `INV-SUB-06`, and MCP invariants `INV-MCP-01` through `INV-MCP-08`.
- Multi-resource same-file aggregation: Multiple subagents in DSH `cordis.patch.yml` and multiple MCP servers in shared agent configuration files (e.g. `~/.claude.json`, `.config/opencode/opencode.jsonc`, `~/.codex/config.toml`) are chained and merged in-memory from a single pre-image and written exactly once, eliminating overwrites and race conditions.
- Stale plan detection: Runtime configuration file or state store modifications between preview/planning and execution halt execution without mutating files or state (`INV-CFG-04`, `INV-MCP-04`).
- Scoped subagent `--force`: `--force <agent>/<subagent>` authorizes overwriting only the specific targeted subagent file or block rather than granting broad file-level overwrite permissions (`INV-SUB-02`).
- Prune safety: `aikito sync subagents --prune` strictly prunes only subagent definitions containing an Aikito ownership marker, preserving unmanaged agent definitions (`INV-SUB-03`).
- MCP Desired Absent removal: `aikito rm mcp <name> --sync` operates via the unified MCP Planner and Executor (`Desired Absent`), reusing transactional file aggregation, backup, and state commit consistency (`INV-MCP-07`).
- Display redaction: Sensitive credential tokens and headers are redacted across all MCP plan previews, table displays, error outputs, and structured views (`INV-MCP-05`).
- Rollback and recovery assurance: If an error occurs during runtime file rollback or state recovery, backup files are strictly preserved and `recovery_required=True` provides clear manual recovery steps (`INV-MCP-08`).

### Changed

- Retired direct per-item write loops in `subagent.py` and `mcp.py` in favor of structured transactional executors (`execute_subagent_plan`, `execute_mcp_plan`).
- Subagent availability checks in `subagent.py` now reuse the canonical registry from `agents.py` rather than importing from `mcp.py`.
- Unified status, diff, and Doctor to evaluate subagent and MCP configuration state through unified inspection and plans.

## [1.47.0] - 2026-09-21

### Added

- Migrated project memory runtime visibility (`.agents/memory/` links for workspace memory references and project `notes/`) to the unified Target → Inspect → Plan → Execute model (Core Model Phase 6), introducing `MemoryResource`, `MemoryBatch`, `MemoryPlan`, and `MemoryExecutionResult`.
- Formalized project memory engineering invariants `INV-MEM-01` through `INV-MEM-12` covering pure link-only semantics, canonical notes source precedence, exact canonical ownership, conflict preservation of unmanaged files and external symlinks, and segmented execution results.
- Added structured memory execution results to `ProjectSyncExecutionResult.memory_result`, fully isolating memory synchronization outcomes from skills and instructions.
- Unified project diagnostics, project summary, status rows, and `Project.prepare()` to evaluate memory link status through `MemoryPlan`, eliminating diverging symlink status heuristics.

### Changed

- Retired and deleted legacy synchronization primitives `sync_resource()` and `apply_runtime_cleanup()`, routing all project memory filesystem modifications through the unified link executor.
- Deleted `LegacySyncResult` dataclass and removed `legacy_results` from `ProjectSyncExecutionResult`.
- Deleted obsolete preflight helpers `_ProjectSyncInputs`, `_resolve_project_sync_inputs()`, and `collect_project_prepare_errors()` from `project_runtime.py`.
- Replaced unconditional overwrite of unmanaged files/directories at memory target paths with safe `CONFLICT` preservation (`INV-MEM-07`).

### Fixed

- Validated state store root directory and raised actionable errors on validation failure during `SkillWriterLock.acquire()`.
- Hardened multi-threaded skill writer lock serialization tests against thread scheduling latency and ensured guaranteed thread cleanup.

## [1.46.0] - 2026-09-21

### Added

- Unified global and project instruction synchronization under the Target → Inspect → Plan → Execute architecture (Core Model Phase 5), introducing `InstructionBatch`, `InstructionPlan`, and `InstructionExecutionResult`.
- Formalized instruction engineering invariants `INV-INST-01` through `INV-INST-15` covering pure link-only semantics, exact canonical ownership, same-object non-management, multi-agent consumer deduplication, empty canonical cleanup, and segmented execution results.
- Added multi-agent instruction target deduplication via `resolve_targets("global_instructions")` and `resolve_targets("project_instructions")`, collapsing shared agent instruction targets (such as `AGENTS.md`) into a single physical link operation while preserving consumer tracking.
- Added structured instruction execution results to `GlobalSyncResult.instruction_result` and `ProjectSyncExecutionResult.instruction_result`, isolating instruction failures from skills and memory.
- Unified Doctor, status, project summary, and `Project.prepare()` to rely on a single canonical instruction inspection and planning layer, removing duplicate symlink classification paths.

### Changed

- Replaced and removed ad-hoc `sync_global_entry()` and `sync_project_instruction()` in favor of atomic link operations (`LinkOperation`).
- Empty project canonical `AGENTS.md` safely removes only exact owned symlinks, strictly preserving project-owned regular files and unmanaged symlinks.
- Legacy Grok instruction paths and `.agents/AGENTS.md` cleanup are fully integrated into `InstructionPlan` and require exact canonical ownership proof.

## [1.45.0] - 2026-09-21

### Added

- Migrated global skills to the unified Target → Inspect → Plan → Execute model (Core Model Phase 4), introducing `GlobalSkillBatch`, three-tier planning (managed container `~/.agents/skills`, managed entries, and consumer links), and `GlobalSkillExecutionResult`.
- Formalized global skill invariants `INV-GLB-01` through `INV-GLB-09` covering canonical ownership, link-only baseline avoidance, non-destructive container management, matching directory conflict preservation, cross-workspace isolation, and execution segmentation.
- Serialized bundled skills refresh and global skill link application under a single outer `SkillWriterLock`, verifying canonical refresh completion before mutating runtime links.
- Unified Doctor and status global skill health reporting to consume `GlobalSkillBatchPlan`, guaranteeing identical target, conflict, and NOOP evaluations across read-only and mutating commands.
- Upgraded global skill idempotency assertions across repeat synchronizations to guarantee zero-write preservation of symlink inodes, `mtime_ns`, and targets.

### Changed

- Retired `sync_resource()` and `sync_global_entry()` invocations from global skills management, routing all destructive filesystem operations through the unified link executor (`apply_link_operation`).
- Transitioned unexpected or external consumer symlinks from destructive silent relinking to explicit conflicts (`INV-GLB-05`).

## [1.44.2] - 2026-09-20

### Added

- Added automated GitHub Actions release workflow (`release.yml`) orchestrating release gate validation, atomic tagging, GitHub release notes extraction, PyPI publishing, and downstream Homebrew tap dispatch.
- Added `workflow_call` support to `publish-pypi.yml` for reusable CD pipeline execution.

## [1.44.1] - 2026-09-20

### Added

- Extracted clean, read-only `Agent`, `AgentRegistry`, and `AgentAvailability` models into `aikito.agents`, eliminating ad-hoc fallback heuristics across synchronization and diagnostics.
- Introduced `Target` model with `is_same_object` evaluation and deduplication in `resolve_targets()`, collapsing 8 bundled agent consumers into 3 physical filesystem operations.
- Partitioned batch project synchronization execution results into isolated segments (`ProjectSyncExecutionResult`), preserving committed skill synchronization results against downstream legacy failures.
- Cataloged engineering invariant rule groups for selection transactions (`INV-TX-01..04`), transaction journals (`INV-PEND-01..03`), recovery passes (`INV-REC-01..04`), writer locks (`INV-LOCK-01..03`), segmented execution results (`INV-RES-01..02`), and global binding identity (`INV-BIND-01..03`).

### Changed

- Tightened symlink ownership verification to exact canonical targets (`canonical_root / name`), preserving cross-skill and cross-resource links upon deselection.
- Converged `classify_project_skill_state()` to a thin read-only wrapper around `inspect_skill_target()` and `plan_single_skill()`, eliminating redundant heuristic state classification.
- Held `SkillWriterLock` during bundled skill and init template refreshes, preventing race conditions with concurrent operations.
- Aligned synchronization output and diagnostic reports to distinguish logical resource counts, agent consumers, and physical target operations.
- Made `SkillWriterLock` thread-aware and kept composite `add skill --sync` workflows under one outer writer lock.
- Deduplicated Agent targets by physical filesystem identity, including symlink aliases and case-insensitive paths, and unified Doctor target inspection with synchronization.

## [1.44.0] - 2026-09-19

### Added

- Implemented project skill synchronization engine supporting both symlink (`link`) and directory snapshot (`copy`) deployment modes.
- Introduced transactional skill state management with atomic staging, generation tracking, compare-and-swap (CAS) verification, and crash recovery journals.
- Added platform-specific path compatibility and atomic replacement primitives across macOS, Linux, and Windows.
- Expanded CI test matrix with cross-platform smoke test assertions for project skill synchronization.

### Changed

- Integrated project skill synchronization into `aikito sync`, `aikito add skill`, and `aikito rm skill` workflows.
- Formalized skill synchronization and transactional state invariants in core model architecture documentation.

## [1.43.0] - 2026-09-18

### Added

- Supported external MCP server configuration ingestion via `aikito add mcp [name] --from <path>`, with automatic format detection across JSON, TOML, and YAML formats.
- Added `--sync` flag to `aikito add mcp` for immediate runtime agent configuration deployment upon server creation.
- Added `--force` flag to `aikito add mcp` to safely overwrite and update existing canonical MCP server definitions.
- Enabled Mermaid diagram rendering support in documentation.

### Changed

- Hardened `aikito add mcp` transactional safety with atomic state promotion, automatic rollback on write or state failure, and empty file preservation.
- Sequentially chained configuration updates when adding multiple MCP servers to shared agent configurations to prevent overwrite conflicts.
- Refined URL credential and sensitive attribute detection to avoid false positives on non-credential fields while strictly sanitizing credentials and suppressing sensitive file backups.
- Excluded agent JSON configs (`agy_json`, `claude_json`) from whole-file backups to prevent leaking credentials.
- Standardized global memory scope naming to `Global` in status output and documentation.

## [1.42.0] - 2026-09-18

### Added

- Enhanced `aikito add subagent` to support external ingestion with `--from <path>`, automatic frontmatter parsing (name, description, platform options), `--sync` for immediate agent runtime deployment, and `--force` for transactional snapshot overwrites.
- Added `aikito rm subagent <name> [--sync]` command to safely remove canonical subagents, unregister them from workspace configuration, and optionally prune rendered definitions across configured Agent runtimes.
- Added `aikito rm mcp <name> [--sync] [--force]` command to safely remove canonical MCP server configurations (`mcps/<name>.toml`) and optionally unregister the server from target Agent configurations with fingerprint conflict protection.
- Added shell completion support for `aikito rm subagent` and `aikito rm mcp` across Bash, Zsh, Fish, and PowerShell.

## [1.41.0] - 2026-09-17

### Added

- Added `aikito rm skill [name]` command to remove skill definitions and unregister them from workspace configuration or specific projects (`--project p1,p2`).
- Supported updating and refreshing existing imported skills from external directories or markdown sources using `aikito add skill --from <path> --force`.
- Protected bundled system skills (`aikito`, `durable-memory`) from accidental deletion or overwriting.
- Added shell completion support for `aikito rm skill` across Bash, Zsh, Fish, and PowerShell.

### Changed

- Aligned CLI output messaging, error hints, and terminology with product standards across `add`, `remove`, `status`, and project runtime commands.

## [1.40.0] - 2026-09-17

### Added

- Added CLI update checking to `aikito version` with `--check` (`-c`), `--force`, and `--json` flags to check for upstream releases and output structured version metadata.
- Added lightweight, non-blocking background update notifications with a 24-hour local cache.
- Supported configuring or suppressing update checks via `[update] check = false` in workspace `config.toml` or `AIKITO_NO_UPDATE_NOTIFIER` / `NO_UPDATE_NOTIFIER` environment variables.

## [1.39.0] - 2026-09-17

### Added

- Supported importing external skill directories or markdown files via `aikito add skill [name] --from <path>`, with automatic name and description inference from YAML frontmatter.
- Supported distributing skills across multiple projects using comma-separated project names (`--project p1,p2`) and optional immediate synchronization (`--sync`).
- Supported synchronizing multiple projects in a single invocation via comma-separated project names in `aikito sync project`.

### Fixed

- Robustly parsed markdown YAML frontmatter across BOM markers, multi-line values, and horizontal rule separators in skill and agent documentation.

## [1.38.0] - 2026-09-17

### Added

- Supported `builtin_mcps` in agent MCP configurations, allowing `aikito adopt` to safely skip agent-bundled MCP servers unless shared across multiple agents.

### Fixed

- Canonicalized hyphen and underscore naming variants for MCP servers during `aikito adopt` to prevent duplicate or conflicting configurations across agents.
- Compared MCP server configurations by target URL in `aikito adopt` to accurately detect existing server definitions and avoid redundant imports.

## [1.37.0] - 2026-09-16

### Changed

- Treat the bundled `aikito` and `durable-memory` skills as system-managed snapshots: inspection commands report divergence, while workspace initialization and global synchronization back up and refresh them from the installed package.

## [1.36.1] - 2026-09-16

### Changed

- Streamlined the bundled `aikito` guidance skill by extracting detailed workflows into dedicated reference documents (`references/adoption.md`, `references/installation.md`, and `references/projects.md`).
- Extended workspace template bundling and integrity verification to package and deploy skill references alongside core skill definitions.
- Refocused documentation onboarding on existing agent setups with refined styling and navigation.

## [1.36.0] - 2026-09-16

### Added

- Added preflight sync planning (`aikito sync --dry-run`) with concise progress metrics (`Changes`, `Unchanged`, `Offline`), detailed logging via `--verbose`, and non-zero exit when blocked by unmanaged targets or conflicts.
- Added selective resource adoption and preflight previews in `aikito adopt` with `--dry-run`, `--verbose`, and repeatable `--skip RESOURCE` flags (`instructions`, `mcp/<name>`, `subagent/<name>`).
- Added an `Adoption` diagnostics section in `aikito doctor` that flags conflicting or invalid existing agent configurations with actionable remediation commands.
- Enhanced `aikito init workspace` with contextual next-step recommendations and clean `[CONNECTED]` status reporting when attaching to existing workspaces.
- Redesigned documentation homepage with custom layout, refined copy, self-hosted fonts, responsive header alignment, and OS-aware installation instructions.

### Changed

- Full workspace sync (`aikito sync`) now evaluates a preflight sync plan before mutating filesystem state, aborting safely if conflicts are detected.

## [1.35.0] - 2026-09-14

### Added

- Skipped provisioning dedicated instruction symlinks for uninstalled agents when their parent directory does not exist, avoiding cluttering project trees with unused agent directories.
- Refined `aikito doctor` environment diagnostics to report detected agent CLIs as OK without warning on uninstalled ones, issuing a single warning only when no supported agent CLIs are found in `$PATH`.

### Fixed

- Marked project memory symlink status as `OFFLINE` in `aikito show memory` when project directories are inaccessible or not present on the current host.
- Auto-registered PowerShell `aikito` alias pointing to the detected executable format (`aikito.exe`, `aikito.cmd`, or `aikito.ps1`) in generated completion scripts.

## [1.34.0] - 2026-09-13

### Added

- Added Git conflict marker detection (`<<<<<<<`, `=======`, `>>>>>>>`) in `aikito doctor` across memory notes and workspace TOML configuration files.
- Added pre-flight Git conflict marker checks before `Project.prepare()` and `aikito sync project` to prevent synchronizing conflicted files into runtime.
- Added actionable `Fix:` guidance in `aikito show project` for conflicts, copied skill drifts, and runtime sync discrepancies.

### Changed

- Consolidated multi-project synchronization hints in `aikito doctor` into a single `→ aikito sync` action at the bottom of failing projects.
- Aligned project memory runtime expectations in `collect_project_summaries` with `sync_project_path` to only expect `notes/` and configured memory references, ignoring documentation files like `README.md`.
- Added descriptive reason reporting in `classify_project_skill_state` when copied project skills contain drift.

## [1.33.0] - 2026-09-12

### Added

- Added an `aikito doctor` warning for subdirectories under Memory `notes/`, which are intentionally not scanned.

### Changed

- Made `notes/*.md` the sole durable Memory source, removed generated indexes and rebuild behavior, and made `category` optional. Existing `memory/index.md` files are preserved but ignored.

## [1.32.0] - 2026-09-11

### Added

- Bundled `marked.umd.js` into the Web Console package distribution for complete GitHub Flavored Markdown (GFM) rendering, including tables, task lists, and `[[target|label]]` wikilinks.

### Changed

- Modernized the Web Console UI inspired by the documentation theme: warm paper / basalt palette, frosted glass headers, pill badges, and refined typography scales.
- Updated documentation screenshots with the modern Web Console UI.

## [1.31.1] - 2026-09-10

### Changed

- `Project.prepare()` now supports any agent configured in the workspace's `agents.toml` instead of restricting runtime preparation to Pi.
- Unconfigured agents now return a descriptive `UnsupportedProjectAgentError`.

## [1.31.0] - 2026-09-10

### Added

- Added the public `Project.load()`, `Project.prepare()`, and `Project.add_path()` APIs and typed project runtime errors for Pi-based project runners.
- Added `aikito show memory --project [<name>]` to scope memory listing and note lookup to one project (`.` or a bare flag resolves the current directory's project, `global` selects global memory), including zsh, bash, fish, and PowerShell completion.

### Changed

- `aikito sync project` now fails preflight when a selected project skill or memory source is missing, instead of warning and continuing in `link` mode.
- Command help now prints the description before the usage line, and subcommands without an explicit description reuse their `help` text.

## [1.30.1] - 2026-09-09

### Changed

- Renamed internal runtime modules under `src/aikito/` to eliminate redundant `aikito_` prefix and normalize module names (`templating.py`, `web_console.py`, `compat.py`, `link.py`, etc.).
- Synchronized all internal imports, mock patches, and test file naming across the entire test suite.

## [1.30.0] - 2026-09-09

### Removed

- Removed legacy `bin/` directory (`bin/aikito`, `bin/aikito.cmd`, `bin/aikito.ps1`). Aikito is now invoked via `python -m aikito` or the installed console script `aikito`.

### Changed

- Updated Windows `install.ps1` installer to install via `uv tool` or an isolated Python virtual environment, eliminating reliance on `bin/` stubs and zipball extraction.
- Documentation now highlights `uv tool install aikito` as the primary cross-platform installation method.
- Updated PowerShell completion generator command targets to reflect the removal of `bin/` stubs.

## [1.29.0] - 2026-09-09

### Added

- `python -m aikito` entry point via `src/aikito/__main__.py`.
- `aikito` pip-installable console script via `pyproject.toml` `[project.scripts]`.
- Wheel build support: `uv build` / `python -m build` produce a fully self-contained wheel with bundled templates and web assets.

### Changed

- Package source tree moved from flat `bin/` layout to `src/aikito/` package layout.
  All runtime modules are now importable as `aikito.*`.
- Bundled templates moved from top-level `templates/` to `src/aikito/templates/`.
  External links pointing to `blob/main/templates/skills/...` are updated to
  `blob/main/src/aikito/templates/skills/...`.
- Bundled web assets moved from top-level `web/` to `src/aikito/web/`.
- `aikito doctor` interpreter-consistency check now reports OK and skips the
  shebang hint when running inside a virtual environment (pip / pipx / uv install).
- Version constant `__version__` is now the sole source of truth in
  `src/aikito/__init__.py`; `pyproject.toml` reads it dynamically via hatchling.

### Deprecated

- Invoking aikito via `bin/aikito` is deprecated and will be removed in v1.30.0.
  Use `python -m aikito` or the `aikito` console script installed by pip/uv.

## [1.28.0] - 2026-09-08

### Changed

- Dropped the project primary-path concept. `show project` now reports
  `Canonical path` and lists every candidate under `Project paths`, for example
  `[1]✓ ~/code/example, [2]- ~/code/example-worktree`. The status Path column uses the same
  markers.
- `aikito maintain memory` resolves the Agent workdir from active project paths:
  `.` matches any local candidate; a named project uses the candidate containing
  the current directory, or its only local path, and requires you to run it from
  one of them when several exist.

## [1.27.0] - 2026-09-07

### Added

- Show per-section progress on `aikito doctor` when stdout is a TTY.

### Changed

- Clarified the bundled Aikito skill: Aikito governs resource ownership, scope,
  synchronization, and structural integrity; ordinary file edits can be
  performed directly by Agents.

## [1.26.0] - 2026-09-06

### Changed

- Memory tab completion now always uses `scope/stem` identifiers so notes can be filtered by project.

## [1.25.1] - 2026-09-06

### Added

- Display status legend under individual component and resource tables across `status` and `show` views whenever warning or error badges (`M`, `C`, `D`, `E`) are present.

### Fixed

- Handled non-string path objects in `render_projects_table` and generic table column width calculation to prevent `TypeError`.

### Changed

- Clarified workspace onboarding and bare `aikito sync` recommendations across documentation and `install.ps1`.

## [1.25.0] - 2026-09-05

### Added

- Whole-workspace synchronization via bare `aikito sync [--dry-run]`, automatically orchestrating global resources, host-gated subagents, MCP configurations, and active projects.
- Host-gating in subagents engine: uninstalled/offline agents on the local machine are cleanly skipped (`SKIP`), preventing false-positive drift, aborts, and orphan deletion.
- `OFFLINE` project status: projects with candidate paths on other hosts but none locally are gracefully handled as offline (dimmed badge in status, OK in doctor) rather than failures.
- Single-point empty config file fault tolerance in `aikito doctor`: 0-byte or empty native configuration files produce an actionable warning instead of crashing JSON parser.

### Changed

- Converted missing credential checks in `sync_mcp_configs` from a fatal pre-flight abort into a per-spec warning skip, continuing sync for remaining servers.
- Removed `aikito doctor --prune` to protect `agents.toml` from removing offline agents in multi-host Git SoT environments.
- Fixed `aikito diff` and project skill collection to ignore offline and unbound projects in `sync_mode = "copy"`.
- Updated `aikito init workspace` to recommend running `aikito sync` for host configuration without touching Agent-native runtimes.

## [1.24.1] - 2026-09-04

### Fixed

- Grok MCP synchronization now writes native `headers` with `${ENV}` interpolation
  instead of Codex `env_http_headers` for Basic API-token authentication.
- Windows `install.ps1` copies extracted files instead of using `Move-Item`, avoiding
  file-locking failures from antivirus or indexer scans during install.

## [1.24.0] - 2026-09-03

### Added

- Multi-path and multi-active project targets: `agent.toml` supports candidate paths
  via a single `path`, a list of `paths`, or a named `[paths]` table for cross-platform roaming.
- Dynamic multi-active project synchronization: `aikito sync project <name>` automatically
  synchronizes instructions, skills, and memory across all active paths detected on the
  local filesystem (e.g. multiple Git worktrees).
- Fail-fast preflight validation across all active project paths before modifying links or files.
- Non-fatal offline paths in `agent.toml`: missing candidate paths on the local machine
  are safely preserved without reporting errors in `aikito status` or `aikito doctor`.
- Model configuration support for Antigravity CLI subagents (`[subagents.<name>.agy].model`).

### Changed

- Documented `-InstallDir` parameter usage for the PowerShell installer `install.ps1`.

### Fixed

- Handled known workspace, configuration, and conflict exceptions cleanly in the CLI
  entrypoint without displaying raw tracebacks unless `--debug` or `AIKITO_DEBUG=1` is set.
- Improved PowerShell command line AST element resolution, auto-injected alias fallback,
  and added a runnable one-liner to append completions in `install.ps1`.
- Resolved temporary test directory symlink paths and 8.3 short names on Windows to prevent
  assertion mismatches.

## [1.23.0] - 2026-09-02

### Added

- Native Windows and PowerShell support with dedicated entry point wrappers
  (`bin/aikito.cmd`, `bin/aikito.ps1`).
- Native PowerShell dynamic shell completion script generator accessible via
  `aikito completion powershell`.
- Cross-platform platform layer (`bin/aikito_platform.py`) with `require_symlink_support()`,
  Windows Developer Mode detection, console UTF-8 initialization, and executable resolution.
- Active NTFS Access Control List (`icacls`) hardening for secret-bearing
  configuration files on Windows, stripping inherited group permissions.
- Windows CI test matrix on Python 3.12, 3.13, and 3.14 on `windows-latest`
  alongside dedicated PowerShell smoke testing.
- `install.ps1` — one-liner Windows installer (`irm … | iex`) that validates
  Python 3.12+, confirms Developer Mode / symlink support, downloads the latest
  release from GitHub Releases, installs to `%LOCALAPPDATA%\Programs\aikito`,
  and adds `bin\` to the current user's `PATH`.


### Changed

- Updated documentation (`docs/safety.md`, `docs/comparison.md`) to reflect
  native Windows support, Developer Mode prerequisites, and NTFS credential
  security models.


## [1.22.0] - 2026-09-01


### Added

- `aikito show mcp --live` now discovers and renders live tool counts in the
  global matrix view, and `aikito show mcp <server> --live` compares remote
  connectivity, configured authentication methods, and read-only `tools/list`
  counts across Agent-native configurations; narrowing with `--agent` also
  prints tool names.

### Fixed

- Centralized live MCP probe error redaction and refused credential-bearing
  plaintext HTTP requests to non-loopback endpoints.

## [1.21.0] - 2026-08-31

### Added

- Added optional project descriptions through `aikito init project --description`,
  surfaced in project summaries and detail views.

## [1.20.1] - 2026-08-29

### Changed

- Isolated the bundled workspace templates and skills under `templates/` so
  source checkouts no longer contain workspace-shaped runtime files; packaged
  installations continue to initialize complete workspaces and serve the Web
  Console.

## [1.20.0] - 2026-08-28

### Added

- `aikito status` memory table gains a combined `Status` column covering
  canonical `index.md` presence and runtime connection health, with a legend
  explaining any warning symbols, and an `Updated` column showing the most
  recent memory update date (today, yesterday, or the date itself) in place
  of the runtime location.

### Changed

- `show mcp`, `show subagents`, and single-agent detail views render their
  fields as aligned key-value blocks for consistent, readable output.

### Fixed

- `doctor` no longer suggests a destructive `rm -rf` command for empty orphan
  skill directories; it now points at the directory for manual review.
- Web Console `/api/overview` serializes memory update dates as ISO strings
  instead of failing with a JSON serialization error.

## [1.19.0] - 2026-08-28

### Added

- Added Pi global and project instructions, shared skills, and headless
  runner registration. Pi does not participate in MCP or subagent
  synchronization, which pi leaves to optional extensions.

### Fixed

- Detect installed Agents during synchronization with the canonical registry
  check (binary on `$PATH` or home-relative marker directory) instead of the
  config parent directory, and create missing config directories for detected
  Agents. Fixes Grok global instructions being skipped forever when the grok
  CLI had not yet created `~/.grok/rules`.

### Changed

- `aikito status` distinguishes agents that do not participate in MCP or
  subagent synchronization (`–`) from participating agents with nothing
  synchronized (`0`), and renders synced counts as bare numbers.

## [1.18.1] - 2026-08-27

### Fixed

- Allow workspaces with an intentionally empty Agent registry to initialize,
  inspect, and synchronize safely.
- Render project memory diagnostics deterministically and report each resource
  issue on its own line.

## [1.18.0] - 2026-08-27

### Added

- Added conflict-safe project instruction links for agents registered in the
  workspace root `agents.toml`.
- Added Agent registry schema and installed-Agent diagnostics with additive
  `doctor --fix` migration.
- Added project runtime diagnostics to `doctor`, sharing the same missing,
  drift, and conflict model as `show project`.
- Added Grok Build global and project instructions, shared skills, MCP,
  subagents, installed-Agent diagnostics, and headless runner registration.

### Changed

- Removed the unused project `.agents/AGENTS.md` runtime link; project
  instructions now use only agent-native discovery paths.
- Workspace initialization now registers only locally detected Agents, and
  project `agent.toml` files no longer duplicate the Agent list.
- Project doctor findings are aggregated by project so conflicts suppress
  unsafe synchronization hints.
- Empty canonical project instructions no longer create native links; managed
  legacy links are cleaned while project-owned `AGENTS.md` files are preserved.
- Project-owned unselected skills now coexist with Aikito-managed skills;
  conflicts are limited to selected skill names.
- Project detail output now renders each concrete resource issue on its own line.
- Matching directory contents no longer imply Aikito ownership for project
  skill copies; unselected directories are preserved as project-owned notices.

## [1.17.0] - 2026-08-26

### Added

- Added an agent-first workflow guide for safely installing, initializing, and
  operating Aikito with a coding agent.
- Added the resolved workspace path and its source to `aikito status`.

### Changed

- Simplified the `aikito status` dashboard by removing the memory section
  heading while preserving its resource table.

## [1.16.0] - 2026-08-25

### Added

- Added OpenCode subagent synchronization through native agent Markdown files,
  including per-subagent model selection.

## [1.15.1] - 2026-08-25

### Fixed

- Fixed Homebrew installations of the Web Console by including its static
  assets and adding end-to-end CI coverage for the homepage and overview API.

## [1.15.0] - 2026-08-24

### Added

- Added an agent-assisted Quick Start option that delegates safe installation,
  workspace initialization, synchronization, verification, and next-step
  guidance to a coding agent.

### Changed

- New workspaces now install and enable the bundled `aikito` skill alongside
  `durable-memory`.

## [1.14.0] - 2026-08-24

### Changed

- New workspaces now enable the bundled `durable-memory` workflow by default,
  while preserving explicit synchronization and conflict-safe instruction
  adoption.

## [1.13.0] - 2026-08-24

### Added

- Added `aikito path workspace` for machine-readable active workspace resolution.

### Changed

- Persist explicit `aikito init workspace <path>` selections while preserving
  `AIKITO_DIR` as the highest-priority temporary override.
- Made the Inbox default workspace-relative and updated skills and documentation
  to avoid treating `~/aikito` as the only canonical workspace path.

## [1.12.0] - 2026-08-21

### Added

- Added `aikito edit inbox <target>` to open an inbox note in the configured external editor.
- Added `aikito rm inbox <target>` and `aikito remove inbox <target>` to delete processed or obsolete inbox notes.

### Changed

- Updated Web Console scrollbars to transient pill scrollbars with hidden tracks that only display while scrolling.
- Added draggable splitters to resize left and right sidebars in the Web Console with persisted width preferences.
- Added support for standard Markdown links `[label](target)` in the Web Console, rendering only the label, displaying the target on hover, and opening HTTP(S) links in a new tab.
- Increased Web Console content typography scale by 1px for improved readability.

## [1.11.0] - 2026-08-20

### Added

- Added a stdlib-only, local, read-only Aikito Web Console.

## [1.10.0] - 2026-08-19

### Added

- Added native DeepSeek Harness (`dsh`) agent support across global instructions (`.dsh/AGENTS.md`), canonical skills (`.agents/skills`), MCP configuration (`.dsh/cordis.patch.yml` via `dsh_cordis` renderer), subagents (`.dsh/.agent-presets/<name>/` via `dsh_preset` renderer), and runner (`dsh --profile headless`).
- Added full diagnostic checks and CLI status checking for DeepSeek Harness in `aikito doctor` and `aikito status`.

### Fixed

- Allowed project operations when the target workspace is at the CLI source root.

## [1.9.0] - 2026-08-18

### Added

- Added `aikito show inbox [target]` to list staged Markdown notes or print a
  selected note by exact name or unique prefix.
- Added configurable Inbox paths through `[inbox].path` in the workspace
  `config.toml`.
- Added dynamic shell completion for Inbox note targets across Zsh, Bash, and
  Fish.
- Added standalone English and Simplified Chinese guides covering the Inbox
  lifecycle, its trust boundary, and how reviewed notes become durable memory.

### Changed

- Removed the unused `memory = []` field from newly generated project
  configuration files.

## [1.8.0] - 2026-08-17

### Added

- Added `aikito maintain memory [global|<project>|.]` to launch a configured interactive Agent for confirmation-gated, full-scope memory maintenance.
- Added interactive runner definitions for Codex, Claude Code, Antigravity CLI, OpenCode, and GitHub Copilot CLI.
- Added per-runner environment overrides through `[agents.<name>.runner.env]`, with inherited process environment and prompt placeholders.

### Changed

- Extended proactive memory maintenance to compare notes with relevant skills and instructions, report upstream corrections separately, and defer unverifiable conflicts to the user.

### Fixed

- Added focused diagnostics for malformed runner placeholders, unknown Agents, invalid runner configuration, and registered projects without a memory scope.

### Security

- Added open-source export sanitization for all Agent runner environment values, including proxies, API keys, and tokens.

## [1.7.0] - 2026-08-16

### Added

- Added `aikito rename memory <target> <new-name>` to atomically rename a note, update its `index.md` entry, and refactor all inbound `[[wikilinks]]` within its scope.
- Added `aikito rm memory <target>` (and `aikito remove memory`) to delete a note, prune its `index.md` entry, and scan for inbound `[[wikilinks]]` within its scope.
- Added `aikito doctor --fix` for safe automated reconciliation of memory index files (prunes dangling dead links and normalizes entries to `[[stem|Title]]` using note heading titles).
- Added note filename validity checking (kebab-case alphanumeric, $\le 50$ chars) and index entry format validation in `aikito doctor`.
- Added `--agent` flag to `aikito show subagents [target] [--agent agent]` to display per-agent subagent overview tables and detail cards showing active platform options (`model`, `effort`, etc.) and explicit non-targeted status.
- Added dynamic shell completion candidate support for `rename memory` and `rm memory` across Zsh, Bash, and Fish.

### Fixed

- Fixed `cmd_show_subagents` exception handling to gracefully report unknown agent and subagent errors without tracebacks.
- Added guards in memory operations to prevent accidental modification or removal of `index.md` and non-note files.
- Isolated inbound wikilink refactoring and scanning during note rename and removal strictly to the note's owning scope.
- Prevented `doctor --fix` from blindly appending unindexed notes to preserve the curated category structure of `index.md`.

## [1.6.0] - 2026-08-15

### Added

- Added `aikito add` command family (`aikito add skill`, `aikito add subagent`, `aikito add mcp`) for creating minimal valid canonical resource skeletons with automatic registration.
- Added support for project-scoped skill creation and registration via `aikito add skill <name> --project <project>`.
- Added shell completion candidate support for `add` subcommands and options across Zsh, Bash, and Fish.

### Changed

- Updated resource creation next-steps guidance to provide explicit canonical file paths alongside optional `aikito edit` shortcuts, accommodating IDE users, AI coding agents, and terminal workflows.

### Fixed

- Fixed project `agent.toml` multi-line array parsing and serialization during skill addition, preserving comments, formatting, and nested table structures (`[table]`).
- Added pre-write TOML syntax validation gates for all resource addition commands.
- Added atomic rollback and cleanup on resource creation failures.
- Added strict mutual exclusion and validation for `aikito add mcp` transport and configuration arguments.

## [1.5.1] - 2026-08-14

### Fixed

- Corrected CLI `--version` output constant to report the active release version.

## [1.5.0] - 2026-08-14

### Added

- Added `aikito show subagent <name>` and `aikito edit subagent <name>` (with `subagents` alias) for inspecting and editing individual subagent definitions.
- Added `aikito edit mcp <server>` for opening MCP server configuration files in the configured editor.
- Added `aikito show mcp <server> --agent` (and `--agent <agent>`) to display detailed per-agent synchronized status and configuration blocks.
- Added dynamic shell completion candidate support for MCP servers (`mcps`) across Zsh, Bash, and Fish.

### Changed

- Migrated MCP server configurations from a single `mcps.toml` file to individual configuration files in `mcps/*.toml`.
- Aligned `aikito show mcp <server>` to print the canonical `mcps/<server>.toml` content directly, matching `show skill` and `show subagents`.

## [1.4.0] - 2026-08-14

### Added

- Added `aikito show project [name]` and its `projects` alias for inspecting registered project configuration, resource counts, synchronization health, and actionable issue details.
- Added project-aware drift output to `aikito diff` for copied skills, including text diffs and binary-change reporting.
- Added `--dry-run` support to project synchronization, including stale-resource cleanup previews.

### Changed

- Restricted `aikito status` to workspace-level resources; project synchronization health now lives under `aikito show project`.

### Fixed

- Prevented project and global synchronization from silently deleting unmanaged skill or memory content.
- Restored safe cleanup of deselected managed project skills without requiring manual deletion.

## [1.3.0] - 2026-08-13

### Added

- Added basename-prefix path completion across the Aikito workspace and registered projects.
- Collapsed duplicate memory completion identifiers into one scope-labelled candidate per note.

## [1.2.0] - 2026-08-12

### Added

- Added detail inspection subcommands `aikito show mcp <name>` and `aikito show agents <name>` for viewing detailed MCP server configurations and Agent definitions.
- Added `aikito diff` command for inspecting full diffs of drifted workspace resources (instructions, MCP servers, subagents, skills).
- Added global and project instruction management commands: `aikito show instructions` and `aikito edit instructions`.
- Added shell completion command `aikito completion` supporting `zsh`, `bash`, and `fish`.

### Fixed

- Protected AGY MCP configuration loading when authentication tokens are absent.
- Improved credential-dependent MCP drift diagnostics in status and health checks.

## [1.1.0] - 2026-08-11

### Added

- Added workspace configuration support via `config.toml` for customizing global settings like `[memory] stale_days`.
- Added project-level memory staleness threshold override in `agent.toml`.
- Registered Antigravity CLI (`agy`) as a supported subagent target (`.gemini/config/agents/<name>/agent.md`).

### Changed

- Refactored CLI command hierarchy: migrated `status` subcommands (`mcp`, `subagents`, `skills`, `memory`) to `show` (`show mcp`, `show subagents`, `show skills`, `show memory`). Restricted `aikito status` strictly to the top-level workspace synchronization dashboard.

### Fixed

- Fixed memory staleness threshold description formatting in `aikito doctor` when project-specific staleness overrides are used.

## [1.0.0] - 2026-08-10

### Added

- Added explicit `aikito init workspace` and `aikito init project` workflows for initializing the central workspace and registering project-scoped Agent resources.

### Changed

- Replaced the legacy `aikito init [path]` syntax with `aikito init workspace [path]`. Existing users must add the `workspace` resource when initializing a workspace.

## [0.3.1] - 2026-08-09

### Added

- Added Memory health diagnostics for unindexed notes, missing index targets, dangling cross-note wikilinks, and notes whose Git history indicates they may need freshness review.

### Fixed

- Made `aikito sync subagents` treat an empty `[subagents]` table as a successful no-op, so a freshly initialized workspace passes the documented synchronization flow and CI smoke test.

## [0.3.0] - 2026-08-09

### Added

- Registered GitHub Copilot CLI (`github-copilot`) as a supported agent for global instructions, skills (`~/.agents/skills`), MCP servers (`.copilot/mcp-config.json`), and custom agents (`.copilot/agents/*.agent.md`).
- Added support for `copilot_json` MCP format and `copilot_markdown` subagent format with typed frontmatter fields (`tools`, boolean flags).
- Added GitHub Copilot CLI scanning to `aikito adopt` and diagnostics to `aikito doctor`.
- Enhanced `aikito status` skills rendering to indicate symbolic link depth (`›` for direct `~/.agents/skills`, `»` for agent-specific paths).
- Added same-path short-circuit in `aikito sync global` to handle direct `~/.agents/skills` target paths without conflict.
- Enabled OpenCode to consume global Skills directly from its native `~/.agents/skills` compatibility path.

### Fixed

- Preserved typed GitHub Copilot custom-agent options and safe MCP headers during adoption.
- Distinguished missing, drifted, and conflicting managed subagents in `status` and `doctor` diagnostics.

## [0.2.0] - 2026-08-07

### Added

- Added `aikito doctor` command for deep workspace diagnostics including orphan skill detection, broken symlink validation, empty directory cleanup hints, and auto-fix capabilities.
- Added `aikito skills status`, `aikito skills show`, and `aikito skills edit` subcommands for interactive skill inspection and editing.
- Added path escape guards for skill target resolution to enhance CLI security.
- Added support for Python 3.12 and 3.13 compatibility in skill status row formatting.
- Improved `VISUAL`/`EDITOR` fallback handling to handle whitespace-only environment variables safely.

## [0.1.0] - 2026-08-04

### Added

- Initial public release of Aikito.
- Added a Git-managed workspace for Agent instructions, skills, durable memory,
  MCP servers, and subagents.
- Added global and project-scoped resource management.
- Added synchronization across supported coding agents based on their
  capabilities.
- Added project skill synchronization with `link` and `copy` modes.
- Added workspace adoption with previews, backups, conflict detection, and
  credential sanitization.
- Added status commands for inspecting synchronized resources and memory.
- Added an open-source export workflow with allowlist filtering, privacy
  scanning, integrity checks, and automated tests.
- Added installation and operational documentation for macOS, Linux, and WSL2.

[Unreleased]: https://github.com/lsaint/aikito/compare/v1.50.0...HEAD
[1.50.0]: https://github.com/lsaint/aikito/compare/v1.49.1...v1.50.0
[1.49.1]: https://github.com/lsaint/aikito/compare/v1.49.0...v1.49.1
[1.49.0]: https://github.com/lsaint/aikito/compare/v1.48.0...v1.49.0
[1.48.0]: https://github.com/lsaint/aikito/compare/v1.47.0...v1.48.0
[1.47.0]: https://github.com/lsaint/aikito/compare/v1.46.0...v1.47.0
[1.46.0]: https://github.com/lsaint/aikito/compare/v1.45.0...v1.46.0
[1.45.0]: https://github.com/lsaint/aikito/compare/v1.44.2...v1.45.0
[1.44.2]: https://github.com/lsaint/aikito/compare/v1.44.1...v1.44.2
[1.44.1]: https://github.com/lsaint/aikito/compare/v1.44.0...v1.44.1
[1.44.0]: https://github.com/lsaint/aikito/compare/v1.43.0...v1.44.0
[1.43.0]: https://github.com/lsaint/aikito/compare/v1.42.0...v1.43.0
[1.42.0]: https://github.com/lsaint/aikito/compare/v1.41.0...v1.42.0
[1.41.0]: https://github.com/lsaint/aikito/compare/v1.40.0...v1.41.0
[1.40.0]: https://github.com/lsaint/aikito/compare/v1.39.0...v1.40.0
[1.39.0]: https://github.com/lsaint/aikito/compare/v1.38.0...v1.39.0
[1.38.0]: https://github.com/lsaint/aikito/compare/v1.37.0...v1.38.0
[1.37.0]: https://github.com/lsaint/aikito/compare/v1.36.1...v1.37.0
[1.36.1]: https://github.com/lsaint/aikito/compare/v1.36.0...v1.36.1
[1.36.0]: https://github.com/lsaint/aikito/compare/v1.35.0...v1.36.0
[1.35.0]: https://github.com/lsaint/aikito/compare/v1.34.0...v1.35.0
[1.34.0]: https://github.com/lsaint/aikito/compare/v1.33.0...v1.34.0
[1.33.0]: https://github.com/lsaint/aikito/compare/v1.32.0...v1.33.0
[1.32.0]: https://github.com/lsaint/aikito/compare/v1.31.1...v1.32.0
[1.31.1]: https://github.com/lsaint/aikito/compare/v1.31.0...v1.31.1
[1.31.0]: https://github.com/lsaint/aikito/compare/v1.30.1...v1.31.0
[1.30.1]: https://github.com/lsaint/aikito/compare/v1.30.0...v1.30.1
[1.30.0]: https://github.com/lsaint/aikito/compare/v1.29.0...v1.30.0
[1.29.0]: https://github.com/lsaint/aikito/compare/v1.28.0...v1.29.0
[1.28.0]: https://github.com/lsaint/aikito/compare/v1.27.0...v1.28.0
[1.27.0]: https://github.com/lsaint/aikito/compare/v1.26.0...v1.27.0
[1.26.0]: https://github.com/lsaint/aikito/compare/v1.25.1...v1.26.0
[1.25.1]: https://github.com/lsaint/aikito/compare/v1.25.0...v1.25.1
[1.25.0]: https://github.com/lsaint/aikito/compare/v1.24.1...v1.25.0
[1.24.1]: https://github.com/lsaint/aikito/compare/v1.24.0...v1.24.1
[1.24.0]: https://github.com/lsaint/aikito/compare/v1.23.0...v1.24.0
[1.23.0]: https://github.com/lsaint/aikito/compare/v1.22.0...v1.23.0
[1.22.0]: https://github.com/lsaint/aikito/compare/v1.21.0...v1.22.0
[1.21.0]: https://github.com/lsaint/aikito/compare/v1.20.1...v1.21.0
[1.20.1]: https://github.com/lsaint/aikito/compare/v1.20.0...v1.20.1
[1.20.0]: https://github.com/lsaint/aikito/compare/v1.19.0...v1.20.0
[1.19.0]: https://github.com/lsaint/aikito/compare/v1.18.1...v1.19.0
[1.18.1]: https://github.com/lsaint/aikito/compare/v1.18.0...v1.18.1
[1.18.0]: https://github.com/lsaint/aikito/compare/v1.17.0...v1.18.0
[1.17.0]: https://github.com/lsaint/aikito/compare/v1.16.0...v1.17.0
[1.15.1]: https://github.com/lsaint/aikito/compare/v1.15.0...v1.15.1
[1.15.0]: https://github.com/lsaint/aikito/compare/v1.14.0...v1.15.0
[1.14.0]: https://github.com/lsaint/aikito/compare/v1.13.0...v1.14.0
[1.13.0]: https://github.com/lsaint/aikito/compare/v1.12.0...v1.13.0
[1.12.0]: https://github.com/lsaint/aikito/compare/v1.11.0...v1.12.0
[1.11.0]: https://github.com/lsaint/aikito/compare/v1.10.0...v1.11.0
[1.10.0]: https://github.com/lsaint/aikito/compare/v1.9.0...v1.10.0
[1.9.0]: https://github.com/lsaint/aikito/compare/v1.8.0...v1.9.0
[1.8.0]: https://github.com/lsaint/aikito/compare/v1.7.0...v1.8.0
[1.7.0]: https://github.com/lsaint/aikito/compare/v1.6.0...v1.7.0
[1.6.0]: https://github.com/lsaint/aikito/compare/v1.5.1...v1.6.0
[1.5.1]: https://github.com/lsaint/aikito/compare/v1.5.0...v1.5.1
[1.5.0]: https://github.com/lsaint/aikito/compare/v1.4.0...v1.5.0
[1.4.0]: https://github.com/lsaint/aikito/compare/v1.3.0...v1.4.0
[1.3.0]: https://github.com/lsaint/aikito/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/lsaint/aikito/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/lsaint/aikito/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/lsaint/aikito/compare/v0.3.1...v1.0.0
[0.3.1]: https://github.com/lsaint/aikito/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/lsaint/aikito/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lsaint/aikito/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lsaint/aikito/releases/tag/v0.1.0
