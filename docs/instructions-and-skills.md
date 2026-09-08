# Manage Instructions and Skills

Use instructions for rules an agent should follow, and skills for reusable
workflows. For your first setup, complete [your first instruction](first-instruction.md).

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

## Create a project skill

> Create an Aikito skill named review-checklist for project example's review
> workflow. Inspect existing skills first, write the workflow, synchronize it,
> and verify the project receives it.

Manually create a skeleton and open its instructions:

```bash
aikito add skill review-checklist --project example
aikito edit skill review-checklist
```

Replace the skeleton with your workflow before synchronizing. The skill source
lives under `<workspace>/skills/review-checklist/`; its project selection is
registered in `projects/example/agent.toml`.

## Select an existing skill

Ensure the skill exists in `<workspace>/skills/`. Add its name to the existing
`skills` array in `projects/example/agent.toml`, preserving selections you need:

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
