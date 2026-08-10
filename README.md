<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.png">
    <img src="docs/assets/logo-light.png" alt="Aikito Logo" width="160">
  </picture>
</p>

<h1 align="center">Aikito</h1>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12 | 3.13 | 3.14](https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/downloads/)
![Platform: macOS | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)
![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)

[简体中文](README.zh-CN.md) · [Documentation](docs/README.md)

Aikito is a lightweight, Git-managed workspace for reusable AI-agent resources.

```text
   Aikito  =  governing agent resources  ×  curating durable memory
                 (sync / distribute)        (retrieve / distill / persist)
```

Both sit on one foundation: a Git workspace, plain files, explicit scopes.

You author the resources, agents maintain the memory, Aikito decides who sees what.

No database, daemon, vector store, or hosted service required.

## Why Aikito

AI agent resources fragment in three directions:

- Across tools: each agent requires a different configuration format
- Across projects: reusable knowledge, skills, and instructions are copied or
  maintained across multiple repositories
- Across time: valuable decisions and hard-won lessons disappear into old
  sessions

Aikito keeps all of it in one personal Git workspace and exposes selected
resources to each agent and project:

```text
~/aikito
├── skills/                         shared reusable skills
├── memory/                         global durable memory
├── global/                         global instructions
├── mcps.toml                       shared MCP definitions
├── subagents/                      shared reusable subagents
└── projects/
    └── <project-name>/
        ├── agent.toml              selected shared resources
        ├── AGENTS.md               project instructions
        └── memory/                 project durable memory
            ├── index.md
            └── notes/
```

Each project selects what it needs. It does not have to become the canonical
home of every shared skill or memory note.

## What Aikito Manages

| Resource | Canonical source | Synchronized destination |
| --- | --- | --- |
| Memory | `memory/`, `projects/<name>/memory/` | Global access and `<project>/.agents/memory/` |
| Skills | `skills/<name>/` | Shared and project-level skill directories |
| Instructions | `global/AGENTS.md`, `projects/<name>/AGENTS.md` | Agent-native and project runtime instructions |
| MCP servers | `mcps.toml` | Native TOML, JSON, or JSONC configs |
| Subagents | `subagents.toml`, `subagents/` | Native subagent definitions |

The default registry includes Codex, Claude Code, Antigravity CLI (`agy`),
OpenCode, and GitHub Copilot CLI. See the [architecture](docs/architecture.md) for the complete mental
model and capability boundaries.

### Share or isolate

- `link` keeps a resource shared and immediately up to date
- `copy` gives a project an isolated snapshot it can evolve independently
- project memory always uses `link` mode with its canonical scope, preserving one history

## Durable Memory

The bundled `durable-memory` skill is the curation half of the equation. It
guides coding agents to retrieve relevant notes before acting, distill durable
conclusions from what they learn, update notes that went stale, and choose the
right global or project scope.

The notes are ordinary Markdown, so Git gives you history, review, rollback, and
portability — and the memory an agent wrote in Claude Code yesterday is the same
memory Codex reads tomorrow.

`aikito status memory` lists what has accumulated, grouped by scope. A typical
example looks like this:

```text
┌────────┬───────────────────────┬─────────────────────────────────────┬───────┬──────┐
│ Scope  │ Note File             │ Title                               │ Index │ Link │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ Global │ commit-message-style  │ Conventional commits, English only  │ ✓     │ –    │
│ Global │ review-tone           │ Ask before large refactors          │ ✓     │ –    │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ aikito │ versioning-principles │ Version bumps skip round numbers    │ ✓     │ ✓    │
│ aikito │ release-checklist     │ Tag only after tests pass           │ ✓     │ ✓    │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ blog   │ draft-workflow        │ Drafts live in content/ until dated │ ✓     │ ✓    │
└────────┴───────────────────────┴─────────────────────────────────────┴───────┴──────┘
```

Global notes are available everywhere; each project's notes are linked only into
that project. In this example, an agent working in `aikito` sees only the global
notes plus `aikito`'s own notes, and nothing from `blog`.

## Boundaries

Aikito manages durable files, explicit scopes, and controlled synchronization.
To stay lightweight and portable, it deliberately **does not**:

- capture every agent action or conversation automatically
- run a vector store, embedding pipeline, or memory service
- inject context into every prompt through a background daemon
- orchestrate supervisor and worker agents
- replace your coding agent's native runtime

Aikito governs the workspace and the memory in it. Your agent does the reasoning.

## Requirements

- macOS or Linux; Windows users should use WSL2.
- Python 3.12, 3.13, or 3.14.
- Git.

Native Windows is not supported: Aikito relies on symbolic links and POSIX file
permissions for synchronization and credential safety.

## Quick Start

```bash
brew install lsaint/tap/aikito

aikito init ~/aikito
aikito sync global
aikito status
```

`aikito status` reports resource state across supported agents:

```text
┌───────────────────────┬──────────────┬────────┬────────────┬───────────┐
│ Agent                 │ Instructions │ Skills │ MCP Config │ Subagents │
├───────────────────────┼──────────────┼────────┼────────────┼───────────┤
│ Codex                 │ ✓            │ 2 ›    │ –          │ –         │
│ Claude Code           │ ✓            │ 2 »    │ –          │ –         │
│ Antigravity CLI (agy) │ ✓            │ 2 »    │ –          │ –         │
│ OpenCode              │ ✓            │ 2 ›    │ –          │ –         │
│ GitHub Copilot CLI    │ ✓            │ 2 ›    │ –          │ –         │
└───────────────────────┴──────────────┴────────┴────────────┴───────────┘

✓ all synced · 5 agents · 2 skills · 0 notes across 1 scopes
```

For building from source, custom install paths, or advanced configuration, see
the [project setup guide](docs/project-setup.md).

## Migrating an Existing Setup

If you already use coding agents with existing instructions, MCP definitions, or
subagents, import them with `aikito adopt`:

```bash
aikito adopt
aikito adopt --apply
```

Adoption previews read-only first. Applying creates timestamped backups under
`~/.aikito/backups/adopt_<timestamp>` and imports detected configurations
without overwriting originals. See the [safety guide](docs/safety.md).

## Companion: Chat Distiller

[Chat Distiller](https://github.com/lsaint/chat-distiller) turns browser AI
conversations into reviewable Markdown notes and saves them to your Aikito
`inbox/`.

Browser conversation → distilled note → review → durable memory

See [capturing browser discussions](docs/chat-distiller.md) for the workflow.

## Safety First

`aikito init` creates a local Git repository; it does not make that repository
private or safe to publish. Before adding a remote or pushing, review memory and
configuration for credentials, customer data, internal addresses, and private
code. Deleting a later commit does not remove a secret from Git history.

Read the [safety model](docs/safety.md) before synchronizing an existing setup.

## Documentation

Browse the [documentation index](docs/README.md) for concepts, operational
guides, the CLI reference, safety details, the roadmap, and the
[FAQ](docs/faq.md).

- [Comparison and Design Boundaries](docs/comparison.md) — where Aikito fits
  alongside memory systems, project-local sync tools, and agent orchestrators

## Contributing

Issues and pull requests are welcome. Before submitting code, run:

```bash
python3 -m unittest discover -s tests
```

Report vulnerabilities privately according to the [Security Policy](SECURITY.md).

## Support

If you find Aikito useful, you can [support its development](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito).
