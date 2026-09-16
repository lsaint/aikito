# 2. Create a Workspace

Your workspace holds the source files Aikito connects to coding agents.
Keep it separate from your application repositories and any Aikito source checkout.
This step assumes [the CLI is installed](installation.md).

## Ask your agent

> Initialize my Aikito workspace at ~/aikito, or inspect it if it already exists.
> If existing Agent configuration is detected, run `aikito adopt` before
> synchronization. Then run `aikito sync` and verify the workspace path and
> status. Both commands preflight before writing; if either stops, explain the
> findings and ask before skipping or forcing anything. Do not register a
> project yet.

## Create and connect manually

```bash
aikito init workspace ~/aikito
aikito path workspace
```

The explicit path becomes the remembered workspace location. Initialization
creates workspace configuration and detects installed supported agents.
If you already have a workspace, use its path instead.
For a cloned workspace on another machine, follow
[Connect a workspace on another machine](workspace-portability.md).

If Aikito reports existing Agent configuration, import it before synchronization:

```bash
aikito adopt
```

Adoption first validates every detected resource. It writes only when the
complete plan is safe and leaves the original Agent configuration unchanged.

| Workspace path | What it holds |
| --- | --- |
| `global/AGENTS.md` | Instructions shared across projects |
| `agents.toml` | Agent integration paths and capabilities |
| `skills/` and `skills.toml` | Reusable skills and global selections |
| `subagents.toml` | Subagent personas configured across agents |
| `mcps/` | Model Context Protocol configurations |
| `projects/` | Each registered project's configuration and memory |

Synchronize workspace resources:

```bash
aikito sync
aikito status
```

The command builds a complete read-only plan first and writes only when every
scope is safe. Its default output is a concise summary. Use
`aikito sync --dry-run` to stop after planning, and add `--verbose` when exact
items and paths are needed. If the plan reports an unmanaged existing file, follow
[conflict diagnosis](troubleshooting.md#existing-files-conflict) before applying.
See the [adoption plan](safety.md#adoption) for source, backup, and write boundaries.

## Verify

`aikito path workspace` should print your selected location. In
`aikito status`, check global instructions, skills, subagents, and MCP
servers for your installed agents. For missing agents, see
[agent detection](troubleshooting.md#an-agent-is-missing).

Optionally run `aikito web` to browse resources and status in the read-only local
Web Console.

Next: [Connect a project](project-setup.md).
