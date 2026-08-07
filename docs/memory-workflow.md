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

Do not place project-specific knowledge in global memory merely because a
project memory scope has not been created. Register the project first, then
store the conclusion in the correct scope.

## Note Structure

Each scope contains an `index.md` and focused notes under `notes/`:

```text
memory/
├── index.md
└── notes/
    ├── retry-policy.md
    └── release-checklist.md
```

Use one stable conclusion per note. Keep `index.md` as navigation rather than a
second copy of the note contents. Obsidian-style `[[wikilinks]]` can connect
related conclusions without imposing a database or proprietary format.

## Lifecycle

The practical loop is:

1. Retrieve relevant notes before making a decision.
2. Perform the work and verify the conclusion.
3. Update an existing note or create one focused note.
4. Link it from the scope index when useful.
5. Retire notes the work just invalidated.
6. Review the change and commit it with Git.

Step 5 is what keeps the store trustworthy. A note becomes a liability once
current code contradicts it, the thing it describes is gone, a preference has
been superseded, or a newer note states the same conclusion better. Rewrite the
note when the topic still matters, merge overlapping notes into the more
accurate one, and delete only when the topic itself stopped being worth
remembering.

An Agent may delete a note on its own when it is plainly useless — the subject
gone, the claim disproven, the content absorbed elsewhere. It should ask you
first when the call is genuinely uncertain, and especially when the note records
one of your own preferences or decisions. Either way the removal takes its
`index.md` entry and inbound `[[wikilinks]]` with it, so the scope never
accumulates dangling links. Aikito keeps no tombstones or deprecation stubs —
Git history is the record of what was removed, and every memory change is
committed, so a deletion you disagree with is recoverable.

The included `durable-memory` skill gives supported Agents the same
heuristics used across Aikito workspaces.

See [Work with memory](durable-memory.md) for the CLI workflow and
[Safety model](safety.md) before pushing a memory repository to a remote.
