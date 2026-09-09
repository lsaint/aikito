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
  <a href="https://github.com/lsaint/aikito/actions/workflows/ci.yml"><img src="https://github.com/lsaint/aikito/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/lsaint/aikito/blob/main/LICENSE"><img src="https://img.shields.io/github/license/lsaint/aikito" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-informational" alt="Platforms">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg" alt="Python 3.12 | 3.13 | 3.14"></a>
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg" alt="Dependencies: stdlib only">
</p>

[简体中文](README.zh-CN.md) · [Documentation](https://lsaint.github.io/aikito/)

Aikito keeps coding-agent instructions, skills, MCP definitions, subagents, and
durable memory in one Git-managed workspace, shared across agents and projects.

Aikito governs the workspace, agents maintain the memory, and you oversee it all.

<p align="center">
  <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/aikito-overview.png" alt="Aikito overview">
</p>

## Why Aikito

AI agent resources fragment in three directions:

- Across tools: each agent requires a different configuration format
- Across projects: reusable knowledge, skills, and instructions are copied or
  maintained across multiple repositories
- Across time: valuable decisions and hard-won lessons disappear into old
  sessions

Aikito keeps the source files in one personal workspace and connects selected
resources to each agent and project. No database, daemon, vector store, or
hosted service is required.

## Durable Memory

The bundled [durable-memory skill](src/aikito/templates/skills/durable-memory/SKILL.md)
guides agents to retrieve useful notes, retain verified conclusions, and update
stale knowledge. Notes are plain Markdown with Git history.

For example, a writing preference belongs in global memory, while an API retry
decision belongs to its project. A project normally connects to global memory
and its own notes; these scopes organize context, not filesystem access permissions.

New workspaces enable the workflow by default; synchronization connects it to
agents. See [memory usage and opt-out](docs/durable-memory.md) and
[why memory needs a maintainer](docs/programming-agent-memory.md).

## Quick Start

### Let your coding agent set it up (recommended)

> Install and configure Aikito from https://github.com/lsaint/aikito. Read the
> README, `templates/skills/aikito/SKILL.md`, and any linked documentation relevant to the
> setup, then follow their safety requirements to initialize the workspace,
> synchronize resources with `aikito sync`, and verify the result with `aikito status`.
> Before importing or changing any existing Agent configuration, show me the
> planned changes and conflicts and wait for my approval. When setup is
> complete, summarize what is ready and guide me through the next step, including
> whether to register my first code project. Do not register a project without
> my confirmation.

<details>
<summary>Install manually (macOS / Linux / Windows)</summary>

Cross-platform with [uv](https://docs.astral.sh/uv/) (recommended):

```bash
uv tool install aikito
```

Or with Homebrew (macOS / Linux):

```bash
brew install lsaint/tap/aikito
```

Or with pipx:

```bash
pipx install aikito
```

Initialize and synchronize your workspace:

```bash
aikito init workspace ~/aikito
aikito sync --dry-run
aikito sync
aikito status
```

Review the preview before applying synchronization. For existing configuration,
see [migration and safety](#migration-and-safety).

On Windows, enable Developer Mode and use `uv tool install aikito` or the
[PowerShell installation guide](docs/installation.md#install-manually).

</details>

Continue with the **[four-step tutorial](docs/installation.md)** to connect your
first project and verify an instruction. For other tasks, use
[Agent Request Examples](docs/agent-workflow.md).

## See the Result

`aikito status` shows resource state across agents. Example output from a
configured workspace (agents and counts depend on your setup):

```text
┌───────────────────────┬──────────────┬────────┬────────────┬───────────┐
│ Agent                 │ Instructions │ Skills │ MCP Config │ Subagents │
├───────────────────────┼──────────────┼────────┼────────────┼───────────┤
│ Codex                 │ ✓            │ 2 ›    │ 0          │ 0         │
│ Claude Code           │ ✓            │ 2 »    │ 0          │ 0         │
│ Antigravity CLI       │ ✓            │ 2 »    │ 0          │ 0         │
│ OpenCode              │ ✓            │ 2 ›    │ 0          │ 0         │
│ GitHub Copilot CLI    │ ✓            │ 2 ›    │ 0          │ 0         │
│ DeepSeek Harness      │ ✓            │ 2 ›    │ 0          │ 0         │
│ Grok Build            │ ✓            │ 2 ›    │ 0          │ 0         │
│ Pi                    │ ✓            │ 2 ›    │ –          │ –         │
└───────────────────────┴──────────────┴────────┴────────────┴───────────┘

✓ all synced · 8 agents · 2 skills · 0 notes across 1 scopes
```

`aikito show memory` lists retained knowledge by scope. This separate example
shows one global note and two notes for project `example`:

```text
┌─────────┬───────────────────┬──────────────────────────────┬───────┬──────┐
│ Scope   │ Note File         │ Title                        │ Index │ Link │
├─────────┼───────────────────┼──────────────────────────────┼───────┼──────┤
│ Global  │ writing-style     │ Keep explanations concise    │ ✓     │ –    │
├─────────┼───────────────────┼──────────────────────────────┼───────┼──────┤
│ example │ api-retry-policy  │ Retry external APIs safely   │ ✓     │ ✓    │
│ example │ release-checklist │ Release verification steps   │ ✓     │ ✓    │
└─────────┴───────────────────┴──────────────────────────────┴───────┴──────┘
```

Global notes hold cross-project knowledge; project notes hold local decisions.
See [memory operations](docs/durable-memory.md#list-memory) for the full workflow.

Use [synchronization troubleshooting](docs/troubleshooting.md) to investigate
missing links, conflicts, or drift. Prefer a browser view? Run
[`aikito web`](docs/cli-reference.md#aikito-web) for the local, read-only Console.

## Boundaries

Aikito uses plain files and Git, with no background service required.

<details>
<summary>What Aikito does not do</summary>

- capture every agent action or conversation automatically
- run a vector store, embedding pipeline, or memory service
- inject context into every prompt through a background daemon
- orchestrate supervisor and worker agents
- replace your coding agent's native runtime

</details>

## Migration and Safety

Already have agent configuration? Run `aikito adopt` for a read-only import
preview. Review the plan before applying it; see
[adoption and backups](docs/safety.md#adoption).

Your workspace is a local Git repository. Review it for secrets and private
data before publishing; removing a secret in a later commit does not erase it
from history. Read the [safety model](docs/safety.md) and report vulnerabilities
through the [Security Policy](SECURITY.md).

## Documentation

- [Getting started](docs/installation.md): installation through your first working instruction.
- [Workspace and synchronization](docs/architecture.md): source files, scopes, and resource ownership.
- [Connect another machine](docs/workspace-portability.md): existing workspaces and custom paths.
- [CLI reference](docs/cli-reference.md): commands and shell completion.
- [Comparison](docs/comparison.md) and [FAQ](docs/faq.md): design choices and common questions.

[Chat Distiller](https://github.com/lsaint/chat-distiller) can turn browser AI
conversations into Markdown notes in your Aikito Inbox. See the
[capture and review workflow](docs/chat-distiller.md).

Browse the full [documentation site](https://lsaint.github.io/aikito/) for more.

## Support

If you find Aikito useful, you can [support its development](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito).
