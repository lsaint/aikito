# Projects

A workspace normally exists once. A project registration represents one code
directory and its project-specific instructions, memory, and skill selection.
When the requested resource is project-specific and the current directory is
unregistered, ask whether to register it. Never register a project merely to
store a global resource.

`aikito init project [<name> <path>]` creates the canonical project skeleton and
synchronizes its `.agents/` runtime. An existing unmanaged runtime resource, or
a project name already bound to another path, is a conflict for the user.

Projects may declare candidate paths for different machines. Paths absent from
the current host are reported as `offline on this host` and skipped without
removing them from the workspace.

## Ownership and Drift

Project instructions and memory remain linked to canonical sources. Project
skills use the configured `sync_mode`:

- `link` keeps symbolic links to canonical skills;
- `copy` creates managed copies suitable for project Git tracking.

Register canonical or external skills for projects with
`aikito add skill <name> --project <name>` (supports `--from <path>`,
`--from <path> --force` for snapshot refresh, and `--sync`).

`aikito status` detects drift in copied skills and `aikito diff` displays it.
Synchronization must not replace a drifted copy unless the changes that should
survive have been merged and the user intentionally authorizes `--force`.

Project `.agents/skills/` has entry-level ownership: preserve project-owned
skills that are not selected in the canonical project configuration. A selected
skill with the same name is a conflict. Matching contents do not prove Aikito
ownership. `.agents/memory/` is exclusively Aikito-managed, so unknown entries
there are conflicts.

## Public Python API

`Project.prepare()` is the execution-facing entry for external runners. It
requires one active project path or an explicit `path` override, applies the
same persistent project synchronization rules, and returns the resolved working
directory. V1 supports Pi and does not synchronize global resources, MCP
servers, or subagents during preparation.

An explicit path may point to an existing unregistered directory for an
ephemeral CI or deployment checkout. The caller remains responsible for
selecting the correct path. Consult the source checkout's `docs/python-api.md`
for the current API surface and examples.

## Automatic project application

`agent.toml` accepts `sync_trigger = "auto"` (default) or `"manual"`.
The remote watcher applies workspace changes to approved local checkouts, including
local workspace edits. Copy skills may reflect edits after the debounce interval;
links expose source changes directly. Global runtime configuration is excluded.
Use `sync_trigger = "manual"` to disable automatic application for a project on all
devices. Remote edits can re-enable `auto` for already approved checkouts, but
cannot approve new paths. Invalid values block the project.

Run `aikito sync project <name>` or `aikito sync` successfully once on each machine
to approve its existing checkouts. Manual sync trusts configured paths; automatic
application additionally requires a host-local approval with matching directory
identity. Older skill state is insufficient because it contains only path identity.
Replacing a directory invalidates approval; symlink/reparse-point checkout aliases
are not automatically approved. No automatic cloning or directory creation occurs.
Dry runs create neither approvals nor runtime files.

`aikito remote sync --apply` also applies approved projects. Exit codes are `1` for
remote failure, `2` for remote success with project application blocked or failed,
and `0` otherwise. Unbound, unapproved, offline and manual projects produce informational
results. `--dry-run` previews project actions against the current workspace; it
does not simulate incoming remote resources in project plans.

`status` and `show project` expose pending Copy updates, trigger and approval
state. `UNBOUND` means no checkout is registered; `UNAPPROVED` is an informational
migration notice and does not change runtime health. Run `aikito sync` once to
approve all configured local checkouts. `doctor` inspects approval records;
`doctor --fix` removes proven obsolete approvals, retaining offline and unverifiable
records and deferring cleanup while a transaction journal is present.
Worker status stores `project_apply` separately from remote health. Project
errors never roll back successful workspace synchronization. Each checkout is
preflighted as a unit, while skills, instructions and memory retain their separate
transaction boundaries; a later phase failure may leave earlier phases applied.
Subsequent rounds rebuild plans from actual state. No automatic force is used.
Deselection retains existing ownership rules: managed links are removed; Copy
directories are preserved with inactive state rather than automatically deleted.

Startup performs local reconciliation even when remote access fails. Workspace
changes trigger application independently of catalog publication, including global
memory referenced by projects. Unchanged result sets back off to five minutes,
including manual, unapproved, offline and blocked results; changed results retry
after thirty seconds. Checkout-only edits are detected within the backoff interval.
Metadata probes share the thirty-second fallback cadence rather than every remote
poll; workspace events and downloaded remote resources bypass that probe gate.
Logs change only when results do.
macOS background services may need permission for protected directories; use
manual sync or grant filesystem access when worker status reports permission errors.
