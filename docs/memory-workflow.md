# Memory Workflow

Aikito treats memory as curated knowledge, not a transcript archive. Persistent
memory should reduce repeated investigation and change how an Agent approaches
future work without forcing it to reread entire conversations.

## What Is Worth Keeping

A conclusion is usually worth persisting when all of the following are true:

- it is likely to remain useful;
- it has been verified through code, tests, configuration, or user confirmation;
- it will change a future decision or action;
- it cannot be recovered with a trivial search.

Temporary progress, raw debug output, secrets, credentials, unverified guesses,
and easy-to-rediscover facts should not become persistent memory.

## Scope

Aikito separates knowledge by where it remains valid:

- `memory/` contains conclusions that apply across projects;
- `projects/<name>/memory/` contains project-specific decisions and constraints.

A writing preference can belong in global memory, while an API retry policy
belongs to the affected project's memory. Agents normally use the global scope
and the current project's connected notes. These connections organize context;
they do not prevent filesystem access to other projects' files.

Do not place project-specific knowledge in global memory merely because a
project memory scope has not been created. Register the project first, then
store the conclusion in the correct scope.

## Note Structure

Each scope contains focused notes under `notes/`:

```text
memory/
└── notes/
    ├── retry-policy.md
    └── release-checklist.md
```

Keep notes directly in `notes/`; nested directories are not part of the memory
collection. `aikito doctor` warns when it finds a subdirectory that would
otherwise be invisible to memory commands.

Use one stable conclusion per note. Optional `category` frontmatter can support
custom grouping:

```markdown
---
category: Project Decisions
---

# Retry external APIs safely
```

The note filename, first heading, and body are the source of truth. Missing
`category` never invalidates a note. Obsidian-style `[[wikilinks]]` can connect
related conclusions without imposing a database or proprietary format.

## Lifecycle

The practical loop is:

1. Retrieve relevant notes before making a decision.
2. Perform the work and verify the conclusion.
3. Update an existing note or create one focused note.
4. Retire notes the work just invalidated.
5. Review the change and commit it with Git.

Step 5 is what keeps the store trustworthy. A note becomes a liability once
current code contradicts it, the thing it describes is gone, a preference has
been superseded, or a newer note states the same conclusion better. Rewrite the
note when the topic still matters, merge overlapping notes into the more
accurate one, and delete only when the topic itself stopped being worth
remembering.

An Agent may delete a note on its own when it is plainly useless — the subject
gone, the claim disproven, the content absorbed elsewhere. It should ask you
first when the call is genuinely uncertain, and especially when the note records
one of your own preferences or decisions. Removal reports inbound `[[wikilinks]]`
so the scope does not silently accumulate dangling links. Aikito keeps no tombstones or deprecation stubs —
Git history is the record of what was removed, and every memory change is
committed, so a deletion you disagree with is recoverable.

The included `durable-memory` skill gives supported Agents the same
heuristics used across Aikito workspaces.

For an intentional full-scope review, run `aikito maintain memory` with
`global`, a registered project name, or `.` for the current project. Unlike
opportunistic retirement during normal work, this command explicitly asks an
Agent to inspect the complete selected scope. It remains confirmation-gated:
the Agent proposes changes before modifying memory.

See [Work with memory](durable-memory.md) for the CLI workflow and
[Safety model](safety.md) before pushing a memory repository to a remote.
