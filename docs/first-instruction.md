# 4. Verify Your First Instruction

You now have a workspace and a [registered project](project-setup.md).
Add a visible rule, check its connection, and confirm your agent can read it.

## Ask your agent

> Add “Include a brief verification summary when reporting completed work” to
> the canonical instructions for project example. Preserve existing content,
> preview project synchronization, and apply it if there are no conflicts.
> Show the instruction source and verify the native project instruction link.

## Edit and synchronize manually

Open the canonical file in your configured editor:

```bash
aikito edit instructions example
```

Alternatively, edit `<workspace>/projects/example/AGENTS.md` directly. Add:

```markdown
# Project Instructions

- Include a brief verification summary when reporting completed work.
```

Save the file, then preview and apply the connection:

```bash
aikito sync project example --dry-run
aikito sync project example
```

With nonempty instructions, Aikito links each registered agent's configured
project instruction path to the canonical file. If a repository already owns
one of those files, synchronization reports a conflict. Follow the
[conflict guide](troubleshooting.md#existing-files-conflict) to reconcile it
before continuing.

## Verify the file and the agent

```bash
aikito show instructions example
aikito show project example
```

The first command should print the new rule. The second should show the native
instruction paths as linked and identify the actual paths for your agents.

Start a fresh coding-agent session in `~/code/example` and ask:

> Read this project's instructions and report the rule about verification
> summaries, including the file you read.

The agent should identify the rule and its instruction file. This checks that
it can read the connected content; future compliance also depends on the
agent's instruction-loading behavior. If it cannot find the rule, inspect the
reported path and your agent's project-instruction support.

## Continue with everyday work

You have completed the basic loop: edit the canonical file, synchronize its
connection, and verify the result. Linked instruction content reflects future
workspace edits directly; synchronize when changing resource selections or
repairing connections.

Next, [manage instructions and skills](instructions-and-skills.md),
[keep a decision in memory](durable-memory.md), or read
[Workspace and synchronization](architecture.md).
