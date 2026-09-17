# Manage Instructions and Skills

Use instructions for rules an agent should follow, and skills for reusable
workflows. If adoption did not import an instruction you need, follow
[create an instruction from scratch](first-instruction.md).

The bundled `aikito` and `durable-memory` skills are system-managed snapshots
of the installed Aikito package. `aikito status`, `aikito doctor`, and
`aikito show skill` report divergent snapshots; workspace initialization and
global synchronization back up and refresh them. Do not customize these two
directories in place. Put custom workflows in separately named skills and
custom policy in global or project instructions.

## Edit instructions in the right scope

Global instructions apply across projects; project instructions add rules for
one repository. Ask your agent:

> Add this rule to the appropriate Aikito instruction scope: <rule>. Explain
> the scope, preserve existing instructions, and verify the affected connections.

To inspect and edit manually:

```bash
aikito show instructions
aikito edit instructions global
aikito edit instructions example
```

These edit `global/AGENTS.md` and `projects/example/AGENTS.md` in the workspace.
Use `aikito sync global` or `aikito sync project example` to establish or repair
connections, previewing with `--dry-run` first.

## Create or import a project skill

> Create an Aikito skill named review-checklist for project example's review
> workflow. Inspect existing skills first, write the workflow, synchronize it,
> and verify the project receives it.

Manually create a skeleton and open its instructions:

```bash
aikito add skill review-checklist --project example
aikito edit skill review-checklist
```

To import an existing external skill directory or file into Aikito and attach it to one or more projects in a single step:

```bash
aikito add skill review-checklist --from /path/to/existing-skill --project example-a,example-b --sync
```

The skill source is imported into `<workspace>/skills/review-checklist/`; its project selection is registered in each specified project's `agent.toml`.

To refresh that canonical snapshot after the external directory changes, repeat
the import with `--force`:

```bash
aikito add skill review-checklist --from /path/to/existing-skill --force \
  --project example-a,example-b --sync
```

The replacement is a complete snapshot: files removed from the external source
are removed from the canonical skill too. Repeat the original registration
options; existing global and project registrations are preserved, while any new
requested projects are added. `--force` is accepted only with `--from`, so it
cannot overwrite a canonical skill unless a replacement snapshot is supplied
explicitly. Aikito does not yet track external-source provenance, so review
local canonical changes before replacing them.

## Select an existing skill

To attach a canonical skill that already exists in `<workspace>/skills/` to additional projects, run `add skill` with the target projects:

```bash
aikito add skill review-checklist --project example-b --sync
```

Alternatively, you can manually add its name to the `skills` array in `projects/example/agent.toml`:

```toml
skills = ["durable-memory", "review-checklist"]
```

Preview, synchronize, and inspect:

```bash
aikito sync project example --dry-run
aikito sync project example
aikito show project example
aikito show skill review-checklist
```

Confirm the selected skill has no missing-resource or conflict finding and its
content matches your workflow. Global selections live in `skills.toml`; inspect
its generated comments before editing and use global synchronization.

For Git-tracked skill snapshots, see
[project skill sync modes](architecture.md#project-skill-sync-modes).
