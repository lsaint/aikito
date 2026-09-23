# Use Durable Memory

Aikito Memory gives an Agent a small, durable knowledge base that survives
across conversations and works across supported Agent tools. It is not a chat
archive or a background service. Memory is a collection of curated Markdown
notes in your Aikito workspace, governed by a skill and versioned with Git.

## What Becomes Memory

The Agent decides autonomously whether a conclusion becomes Memory. The
`durable-memory` skill gives supported Agents the same criteria, but it does not
make that judgment deterministic. Different models may retrieve, select, and
summarize knowledge differently, so model capability affects the quality and
consistency of the resulting Memory.

Under that policy, the Agent favors conclusions that are verified, likely to
matter again, and difficult enough to rediscover that they could change a future
decision. Examples include a user preference, an architectural constraint, or a
non-obvious debugging lesson.

The Agent skips task progress, raw logs, full conversations, secrets, unverified
guesses, and facts that are obvious from the current code. When it does write a
note, the skill directs it to capture one durable conclusion rather than narrate
how the task unfolded.

See [Memory scope and lifecycle](memory-workflow.md) for the complete persistence
and retirement criteria.

## How Aikito Memory Works

During a task, the normal flow is:

```text
new task
   ↓
Agent checks whether earlier knowledge could affect the work
   ↓
Agent searches only the relevant global and project notes
   ↓
Agent completes and verifies the work
   ↓
Reusable conclusion?
   ├── No: leave Memory unchanged
   └── Yes: update one focused note in the correct scope and commit it with Git
```

Three pieces make this possible:

1. **Markdown notes hold the knowledge.** The workspace is the canonical source;
   there is no separate database or hidden Agent-specific copy.
1. **The `durable-memory` skill supplies the judgment.** It tells the Agent when
   to retrieve Memory, what is worth keeping, which scope owns it, and when an
   obsolete note should be updated or retired.
1. **Agent instructions activate the workflow.** A rule in `AGENTS.md` requires
   the Agent to apply the skill. This does not inject every note into every
   prompt. The Agent searches relevant notes only when they could affect the
   task.

Aikito creates and connects these resources; the Agent reads and curates them
while doing real work. Git makes every Memory change reviewable and recoverable.

## Where Memory Lives

Memory has two scopes:

| Scope | Canonical location | Use it for |
| --- | --- | --- |
| Global | `<workspace>/memory/notes/` | Preferences and knowledge that remain valid across projects |
| Project | `<workspace>/projects/<name>/memory/notes/` | Decisions, constraints, and lessons specific to one project |

When a project is synchronized, Aikito links its `.agents/memory/notes` entry to
the canonical project notes. This gives Agents working in the repository a
stable runtime path without creating another copy:

```text
<project>/.agents/memory/notes
└── linked to: <workspace>/projects/example/memory/notes
```

Global Memory remains in the workspace and is available across registered
projects. Project-specific knowledge should not be placed in Global Memory just
because a project has not been registered yet.

## Inspect Memory

List notes from every scope when run outside a registered project:

```bash
aikito show memory
```

When run inside a registered project directory, `aikito show memory` defaults to
displaying notes for the current project alongside Global notes. Use `--all` to
list notes across all registered projects and Global memory:

```bash
aikito show memory --all
```

To narrow the list explicitly to a specific registered project:

```bash
aikito show memory --project example
```

The output shows each note's scope, identifier, title, and project link state:

```text
┌─────────┬────────────────────────────┬────────────────────────────────┬──────┐
│ Scope   │ Note File                  │ Title                          │ Link │
├─────────┼────────────────────────────┼────────────────────────────────┼──────┤
│ Global  │ cross-agent-memory         │ Cross-agent memory rules       │ –    │
│ Global  │ skill-authoring-guidelines │ Guidelines for reusable skills │ –    │
├─────────┼────────────────────────────┼────────────────────────────────┼──────┤
│ example │ api-retry-policy           │ Retry external APIs safely     │ ✓    │
│ example │ release-checklist          │ Release verification checklist │ ✓    │
└─────────┴────────────────────────────┴────────────────────────────────┴──────┘
```

Show one note by exact name, unique prefix, or project-qualified target:

```bash
aikito show memory skill-authoring
aikito show memory release-checklist --project example
aikito show memory example/release-checklist
```

Ambiguous prefixes are rejected with the matching full identifiers.

## Edit, Rename, or Retire a Note

Open the canonical note with `$VISUAL` or `$EDITOR`:

```bash
aikito edit memory example/release-checklist
```

The first `#` heading supplies its display title. Optional `category`
frontmatter can support custom grouping, but is not required:

```markdown
---
category: Project Decisions
---

# Retry external APIs safely
```

Rename a note and update inbound `[[wikilinks]]` in the same scope:

```bash
aikito rename memory old-note-name new-note-name
```

Retire a note that no longer has decision value:

```bash
aikito rm memory example/release-checklist
```

Removal reports any remaining inbound wikilinks with their file and line
numbers. Review and commit Memory changes after verifying them.

## Enable or Disable Agent Integration

Aikito separates storage, Agent capability, and Agent behavior:

| Layer | Configuration | Effect |
| --- | --- | --- |
| Storage | `aikito init workspace` or `aikito init project` | Creates the canonical note directories |
| Capability | `durable-memory` in `skills.toml` or `agent.toml` | Makes the skill available to selected Agents after synchronization |
| Behavior | The Persistent Memory rule in `AGENTS.md` | Requires Agents to evaluate Memory relevance during tasks |

`aikito init workspace` configures all three layers by default. The integration
becomes active after synchronization:

```bash
aikito sync
```

Use `aikito sync --dry-run --verbose` for a read-only path-level preview. If the
workspace already contains Agent configuration, follow
[Adopt your existing setup](workspace-setup.md) before synchronizing.

For project-only use, select `durable-memory` in
`projects/<name>/agent.toml`, place the Persistent Memory rule in the project's
`AGENTS.md`, and synchronize that project:

```toml
skills = ["durable-memory"]
```

```bash
aikito sync project <name> --dry-run
aikito sync project <name>
```

To opt out, remove the rule and skill selection from the relevant scope, then
synchronize it again. Aikito retains existing notes as user data. Because there
is no background Memory service, disabling the Agent integration means Agents
are no longer instructed to retrieve or curate those notes.

The complete [durable-memory skill](https://github.com/lsaint/aikito/blob/main/src/aikito/templates/skills/durable-memory/SKILL.md)
is plain Markdown and documents the exact retrieval, persistence, and retirement
policy.

## Check Memory Integrity

Use `doctor` to inspect note filenames, wikilinks, staleness, and project links:

```bash
aikito doctor
aikito doctor --fix
```

`doctor --fix` repairs supported workspace configuration issues but does not
rewrite note content or remove dangling wikilinks. Older workspaces may retain
`memory/index.md`; Aikito preserves it as user data but no longer reads,
synchronizes, or requires it.

## Review a Complete Scope

Normal Agent work retrieves and updates only relevant notes. To deliberately
review every note in one scope, launch proactive maintenance:

```bash
aikito maintain memory .
aikito maintain memory global
aikito maintain memory example --agent codex
```

`.` selects the project containing the current directory. The Agent first
proposes updates, merges, moves, or retirements and waits for confirmation
before changing files. This semantic review complements `aikito doctor`, which
checks structure and freshness signals.

Complete-scope review consumes model usage in proportion to the number and size
of notes. Run it selectively and prefer a capable reasoning model. Runner
configuration is documented in the [CLI reference](cli-reference.md).

Read the [Safety model](safety.md) before pushing Memory to a remote repository.
