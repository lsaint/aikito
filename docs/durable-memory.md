# Work with Memory

Use the memory commands to inspect available scopes, find a note, and edit the
canonical file without navigating the workspace manually.

## Default Behavior and Opt-Out

Aikito separates Memory storage, Agent capability, and Agent behavior:

| Layer | Configuration | Effect |
| --- | --- | --- |
| Storage | `aikito init workspace` or `aikito init project` | Creates the canonical Memory directories and indices. |
| Capability | `durable-memory` in global `skills.toml` or project `agent.toml` | Makes the skill available to the selected Agents after synchronization. |
| Behavior | A rule in global or project `AGENTS.md` | Tells Agents when they must apply the skill, including whether every task should evaluate Memory relevance. |

`aikito init workspace` configures all three layers by default. It copies the
bundled `aikito` and `durable-memory` skills into `skills/`, selects both in
`skills.toml`, and writes this rule to `global/AGENTS.md`:

```markdown
- All tasks must follow the `durable-memory` skill as the single source of truth
  for durable memory boundaries, retrieval, evaluation, and persistence.
```

Initialization changes only the Aikito workspace. The integration becomes
active only after an explicit synchronization:

```bash
aikito sync global --dry-run
aikito sync global
```

If existing Agent instructions are detected, run `aikito adopt` before
synchronizing. When all detected Agent instructions agree and the canonical
file is still Aikito's default, adoption preserves the user content and appends
the default Memory rule exactly once. Different Agent instructions or a
separately customized canonical file remain conflicts for manual review; Aikito
does not overwrite them.

For project-only use, remove the global selection and rule, then add the skill
to `projects/<name>/agent.toml`:

```toml
skills = ["durable-memory"]
```

Place the behavior rule in `projects/<name>/AGENTS.md`, then run:

```bash
aikito sync project <name> --dry-run
aikito sync project <name>
```

The instruction does not mean reading or writing Memory on every task. The
skill still decides when historical knowledge is relevant and when a conclusion
has enough future value to persist. The instruction makes that evaluation
mandatory rather than leaving the workflow merely available.

To opt out, remove the Persistent Memory rule and remove `durable-memory` from
the relevant skill list, then synchronize that scope again. The bundled skill
directory, existing notes, and canonical Memory directories are retained as
user data; opting out does not delete them. Aikito has no background Memory
service, so no automatic capture or prompt injection continues after the Agent
integration is disabled.

## Use or Adapt the Prompt

The complete [durable-memory prompt](../templates/skills/durable-memory/SKILL.md)
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

## Rename a Note

```bash
aikito rename memory old-note-name new-note-name
```

The command atomically renames the note file, updates its entry in `index.md`,
and refactors all inbound `[[wikilinks]]` pointing to the note across the entire
workspace.

## Retire a Note

```bash
aikito rm memory example/release-checklist
```

The command deletes the note file, removes its entry from `index.md`, and scans
the workspace for any remaining inbound `[[wikilinks]]`, reporting their exact
file and line numbers so you can review and adjust referencing notes.

## Memory Integrity and Auto-Repair

Run `aikito doctor` to inspect memory note filename validity, index consistency,
and staleness:

```bash
aikito doctor
aikito doctor --fix
```

With `--fix`, Aikito safely reconciles mechanical `index.md` formatting:
- Prunes dangling index entries pointing to non-existent notes;
- Normalizes non-standard index entries into the canonical `[[note-stem|Display Text]]` syntax using the note's heading title and removing trailing descriptions.

Missing notes and dangling wikilinks in note bodies are reported by diagnostics
without being automatically appended or removed, preserving the curated
categorization of `index.md` and placeholder markers for future knowledge.

Memory has two scopes:

- `memory/` for cross-project knowledge;
- `projects/<name>/memory/` for project-specific decisions and constraints.

## Proactive Scope Maintenance

Use an interactive Agent to review every note in one selected scope:

> **Usage note:** A complete-scope review consumes model usage in proportion to
> the number and size of notes. Run it selectively, and prefer a capable
> reasoning model for more reliable decisions about accuracy, duplication,
> consolidation, and retirement.

```bash
aikito maintain memory .
aikito maintain memory global
aikito maintain memory example --agent codex
```

This is the semantic counterpart to `aikito doctor`: doctor detects structural
and freshness signals, while the Agent evaluates accuracy, duplication, scope
ownership, and continued decision value. The generated prompt requires a
proposal first and forbids file changes or commits until you confirm it.
It also compares memory with relevant canonical skills and instructions,
reports upstream corrections separately, and asks you to resolve conflicts
that cannot be verified from objective evidence.

The default target, `.`, resolves the registered project containing the current
directory. Use `global` or a registered project name to select another scope,
and `--agent` to choose a configured runner. Aikito invokes the runner command
directly, so shell aliases are not expanded; put required arguments and
environment overrides in `agents.toml`. See the
[CLI reference](cli-reference.md) for runner configuration and placeholders.

Read [Memory workflow](memory-workflow.md) before deciding what to persist and
[Safety model](safety.md) before pushing memory to a remote repository.
