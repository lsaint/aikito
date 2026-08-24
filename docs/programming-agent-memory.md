# Memory Needs a Maintainer

## Programming the Agent's Cognitive Environment

AI can maintain memory, but it cannot be the sole governor of memory.

That distinction is central to Aikito.

A coding agent is good at searching notes, extracting durable conclusions,
repairing links, detecting stale statements, and applying a migration across
many files. Those capabilities make lightweight automated memory practical.
They do not remove the need for a person to decide what the memory means, where
it belongs, or whether it should still exist.

An entirely automated memory system can remain internally consistent while
becoming conceptually wrong.

## A real refactoring case

Chat Distiller originally lived inside the Aikito workspace. Its source was
edited there and exported into a separate public repository. Its architecture,
product, compliance, and release memories therefore lived under the Aikito
project as well.

Once Chat Distiller became capable of independent development, an agent could
handle most of the mechanical migration:

- compare the canonical and exported repositories;
- move the source of truth into the public repository;
- preserve packaging, privacy, and verification gates;
- register a separate Aikito project;
- move notes, repair indexes and links, synchronize runtimes, and run tests.

But automation alone did not identify the right final design. Human review
introduced several semantic corrections.

First, moving the source code was not enough. Chat Distiller's memory still
belonged to the Aikito project, so its cognitive scope contradicted its new
repository boundary. The project had to be registered independently, and its
five product-specific notes had to move with it.

Second, one shared release skill still contained the release procedures for
Aikito, Chat Distiller, and another Python package. It was technically reusable
but conceptually shallow: each product had different sources, artifacts,
validation gates, and publication channels. It was replaced by three
self-contained project skills.

Third, versioning rules remained in a standalone memory note even though they
were mandatory release behavior. The note was retired and the rules were moved
into each release skill. Human review then corrected one more detail: approval
was required not only for version `2.0.0`, but for every major version,
including `1.0.0`, `2.0.0`, and later major releases.

At every stage, the agent could keep files consistent. The person decided what
the system ought to represent.

## Maintenance is not governance

Memory maintenance handles operations such as:

- retrieving relevant knowledge;
- proposing durable conclusions;
- updating stale wording;
- finding duplicates and broken links;
- moving notes and validating the resulting structure.

Memory governance asks different questions:

- Is this still true?
- Is it knowledge, an executable policy, or temporary context?
- Is its scope global, project-specific, or obsolete?
- Should two concepts share an abstraction, or evolve independently?
- Which rule expresses the author's real intent?
- What should the agent forget?

The first group is highly automatable. The second requires responsibility,
product judgment, and an understanding of future direction.

This does not mean people should manually edit every note. A better loop is:

```text
AI retrieves, audits, and proposes
                ↓
Human reviews semantic boundary changes
                ↓
AI migrates, repairs, tests, and reports
                ↓
Git preserves the decision and its history
```

Human participation should concentrate where a change affects meaning,
ownership, policy, or lifecycle—not on repetitive file operations.

## A new layer of programming

In traditional programming, developers primarily describe what a machine
should execute. With coding agents, developers also shape the environment in
which another reasoning system operates.

| Traditional software | Agent cognitive environment |
| --- | --- |
| Persistent state | Memory |
| Executable logic | Skills |
| Configuration and policy | Instructions |
| Module boundary | Project scope |
| Refactoring and migration | Split, merge, move, or retire knowledge |
| Tests and observability | Verifiers, status checks, and Git diffs |

This is a form of higher-level programming. The object being programmed is not
only the application. It is also what the agent can see, remember, follow, and
forget.

The programmer's role therefore shifts upward:

- from producing every implementation to defining intent and evaluating output;
- from managing only code architecture to managing cognitive architecture;
- from writing isolated prompts to maintaining durable context and executable
  policies;
- from accepting accumulated knowledge to continuously refactoring it.

Code still matters. But memory, skills, and instructions increasingly determine
whether agents can change that code safely and consistently.

## Why Aikito keeps memory as files

Aikito does not promise a magical autonomous memory that silently records
everything. It keeps memory and agent resources as scoped, reviewable files
because governance needs visible boundaries.

Plain files and Git make it possible to:

- inspect exactly what an agent will inherit;
- review AI-proposed changes before accepting them;
- move knowledge when project ownership changes;
- turn a remembered rule into an executable skill;
- retire obsolete knowledge without leaving hidden state behind;
- understand when and why the cognitive environment changed.

The goal is not to remove the human from memory management. The goal is to make
human judgment effective at a higher level while agents perform the maintenance
work beneath it.

In the AI era, programmers do not merely program software. They increasingly
program the cognitive environment that programs software with them.

[简体中文](programming-agent-memory.zh-CN.md)
