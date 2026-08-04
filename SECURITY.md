# Security Policy

## Supported Versions

Aikito is under active development. Security updates are applied to the latest version on the `main` branch.

| Version | Supported          |
| ------- | ------------------ |
| `main`  | :white_check_mark: |
| < 0.1   | :x:                |

## Reporting a Vulnerability

Because Aikito reads, modifies, and synchronizes AI agent configurations (such as Codex, Claude Code, Antigravity, and OpenCode) and MCP server definitions, security and credential handling are top priorities.

If you discover a security vulnerability or credential handling flaw in Aikito:

1. **Do NOT open a public GitHub issue** for undisclosed security vulnerabilities.
2. Please report the vulnerability privately via [GitHub Security Advisories](https://github.com/lsaint/aikito/security/advisories/new) or directly via email to **ls4int@gmail.com**.
3. Include details of the vulnerability, reproduction steps, and any impact on credentials or local configuration.

You can expect an initial acknowledgement within 48 hours and progress updates until a fix is published.

## Security Scope & Principles

Aikito adheres to conservative security principles when managing workspace assets:

- **Local Configuration Safeguards**: Commands like `aikito adopt --apply` take timestamped backups before modifying configuration files.
- **MCP Secret Reference Conversion**: Plaintext API keys or credentials in MCP configurations are converted to environment variable references rather than hardcoding secret values in synchronized files.
- **Git & Memory Privacy**: `aikito init` creates a local Git repository for workspace tracking. Users are responsible for ensuring that memory notes and configurations do not contain API keys, credentials, customer data, or internal infrastructure details prior to adding remotes or pushing to public repositories.
- **Unmanaged File Protection**: Aikito will not silently overwrite existing unmanaged agent instructions or configurations; manual conflict resolution or explicit override flags are required.
