# Changelog

All notable changes to Aikito will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/lsaint/aikito/compare/v1.24.1...HEAD
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
