# Changelog

All notable changes to Aikito will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/lsaint/aikito/compare/v1.3.0...HEAD
[1.3.0]: https://github.com/lsaint/aikito/compare/v1.2.0...v1.3.0
[1.2.0]: https://github.com/lsaint/aikito/compare/v1.1.0...v1.2.0
[1.1.0]: https://github.com/lsaint/aikito/compare/v1.0.0...v1.1.0
[1.0.0]: https://github.com/lsaint/aikito/compare/v0.3.1...v1.0.0
[0.3.1]: https://github.com/lsaint/aikito/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/lsaint/aikito/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/lsaint/aikito/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lsaint/aikito/releases/tag/v0.1.0
