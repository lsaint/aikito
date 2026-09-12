# 3. Connect a Project

Connect an existing code repository to your [initialized workspace](workspace-setup.md).
We use `~/code/example`; replace it with your repository path.
The directory name becomes the project name.

## Ask your agent

> Connect ~/code/example as an Aikito project named example. Inspect existing
> instructions and .agents resources first. If there are conflicts, explain
> them before changing anything. Verify the registered path and project memory.

## Register manually

From your existing repository:

```bash
cd ~/code/example
aikito init project
aikito show project example
```

Initialization registers and synchronizes the project. It creates these source
files inside your **Aikito workspace**, separate from the code repository:

```text
<workspace>/projects/example/
├── agent.toml
├── AGENTS.md
└── memory/
    └── notes/
```

`agent.toml` records the repository path and selected skills. `memory/` holds
project-specific knowledge. The repository's `.agents/memory/` connects to
that directory; selected skills are exposed under `.agents/skills/`.

The new canonical `AGENTS.md` is empty. Instruction synchronization stays
disabled until you add content in the next step. An existing repository-owned
`AGENTS.md` stays untouched while canonical instructions are empty.

## Verify

In `aikito show project example`, confirm the repository path, workspace project
directory, and memory connection. Empty instructions are expected at this stage.
Investigate reported conflicts using [synchronization troubleshooting](troubleshooting.md).

If you used another directory name, substitute it for `example` in subsequent
commands. For explicit names, multiple worktrees, or skill copy mode, see
[Advanced project setup](project-configuration.md).

Next: [Add and verify your first instruction](first-instruction.md).
