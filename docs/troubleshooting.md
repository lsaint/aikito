# Check and Repair Synchronization

Start with read-only inspection:

```bash
aikito status
aikito show project example
aikito doctor
```

Or ask your agent:

> Diagnose my Aikito synchronization status without changing files. Identify
> the affected canonical and runtime paths, explain the cause, and propose
> the smallest repair.

## A managed connection is missing

Confirm the canonical resource exists and is selected. Preview the affected
scope, then apply when the plan is correct:

```bash
aikito sync project example --dry-run
aikito sync project example
aikito show project example
```

Use `aikito sync global --dry-run` for global instructions and skills, or `aikito sync --dry-run` for the entire workspace.
Empty canonical project instructions intentionally disable their connections;
see [your first instruction](first-instruction.md).

## Existing files conflict

An unmanaged file occupies a path Aikito needs. Inspect both it and the workspace
source before choosing which content to retain. Repository instructions may
contain shared team rules.

Use `aikito adopt` to inspect supported import candidates, and read
[adoption rules](safety.md#adoption) before applying an import. Other conflicts
need explicit reconciliation or a reviewed backup and relocation of the
existing target. Re-run the preview after resolving the cause.

## A copied or generated resource has drifted

```bash
aikito diff
```

Review runtime changes and bring improvements you want to keep back into the
canonical source. Synchronize the affected scope afterward. Use `--force` only
when you have reviewed and chosen to discard runtime differences.

## An agent is missing

`aikito doctor` reports installed supported agents missing from the registry.
Review findings before using `aikito doctor --fix`, which can add bundled
defaults without replacing existing values. Agents not installed on this host
remain registered as offline for multi-machine use.

## A project is offline

A project with no locally existing candidate path is offline on this machine.
This is expected for another machine's checkout. If you expected it to be active,
inspect its paths with `aikito show project example` and correct the configuration.
See [multiple project paths](project-configuration.md#path-resolution-and-offline-semantics).

After repairs, repeat the relevant `show` command and `aikito status`.
For recovery and ownership details, consult the [Safety model](safety.md).
