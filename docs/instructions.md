# Manage Instructions

Instructions tell coding agents how to behave, defining project standards,
review guidelines, and team policies. Aikito keeps instructions in canonical,
Git-managed Markdown files in your workspace and links them outward to each
agent's expected instruction paths (such as `AGENTS.md` or `CLAUDE.md`).

## Choose the right instruction scope

Aikito supports two scopes for instructions:

- **Global instructions** (`<workspace>/global/AGENTS.md`): apply to every
  agent session across all projects on this machine.
- **Project instructions** (`<workspace>/projects/<project>/AGENTS.md`): add
  rules specific to a single repository.

Ask your agent to place a rule in the correct scope:

> Add this rule to the appropriate Aikito instruction scope: <rule>. Explain
> the scope, preserve existing instructions, and verify the affected connections.

## Inspect and edit instructions

To inspect or edit canonical instructions manually:

```bash
aikito show instructions
aikito edit instructions global
aikito edit instructions example
```

These commands open the canonical `AGENTS.md` files in your configured editor.

## Add project rules and synchronize

When a project is newly registered, its canonical `AGENTS.md` is empty. Aikito
deliberately leaves instruction links unmanaged while the canonical file is empty
to avoid overwriting unmanaged repository files prematurely.

Once you add content:

```bash
aikito edit instructions example
```

Add your rules to `<workspace>/projects/example/AGENTS.md`:

```markdown
# Project Instructions

- Include a brief verification summary when reporting completed work.
```

Save the file, then synchronize the connection:

```bash
aikito sync project example
```

Synchronization validates the plan before writing. With nonempty instructions,
Aikito links each registered agent's configured instruction path to the canonical
file.

If the target repository already contains an unmanaged instruction file,
synchronization stops and reports a conflict. Review the
[conflict guide](troubleshooting.md#existing-files-conflict) to adopt or merge
the existing file before continuing.

## Verify the connection and agent reading

Verify that the canonical rule is recorded and linked:

```bash
aikito show instructions example
aikito show project example
```

The first command displays the canonical rules. The second shows the native
instruction paths as linked and identifies the exact targets for your installed
agents.

To confirm that your coding agent actually reads the linked instructions, start a
fresh session in your project directory and ask:

> Read this project's instructions and report the rule about verification
> summaries, including the file you read.

The agent should identify the rule and the file path it loaded.

## Continue with everyday work

Next, [Manage Skills](skills.md),
[keep a decision in memory](durable-memory.md), or review
[Workspace and synchronization](architecture.md).
