# Changelog

All notable changes to Aikito will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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

[Unreleased]: https://github.com/lsaint/aikito/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/lsaint/aikito/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/lsaint/aikito/releases/tag/v0.1.0
