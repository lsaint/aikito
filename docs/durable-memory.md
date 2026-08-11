# Work with Memory

Use the memory commands to inspect available scopes, find a note, and edit the
canonical file without navigating the workspace manually.

## Use or Adapt the Prompt

The complete [durable-memory prompt](../skills/durable-memory/SKILL.md)
is plain Markdown. You can use it as provided or copy and adapt it to match your
own storage layout, naming conventions, review process, and criteria for what
deserves persistent memory. Review the prompt before enabling it so its scope
and write behavior match your workflow.

## List Memory

```bash
aikito show memory
```

The output lists global and project scopes, note identifiers, index state, and
project link state.

Example output from a configured workspace:

```text
┌─────────┬────────────────────────────┬────────────────────────────────┬───────┬──────┐
│ Scope   │ Note File                  │ Title                          │ Index │ Link │
├─────────┼────────────────────────────┼────────────────────────────────┼───────┼──────┤
│ Global  │ cross-agent-memory         │ Cross-agent memory rules       │ ✓     │ –    │
│ Global  │ skill-authoring-guidelines │ Guidelines for reusable skills │ ✓     │ –    │
├─────────┼────────────────────────────┼────────────────────────────────┼───────┼──────┤
│ example │ api-retry-policy           │ Retry external APIs safely     │ ✓     │ ✓    │
│ example │ release-checklist          │ Release verification checklist │ ✓     │ ✓    │
└─────────┴────────────────────────────┴────────────────────────────────┴───────┴──────┘
```

## Show a Note

```bash
aikito show memory skill-authoring
```

Targets may be an exact note name, a qualified project path, or any unique
prefix displayed in the `Note File` column. A copied truncated value such as
`skill-authoring-guideli…` also works. Ambiguous prefixes are rejected with the
matching full identifiers.

## Edit a Note

```bash
aikito edit memory example/release-checklist
```

The command opens the canonical note with `$VISUAL` or `$EDITOR`. Review and
commit the resulting change in the Aikito workspace after verifying the
conclusion.

## Retire a Note

There is no `delete` command. Removing a note is a deliberate manual edit in the
canonical workspace, because it must stay consistent with the rest of the scope:

```bash
cd ~/aikito
rm memory/notes/<note>.md      # or projects/<name>/memory/notes/<note>.md
grep -rn '<note>' memory projects   # find the index entry and inbound wikilinks
```

Delete the `index.md` entry and repair every `[[wikilink]]` the grep reports,
then commit the removal and its cleanups as one change. `aikito show memory`
afterwards confirms the scope still lists what you expect.

Memory has two scopes:

- `memory/` for cross-project knowledge;
- `projects/<name>/memory/` for project-specific decisions and constraints.

Read [Memory workflow](memory-workflow.md) before deciding what to persist and
[Safety model](safety.md) before pushing memory to a remote repository.
