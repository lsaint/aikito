---
name: durable-memory
description: Git-versioned memory stored in the Aikito workspace, separate from any Agent's built-in memory. Use to search, retrieve, or persist cross-project and project-scoped durable knowledge, historical decisions, user preferences, or verified architecture constraints.
---

# Durable Memory

## Objective

Reduce redundant investigation and repeated pitfalls using a minimal, trustworthy, and searchable memory store.

Autonomously decide when to retrieve, follow related knowledge, or persist memory based on the current task. Do not treat this skill as a rigid step-by-step procedure, nor perform low-value reads or writes just for compliance.

## Scope & Single Source of Truth

All memory is stored centrally in `~/aikito` and managed by Git:

- **Global Memory**: `~/aikito/memory/`
  - Best for cross-project experience, user preferences, and general engineering patterns.
  - Always available regardless of whether `.agents/memory/` exists in the current project.
- **Project Memory**: `~/aikito/projects/<project-name>/memory/`
  - Best for project-specific constraints, historical architecture decisions, and project-unique debugging lessons.
  - Typically accessed via the `.agents/memory/` symlink in registered projects.

`~/aikito` is the single source of truth; `.agents/memory/` is merely a runtime entry point without maintaining independent copies. Standard memory CRUD operations act directly on the canonical source files above.

Standard storage layout:

```text
~/aikito/
├── memory/                         # Global memory
│   ├── index.md                    # Global memory navigation entry
│   ├── notes/                      # Global atomic notes
│   └── README.md                   # Optional description
└── projects/
    └── <project-name>/
        ├── agent.toml              # Project Agent configuration
        ├── AGENTS.md               # Project-specific rules
        └── memory/
            ├── index.md            # Project memory navigation entry
            ├── notes/              # Project atomic notes
            └── README.md           # Optional description
```

## Decision Principles

Proactively retrieve memory when historical knowledge could materially affect judgment (e.g., tasks involving familiar modules, recurring issues, user preferences, architecture constraints, or past decisions). Read only content relevant to the current task and avoid loading all memories by default.

Knowledge worth persisting typically exhibits all of the following traits:

- Likely to be useful in future tasks.
- Verified through code, tests, configuration, or explicit user confirmation.
- Alters future Agent judgment or action.
- Difficult to re-derive from simple search or inspection alone.

Do **NOT** record:

- Temporary debugging outputs, transient task progress, or modified file lists.
- Unverified hypotheses or rapidly shifting guesses.
- Facts easily readable from current codebase inspection.
- Passwords, tokens, API keys, or other sensitive credentials.

Persist memory at natural work milestones when knowledge stabilizes. Do not wait for explicit user task termination, nor perform file operations on every response. Exercise restraint when value is uncertain.

## Retrieval

Choose appropriate entry points based on task keywords:

- **Global entry**: `~/aikito/memory/index.md`
- **Project entry**: `.agents/memory/index.md` (when present)

Indices are for navigation only. Search filenames, headings, body text, and `[[wikilinks]]` as needed, following links to related notes when necessary. When memory conflicts with current code, tests, or configuration, ground decisions in current code facts and update stale memory accordingly.

## Ownership & Persistence

Select storage scope based on knowledge applicability:

- Write cross-project knowledge to global memory.
- Write project-dependent knowledge to project memory.
- Refrain from writing when ownership is ambiguous.

Global memory remains fully operational when a project is not registered in Aikito. If project-unique knowledge emerges:

- Do **NOT** downgrade it into global memory.
- Ask the user if they wish to register the project in Aikito and link `.agents/memory/`.
- With user consent, use the `aikito` skill to register the project and verify its `.agents/memory/` link before persisting project-specific knowledge.
- If the user declines, do not persist the project-specific knowledge.

Prompt the user only when genuinely project-unique knowledge needs saving. Avoid routine prompts solely because a project is unregistered.
Also use the `aikito` skill when an existing project's memory link needs setup, repair, or adjustment.

## Formatting & Organization

- Express exactly one standalone concept per note.
- Use lowercase kebab-case filenames that remain stable (e.g., `payment-idempotency.md`).
- Title notes with clear, definitive conclusions, explaining scope, rationale, and actionable guidance in the body.
- Use Obsidian-style wikilinks for references: `[[note-name]]` or `[[note-name|Display Text]]`.
- Keep `index.md` strictly as a categorized index of links without expanding full note contents.

Search for existing or similar notes before writing. Prefer updating existing notes to avoid duplication; correct obsolete content directly, cleaning up indices and links as needed. Rely on Git for historical tracking rather than appending changelogs to notes.

## Commit & Version Control

When memory changes, stage only the modified memory files and create a local Git commit in `~/aikito`. Never mix unrelated workspace code or config changes into memory commits.
