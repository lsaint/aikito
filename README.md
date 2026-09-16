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

[简体中文](README.zh-CN.md) · [Homepage](https://lsaint.github.io/aikito/) · [Documentation](https://lsaint.github.io/aikito/guide/)

Aikito keeps coding-agent instructions, skills, MCP definitions, subagents, and
durable memory in one Git-managed workspace, shared across agents and projects.

It is built for people whose Agent setup already works, but has become tedious
to keep consistent across tools, projects, machines, and time.

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
> README, `src/aikito/templates/skills/aikito/SKILL.md`, and any linked
> documentation relevant to the setup. Inspect the Agent configuration I
> already use, initialize the Aikito workspace, adopt supported existing resources,
> synchronize them through Aikito, and verify the result with `aikito status`.
> `adopt` and `sync` preflight their complete plans before writing. If either
> command stops, explain the findings and ask before using `--skip`, `--force`,
> `--prune`, or manually resolving a conflict. Preserve my current setup and do
> not register a project without my confirmation. If there is nothing to adopt,
> skip that step and tell me.

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

Bring your existing Agent setup under Aikito:

```bash
aikito init workspace ~/aikito
aikito adopt
aikito sync
aikito status
```

`aikito adopt` and `aikito sync` check their complete plans before writing and
stop if anything needs attention. Use `--dry-run` for a concise read-only
summary, or add `--verbose` for every item and path. For adoption details, see
[migration and safety](#migration-and-safety).

Starting without existing Agent configuration? Skip `aikito adopt`; the rest of
the workflow is unchanged.

On Windows, enable Developer Mode and use `uv tool install aikito` or the
[PowerShell installation guide](docs/installation.md#install-manually).

</details>

Continue with the **[existing-setup guide](docs/workspace-setup.md)** to consolidate
what your Agents already use. To create resources from scratch, connect a
project, or handle other tasks, use
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
┌─────────┬───────────────────┬──────────────────────────────┬──────┐
│ Scope   │ Note File         │ Title                        │ Link │
├─────────┼───────────────────┼──────────────────────────────┼──────┤
│ Global  │ writing-style     │ Keep explanations concise    │ –    │
├─────────┼───────────────────┼──────────────────────────────┼──────┤
│ example │ api-retry-policy  │ Retry external APIs safely   │ ✓    │
│ example │ release-checklist │ Release verification steps   │ ✓    │
└─────────┴───────────────────┴──────────────────────────────┴──────┘
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

Already have agent configuration? Run `aikito adopt`; it checks the complete
import plan and stops before writing if anything needs attention. Use
`aikito adopt --dry-run --verbose` for a detailed read-only review; see
[adoption and backups](docs/safety.md#adoption). `aikito doctor` reports the
same adoption issues; when one resource is intentionally excluded, use
the exact resource-level `--skip` command shown in the finding.

Your workspace is a local Git repository. Review it for secrets and private
data before publishing; removing a secret in a later commit does not erase it
from history. Read the [safety model](docs/safety.md) and report vulnerabilities
through the [Security Policy](SECURITY.md).

## Documentation

- [Getting started](docs/guide.md): installation through adopting and synchronizing your existing setup.
- [Workspace and synchronization](docs/architecture.md): source files, scopes, and resource ownership.
- [Connect another machine](docs/workspace-portability.md): existing workspaces and custom paths.
- [CLI reference](docs/cli-reference.md): commands and shell completion.
- [Comparison](docs/comparison.md) and [FAQ](docs/faq.md): design choices and common questions.

[Chat Distiller](https://github.com/lsaint/chat-distiller) can turn browser AI
conversations into Markdown notes in your Aikito Inbox. See the
[capture and review workflow](docs/chat-distiller.md).

Browse the full [documentation site](https://lsaint.github.io/aikito/guide/) for more.

## Support

If you find Aikito useful, you can [support its development](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito).
