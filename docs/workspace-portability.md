# Connect a Workspace on Another Machine

Use this guide when you already have a Git-managed Aikito workspace and want
to connect it on another host. For a new workspace, start with
[the getting-started guide](installation.md).

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

Synchronize the resources that are available on this host:

```bash
aikito sync
```

The command checks all scopes before writing and stops the whole operation on a
conflict. Use `aikito sync --dry-run` for a read-only summary or
`aikito sync --dry-run --verbose` to inspect every path. If synchronization is
blocked, resolve any
[existing-file conflicts](troubleshooting.md#existing-files-conflict), then rerun:

```bash
aikito sync
aikito status
```

Agents not installed on this host and projects with no locally existing path
remain offline. Preserve those entries for the machines that use them.
Use [multiple project paths](project-configuration.md#path-resolution-and-offline-semantics)
for different checkouts, worktrees, or operating systems.

## Merge another workspace

If this machine already has an active workspace, you can import resources from
another workspace instead of replacing the active one with a Git clone. Preview
the complete merge before applying it:

```bash
aikito import workspace /path/to/other-workspace --dry-run
aikito import workspace /path/to/other-workspace
```

The import preserves resources that exist only in the active workspace and
creates missing projects even when their code directories are not on this host.
Resolve reported conflicts before retrying. After importing, run
`aikito sync --dry-run`; once a project has been cloned locally, bind it with
`aikito sync project <name> <path>`. Review the resulting workspace changes with
`aikito git` before committing them.

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
