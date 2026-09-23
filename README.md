<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-light.png">
    <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-light.png" alt="Aikito Logo" width="160">
  </picture>
</p>

<h1 align="center">Aikito</h1>

<p align="center">
  <b>Multi-Agent · Multi-Project · Multi-OS · Multi-Machine</b>
</p>

<p align="center">
  <a href="https://github.com/lsaint/aikito/releases"><img src="https://img.shields.io/github/v/release/lsaint/aikito" alt="Release"></a>
  <a href="https://github.com/lsaint/aikito/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/lsaint/aikito/ci.yml?branch=main&label=CI" alt="CI"></a>
  <a href="https://github.com/lsaint/aikito/blob/main/LICENSE"><img src="https://img.shields.io/github/license/lsaint/aikito" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg" alt="Python 3.12 | 3.13 | 3.14"></a>
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg" alt="Dependencies: stdlib only">
</p>

<p align="center">
  <a href="README.zh-CN.md">简体中文</a> · <a href="https://lsaint.github.io/aikito/">Homepage</a> · <a href="https://lsaint.github.io/aikito/guide/">Documentation</a>
</p>

Aikito gives you one place to govern the context your coding agents share.

Manage instructions, skills, MCPs, subagents, and durable memory across agents, projects, and machines.

<p align="center">
  <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/aikito-overview.png" alt="Aikito overview">
</p>

## Quick Start

### 1. Install

With [uv](https://docs.astral.sh/uv/) (recommended cross-platform) or Homebrew (macOS / Linux):

```bash
uv tool install aikito
# or: brew install lsaint/tap/aikito
```

### 2. Set Up Your Workspace

Bring your existing Agent setup under Aikito in 30 seconds:

```bash
aikito init workspace ~/aikito
aikito adopt  # use --dry-run to preview, --verbose for full paths
aikito sync
aikito status
```

`adopt` and `sync` preflight their complete plans before writing. Starting from
scratch? Simply skip `aikito adopt`.

> On Windows, enable Developer Mode and see the [Windows installation guide](docs/installation.md#install-manually).

<details>
<summary><b>Prefer your coding agent set it up?</b> (Click to expand prompt)</summary>

Paste this prompt into your coding agent:

> Install and configure Aikito from https://github.com/lsaint/aikito. Read the
> README, `src/aikito/templates/skills/aikito/SKILL.md`, and any linked
> documentation relevant to the setup. Inspect the Agent configuration I
> already use, initialize the Aikito workspace, adopt supported existing resources,
> synchronize them through Aikito, and verify the result with `aikito status`.
> `adopt` and `sync` preflight their complete plans before writing. If either
> command stops, explain the findings and ask before using `--skip`, `--force`,
> `--prune`, or manually resolving a conflict. Preserve my current setup and do
> not register a project without my confirmation. If there is nothing to adopt,
> skip that step and tell me.

</details>

## See the Result

`aikito status` shows synchronized context and durable memory across all agents and projects:

```text
┌──────────┬───────┬────────┬────────┬───────┬──────┬────────┐
│ Project  │ Instr │ Skills │ Memory │ Paths │ Mode │ Status │
├──────────┼───────┼────────┼────────┼───────┼──────┼────────┤
│ aikito   │ 24L   │ 1      │ 12     │ 2/3   │ link │ ✓      │
│ payments │ 128L  │ 5      │ 14     │ 1/1   │ link │ ✓      │
│ infra    │ 210L  │ 6      │ 21     │ 1/1   │ copy │ ✓      │
│ blog     │ –     │ 2      │ 8      │ 0/1   │ link │ –      │
└──────────┴───────┴────────┴────────┴───────┴──────┴────────┘

Global: ✓ · Instr 24L · Skills 12 · Memory 6 · MCP 1 · Sub 3
Consumers (8): agy · claude · codex · copilot · dsh · grok · opencode · pi
All in one workspace: ~/aikito (AIKITO_DIR)
```

Prefer a browser view? Run [`aikito web`](docs/cli-reference.md#aikito-web) for
the local, read-only Console.

## Why Aikito

AI agent resources fragment in three directions:

- **Across tools**: each agent invents its own folders, memory formats, and config conventions.
- **Across projects**: reusable knowledge, skills, and instructions are copied or drift out of sync.
- **Across time**: valuable decisions and hard-won lessons disappear into ephemeral chat sessions.

Aikito keeps the source files in one personal Git workspace and connects
selected resources to each agent and project. No background daemon, vector store,
database, or hosted service is required.

### Durable Memory

The bundled [durable-memory skill](src/aikito/templates/skills/durable-memory/SKILL.md)
guides agents to retrieve relevant notes, record verified conclusions, and
update stale knowledge. Notes are plain Markdown files versioned by Git.

- **Global scope**: cross-project preferences, coding conventions, and tool practices.
- **Project scope**: architecture decisions, API policies, and local workflows.

See [memory usage and opt-out](docs/durable-memory.md) and
[why memory needs a maintainer](docs/programming-agent-memory.md).

## Safety & Boundaries

Aikito is local-first and predictable by design:

- **Preflight before write**: `adopt` and `sync` simulate and check operations before modifying files.
- **Plain files & Git**: no background service, no proprietary database, no implicit prompt injection.
- **You govern secrets**: workspace is a local Git repository; review secrets before pushing.

Detailed safety and boundary references:
- [Safety model & backups](docs/safety.md#adoption)
- [Design boundaries & comparisons](docs/comparison.md)

## Documentation

Visit the full [documentation site](https://lsaint.github.io/aikito/guide/) or dive into:

- [Getting started](docs/guide.md): installation through adopting your existing setup.
- [Workspace and synchronization](docs/architecture.md): directory layout, scopes, and linking model.
- [Connect another machine](docs/workspace-portability.md): multi-machine sync and custom paths.
- [CLI reference](docs/cli-reference.md): commands, options, and shell completion.
- [Chat Distiller](docs/chat-distiller.md): distill browser AI chats into Markdown notes in your Inbox.
- [Troubleshooting](docs/troubleshooting.md): resolving conflicts and synchronization drift.

## Support

If you find Aikito useful, you can [support its development](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito).
