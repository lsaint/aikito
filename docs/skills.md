# Manage Skills

Skills provide reusable, multi-step workflows and capabilities for your coding
agents. Aikito stores skills canonically in your workspace and connects them to
your code repositories.

The bundled `aikito` and `durable-memory` skills are system-managed snapshots
of the installed Aikito package. `aikito status`, `aikito doctor`, and
`aikito show skill` report divergent snapshots; workspace initialization and
global synchronization back up and refresh them. Do not customize these two
directories in place. Put custom workflows in separately named skills and
custom policy in global or project instructions.

## Create or import a skill

Ask your agent to create a new workflow skill:

> Create an Aikito skill named review-checklist for project example's review
> workflow. Inspect existing skills first, write the workflow, synchronize it,
> and verify the project receives it.

To manually scaffold a new skill skeleton:

```bash
aikito add skill review-checklist --project example
aikito edit skill review-checklist
```

This creates `<workspace>/skills/review-checklist/SKILL.md` and attaches it to
project `example`.

### Import an existing external skill

To import an existing external skill directory or file into Aikito and attach it
to one or more projects in a single step:

```bash
aikito add skill review-checklist --from /path/to/existing-skill --project example-a,example-b --sync
```

The skill source is imported into `<workspace>/skills/review-checklist/`; its
project selection is registered in each specified project's `agent.toml`.

To refresh that canonical snapshot after the external directory changes, repeat
the import with `--force`:

```bash
aikito add skill review-checklist --from /path/to/existing-skill --force \
  --project example-a,example-b --sync
```

The replacement is a complete snapshot: files removed from the external source
are removed from the canonical skill too. Existing project registrations are
preserved, and newly requested projects are attached. `--force` is accepted only
with `--from`, preventing accidental overwrites.

## Attach an existing skill to projects

To attach a canonical skill that already exists in `<workspace>/skills/` to
additional projects, run `add skill` with the target projects:

```bash
aikito add skill review-checklist --project example-b --sync
```

Alternatively, you can manually add the skill name to the `skills` array in
`projects/example/agent.toml`:

```toml
skills = ["durable-memory", "review-checklist"]
```

To enable a skill across all projects by default, configure it in
`<workspace>/skills.toml` and synchronize globally:

```bash
aikito sync global
```

### Inspect and verify

Preview, synchronize, and inspect the attached skill:

```bash
aikito sync project example --dry-run
aikito sync project example
aikito show project example
aikito show skill review-checklist
```

Confirm the selected skill has no missing-resource or conflict finding and its
content matches your workflow.

## Unregister or remove a skill

To unregister a skill from one or more projects while keeping its canonical
directory in `<workspace>/skills/`:

```bash
aikito rm skill review-checklist --project example-b --sync
```

To remove a skill globally (unregisters from `skills.toml` and deletes
`<workspace>/skills/<name>`):

```bash
aikito rm skill review-checklist --sync
```

If the skill is still registered in any project, global removal is blocked by
default to prevent dangling references. To unregister it from all referencing
projects and delete it globally in one command, use `--force`:

```bash
aikito rm skill review-checklist --force --sync
```

For Git-tracked skill snapshots, see
[project skill sync modes](architecture.md#project-skill-sync-modes).
Next, [Manage Instructions](instructions.md) or
[keep decisions in memory](durable-memory.md).
