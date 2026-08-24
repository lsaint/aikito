---
name: durable-memory
description: Git-versioned memory stored in the Aikito workspace, separate from any Agent's built-in memory. Use to search, retrieve, or persist cross-project and project-scoped durable knowledge, historical decisions, user preferences, or verified architecture constraints.
---

# Durable Memory

## Objective

Reduce redundant investigation and repeated pitfalls using a minimal, trustworthy, and searchable memory store.

Memory is a decision-support layer, not a transcript or activity log. Optimize for future decision value rather than completeness, and prefer omission over low-confidence memory.

Autonomously decide when to retrieve, follow related knowledge, or persist memory based on the current task. Do not treat this skill as a rigid step-by-step procedure, nor perform low-value reads or writes just for compliance.

## Scope & Single Source of Truth

Resolve `<workspace>` with `aikito path workspace`. All memory is stored
centrally in that workspace and managed by Git:

- **Global Memory**: `<workspace>/memory/`
  - Best for cross-project experience, user preferences, and general engineering patterns.
  - Always available regardless of whether `.agents/memory/` exists in the current project.
- **Project Memory**: `<workspace>/projects/<project-name>/memory/`
  - Best for project-specific constraints, historical architecture decisions, and project-unique debugging lessons.
  - Typically accessed via the `.agents/memory/` symlink in registered projects.

`<workspace>` is the single source of truth; `.agents/memory/` is merely a
runtime entry point without maintaining independent copies. Standard memory
CRUD operations act directly on the canonical source files above.

Standard storage layout:

```text
<workspace>/
├── memory/                         # Global memory
│   ├── index.md                    # Global memory navigation entry
│   └── notes/                      # Global atomic notes
└── projects/
    └── <project-name>/
        ├── agent.toml              # Project Agent configuration
        ├── AGENTS.md               # Project-specific rules
        └── memory/
            ├── index.md            # Project memory navigation entry
            └── notes/              # Project atomic notes
```

## Decision Principles

Proactively retrieve memory when historical knowledge could materially affect judgment (e.g., tasks involving familiar modules, recurring issues, user preferences, architecture constraints, or past decisions). Read only content relevant to the current task and avoid loading all memories by default.

Knowledge is usually worth persisting when it is likely to matter again, changes future judgment, and is not trivial to recover from the current environment.

Prefer verified knowledge. Explicit user decisions and preferences count as authoritative for their own scope.

Do **NOT** record:

- Temporary debugging outputs, transient task progress, or modified file lists.
- Unverified hypotheses or rapidly shifting guesses.
- Facts easily readable from current codebase inspection.
- Passwords, tokens, API keys, or other sensitive credentials.

Persist memory at natural work milestones when knowledge stabilizes. Do not wait for explicit user task termination, nor perform file operations on every response. Exercise restraint when value is uncertain.

## Retrieval

Choose retrieval entry points based on the task's likely scope and relevance:

- **Global entry**: `<workspace>/memory/index.md`
- **Project entry**: `.agents/memory/index.md` (when present)

Indices are for navigation only. Search the memory store using the most efficient available method. Use filenames, headings, body text, wikilinks, or other available retrieval mechanisms when useful. Follow links to related notes when necessary. When memory conflicts with current code, tests, or configuration, ground decisions in current code facts and update stale memory accordingly.

## Ownership & Persistence

Select storage scope based on knowledge applicability:

- Write cross-project knowledge to global memory.
- Write project-dependent knowledge to project memory.
- When scope is genuinely ambiguous, prefer not to persist until the ambiguity is resolved.

Project-specific knowledge belongs in project memory. Never place it in global memory merely because the project is unregistered.

If worthwhile project-specific knowledge cannot be stored because project
memory is unavailable, ask whether the user wants the project registered in
Aikito. After confirmation, use the `aikito` skill and `aikito init project` to
create the canonical project scope and runtime links before writing the memory.
Do not register a project for global memory, and otherwise avoid prompting
merely because a project is unregistered.

## Formatting & Organization

- Prefer one durable, independently reusable concept per note.
- Use stable lowercase kebab-case note names (filename stems) of at most 50 characters (e.g., `payment-idempotency`).
- Use titles that state the durable idea clearly and make the note easy to recognize from search results or wikilinks. Explain scope, rationale, and actionable guidance in the body.
- In note, Use Obsidian-style wikilinks for references: `[[note-name]]` or `[[note-name|Display Text]]`.
- In `index.md`, always use `[[note-name|Display Text]]` without a trailing description.
- Keep `index.md` strictly as a categorized index of links without expanding full note contents.

When duplication is plausible, check for related notes first and prefer updating or consolidating existing knowledge over creating another note. Correct obsolete content directly, cleaning up indices and links as needed. Rely on Git for historical tracking rather than appending changelogs to notes.

## Retirement

Memory stays trustworthy only if invalidated knowledge leaves it. A note has outlived its value when it is no longer accurate, useful, distinctive, or costly to re-derive. Judge this opportunistically while reading notes for the current task; do not sweep the whole store looking for work.

When substantially relying on or modifying a note, evaluate whether the whole note still has value.

Prefer the least destructive remedy that restores accuracy: rewrite when the topic still matters, merge when notes overlap, delete only when the topic itself stopped being worth remembering.

Act autonomously when retirement is clear; ask only when the note may still encode a valid user preference, decision, or context you cannot verify.

A retired note should leave nothing pointing at it: drop its index entry and repair inbound `[[wikilinks]]`. Keep no tombstones or deprecation stubs, since Git history already records what was removed.

## Commit & Version Control

When memory changes, stage only files directly required by that memory change
and create a local Git commit in `<workspace>`. Deletions are staged the same
way, together with the index and wikilink repairs they require, so the removal
lands as one reviewable commit. Keep memory commits narrowly scoped and
reviewable.
