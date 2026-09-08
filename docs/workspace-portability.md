# Connect a Workspace on Another Machine

Use this guide when you already have a Git-managed Aikito workspace and want
to connect it on another host. For a new workspace, start with
[Create a workspace](workspace-setup.md).

## Ask your agent

> Connect my existing Aikito workspace at <local path> on this machine. Inspect
> the workspace, installed agents, and local project paths. Preview synchronization
> and report conflicts before applying it. Preserve offline entries and verify
> the active resources. Do not publish the workspace.

## Connect the existing workspace

Install Aikito, then clone or download your own workspace repository to
`~/aikito` (or another local path). Keep it separate from the CLI installation.
Register that location:

```bash
aikito init workspace ~/aikito
aikito path workspace
aikito doctor
```

For an existing recognized workspace, initialization records the local pointer
without changing the workspace files or agent runtime configuration. Review
doctor findings for installed agents missing from the registry; see
[agent detection](troubleshooting.md#an-agent-is-missing).

Preview the resources that will be connected on this host:

```bash
aikito sync --dry-run
```

Resolve any [existing-file conflicts](troubleshooting.md#existing-files-conflict)
before applying:

```bash
aikito sync
aikito status
```

Agents not installed on this host and projects with no locally existing path
remain offline. Preserve those entries for the machines that use them.
Use [multiple project paths](project-configuration.md#path-resolution-and-offline-semantics)
for different checkouts, worktrees, or operating systems.

## Custom workspace paths

An explicit `aikito init workspace <path>` remembers that path for later commands.
`aikito path workspace` prints the active location. One workspace can manage
many code projects; project registration stores their resources, not their source code.

`AIKITO_DIR` temporarily overrides workspace resolution, which is useful for
CI and isolated automation. For a read-only check on POSIX shells:

```bash
AIKITO_DIR=/path/to/workspace aikito path workspace
```

On PowerShell, `$env:AIKITO_DIR` sets the override for the current session and
child processes. Restore its previous value afterward. Automation that runs
initialization must also isolate the user configuration location used for
persistent workspace pointers; a temporary resource directory alone is not
sufficient isolation.

Before publishing or sharing the workspace repository, review the
[Git and memory privacy rules](safety.md#git-and-memory-privacy).
