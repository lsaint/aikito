# 2. Create a Workspace

Your workspace holds the source files Aikito connects to coding agents.
Keep it separate from your application repositories and any Aikito source checkout.
This step assumes [the CLI is installed](installation.md).

## Ask your agent

> Initialize my Aikito workspace at ~/aikito, or inspect it if it already exists.
> Preview global synchronization, report any conflicts, and synchronize when
> there are none. Verify the workspace path and status. Do not adopt existing
> configuration or register a project yet.

## Create and connect manually

```bash
aikito init workspace ~/aikito
aikito path workspace
```

The explicit path becomes the remembered workspace location. Initialization
creates workspace configuration and detects installed supported agents.
If you already have a workspace, use its path instead.

| Workspace path | What it holds |
| --- | --- |
| `global/AGENTS.md` | Instructions shared across projects |
| `agents.toml` | Agent integration paths and capabilities |
| `skills/` and `skills.toml` | Reusable skills and global selections |
| `projects/` | Each registered project's configuration and memory |

Preview and apply global instructions and skills:

```bash
aikito sync global --dry-run
aikito sync global
aikito status
```

If the preview reports an unmanaged existing file, follow
[conflict diagnosis](troubleshooting.md#existing-files-conflict) before applying.
Existing configuration can be imported through a reviewed
[adoption plan](safety.md#adoption).

## Verify

`aikito path workspace` should print your selected location. In
`aikito status`, check global instructions and skills for your installed agents.
Other resource classes may still need setup; this step synchronizes only global
instructions and skills. For missing agents, see
[agent detection](troubleshooting.md#an-agent-is-missing).

Optionally run `aikito web` to browse resources and status in the read-only local
Web Console.

Next: [Connect a project](project-setup.md).
