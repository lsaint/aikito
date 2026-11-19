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
see [Manage Instructions](instructions.md) when that is not
intentional.

## Existing files conflict

An unmanaged file occupies a path Aikito needs. Inspect both it and the workspace
source before choosing which content to retain. Repository instructions may
contain shared team rules.

Use `aikito adopt --dry-run --verbose` to inspect supported import candidates,
and read [adoption rules](safety.md#adoption) before importing. Other conflicts
need explicit reconciliation or a reviewed backup and relocation of the
existing target. Re-run the plan after resolving the cause.

`aikito doctor` shows the same adoption finding with its resource, source,
reason, and suggested commands. Fix the source when the resource should be
imported. If one resource is intentionally excluded, rerun adoption with
the displayed `--skip` command; skips are explicit, one-shot, and repeatable.
Malformed or unreadable source files must be repaired and cannot be skipped.

## A copied or generated resource has drifted

```bash
aikito diff
aikito diff project <project>
aikito diff --all
```

Review runtime changes and bring improvements you want to keep back into the
canonical source. Synchronize the affected scope afterward. Use `--force` only
when you have reviewed and chosen to discard runtime differences.

## An agent is missing or outdated

`aikito doctor` reports installed supported agents missing from the registry, as
well as agent versions that fall below minimum compatibility thresholds (such as
Claude Code requiring version >= 2.1.277 for native `AGENTS.md` support).

If your workspace's `agents/claude-code.toml` still uses the legacy `.claude/CLAUDE.md`
default, `aikito doctor --fix` updates it to `AGENTS.md`.

Review findings before using `aikito doctor --fix`, which can add bundled
defaults without replacing existing custom values. Agents not installed on this host
remain registered as offline for multi-machine use.

## Git exclude and overlay diagnostics

When Aikito creates overlay instructions (`.agents/AGENTS.md`), project memory
(`.agents/memory`), or link-mode skills (`.agents/skills/<name>`), it manages entries
in `.git/info/exclude` to keep `git status` clean.

- `aikito doctor` checks that all required entries are present in `.git/info/exclude`.
  Missing entries can be repaired automatically with `aikito doctor --fix`.
- `aikito doctor` warns if Git reports a typechange (`T`) at any managed target,
  indicating that a Git-tracked regular file was converted into a symlink.
- `aikito doctor` inspects personal instructions and warns if any paragraph is a
  verbatim duplicate of the repository's team `AGENTS.md`, helping keep personal
  instructions lean.

## A project is offline

A project with no locally existing candidate path is offline on this machine.
This is expected for another machine's checkout. If you expected it to be active,
inspect its paths with `aikito show project example` and correct the configuration.
See [multiple project paths](project-configuration.md#path-resolution-and-offline-semantics).

After repairs, repeat the relevant `show` command and `aikito status`.
For recovery and ownership details, consult the [Safety model](safety.md).
