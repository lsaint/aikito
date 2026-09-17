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
