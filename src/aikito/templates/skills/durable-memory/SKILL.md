---
name: durable-memory
description: Git-versioned memory stored in the Aikito workspace, separate from any Agent's built-in memory. Use to search, retrieve, or persist cross-project and project-scoped durable knowledge, historical decisions, user preferences, or verified architecture constraints.
---

# Durable Memory

## Objective

Reduce redundant investigation and repeated pitfalls using a minimal, trustworthy, and searchable memory store.

Memory is a decision-support layer, not a transcript or activity log. Optimize for future decision value rather than completeness, and prefer omission over low-confidence memory.

Decide autonomously when to retrieve, follow related knowledge, or persist. This skill is not a rigid step-by-step procedure: never read or write merely for compliance, and exercise restraint when value is uncertain.

## Scope & Single Source of Truth

Resolve `<workspace>` with `aikito path workspace`. All memory lives there under Git:

- **Global Memory** — `<workspace>/memory/`: cross-project experience, user preferences, general engineering patterns. Always available.
- **Project Memory** — `<workspace>/projects/<project-name>/memory/`: project-specific constraints, historical architecture decisions, project-unique debugging lessons. Usually reached through the `.agents/memory/` symlink in registered projects.

Each scope holds an `index.md` navigation entry and a `notes/` directory of atomic notes. `.agents/memory/` is only a runtime entry point and keeps no independent copy; all memory operations act directly on the canonical files above.

## Decision Principles

Retrieve proactively when historical knowledge could materially affect judgment (familiar modules, recurring issues, user preferences, architecture constraints, past decisions). Read only what is relevant to the current task; never load all memories by default.

Knowledge is usually worth persisting when it is likely to matter again, changes future judgment, and is not trivial to recover from the current environment. Prefer verified knowledge; explicit user decisions and preferences are authoritative for their own scope.

Do **NOT** record:

- Temporary debugging outputs, transient task progress, or modified file lists.
- Unverified hypotheses or rapidly shifting guesses.
- Facts easily readable from current codebase inspection.
- Passwords, tokens, API keys, or other sensitive credentials.

Persist at natural work milestones when knowledge stabilizes — neither waiting for explicit user task termination nor writing files on every response.

## Retrieval

Enter through `<workspace>/memory/index.md` or, when present, `.agents/memory/index.md`, choosing by the task's likely scope. Search with the most efficient method available, using filenames, headings, body text, or wikilinks, and follow links to related notes when necessary. When memory conflicts with current code, tests, or configuration, ground decisions in current code facts and update the stale memory.

## Ownership & Persistence

Write cross-project knowledge to global memory and project-dependent knowledge to project memory. When scope is genuinely ambiguous, do not persist until the ambiguity is resolved. An unregistered project is never a reason to put project-specific knowledge in global memory.

If worthwhile project-specific knowledge has nowhere to go because project memory is unavailable, ask whether to register the project; after confirmation use the `aikito` skill and `aikito init project` to create the canonical scope and runtime links, then write the note. Never register a project just to store global memory, and otherwise do not prompt merely because a project is unregistered.

## Formatting & Organization

- Prefer one durable, independently reusable concept per note.
- Use stable lowercase kebab-case note names (filename stems) of at most 50 characters (e.g., `payment-idempotency`).
- In project memory, omit the project name as a filename prefix unless it prevents a real ambiguity within that project.
- Use titles that state the durable idea clearly and stay recognizable in search results or wikilinks; explain scope, rationale, and actionable guidance in the body.
- Reference other notes with Obsidian-style wikilinks: `[[note-name]]` or `[[note-name|Display Text]]`.
- Keep `index.md` strictly a categorized list of `[[note-name|Display Text]]` links, without trailing descriptions or note contents.

When duplication is plausible, check related notes first and prefer updating or consolidating existing knowledge over adding another note. Correct obsolete content directly, cleaning up indices and links as needed; Git history replaces in-note changelogs.

## Retirement

Memory stays trustworthy only if invalidated knowledge leaves it. A note has outlived its value when it is no longer accurate, useful, distinctive, or costly to re-derive. Judge this opportunistically whenever you substantially rely on or modify a note; do not sweep the whole store looking for work.

Prefer the least destructive remedy that restores accuracy: rewrite when the topic still matters, merge when notes overlap, delete only when the topic itself stopped being worth remembering. Act autonomously when retirement is clear; ask only when the note may still encode a valid user preference, decision, or context you cannot verify.

A retired note leaves nothing pointing at it: drop its index entry and repair inbound `[[wikilinks]]`. Keep no tombstones or deprecation stubs.

## Commit & Version Control

When memory changes, stage only the files that change requires — including the index entries and wikilink repairs a deletion forces — and create one narrowly scoped, reviewable local Git commit in `<workspace>`.
