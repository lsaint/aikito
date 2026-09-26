# Safety Model

Aikito modifies configuration consumed by other tools and stores durable
knowledge in Git. Conservative write behavior reduces risk, but it does not
remove the user's responsibility to review sensitive data and planned changes.

## Git and Memory Privacy

`aikito init workspace` creates a local Git repository. It does not configure a
remote, make the repository private, or certify the contents as safe to
publish.

Before adding a remote or pushing, inspect memory and configuration for:

- API keys, tokens, passwords, or credentials;
- customer data and private conversations;
- internal addresses, infrastructure details, or private source code;
- sensitive raw debug output.

Do not use persistent memory as a transcript archive or secret store. Once a
secret is committed, deleting it in a later commit does not remove it from Git
history.

## Initialization Write Boundaries

`aikito init workspace` refuses to write into the CLI source tree, another
directory that looks like an Aikito source checkout, or an unrecognized
non-empty directory. Keep the CLI checkout and user workspace separate:

```text
~/aikito-src   CLI source checkout
~/aikito       user workspace
```

This guard applies before workspace files are written. When pointing to an
existing recognized workspace on a new machine, `aikito init workspace` only
registers the local pointer without modifying files or Agent runtimes.
`--force` can refresh templates in a recognized Aikito workspace, but it does
not bypass directory safety checks.

`aikito init project` refuses to replace unmanaged agent-native instruction, skill,
or memory resources. It also refuses to bind an existing project name to a
different code directory.

## Doctor pruning

In multi-host SoT setups, undetected Agents on a specific machine are offline and
must not be removed from `agents/<name>.toml` so other hosts can continue using them.
The `--prune` flag has been removed from `aikito doctor`. Offline
agents are preserved safely across all machines.

## Adoption

`aikito adopt` builds and validates the complete import plan before writing. It
refuses the entire plan while instruction conflicts or invalid generated
resources remain; unreadable or malformed source configuration also blocks the
plan. Use `aikito adopt --dry-run` for a concise read-only plan, or
add `--verbose` to inspect every source and target.

Each blocking finding names the affected resource, source, reason, and an exact
next command. `aikito doctor` reports the same findings in its Adoption section
without importing anything; `doctor --fix` does not apply or skip adoption.
If a detected resource is intentionally out of scope, use a repeatable,
one-shot `--skip instructions`, `--skip mcp/<name>`, or
`--skip subagent/<name>`. Every skip is visible in the plan, and an unknown
target fails. There is no global skip-errors mode. Unreadable or malformed
source files remain unskippable because Aikito cannot safely determine their
contents.

An applied adoption creates timestamped backups under:

```text
~/.aikito/backups/adopt_<timestamp>
```

Adoption imports resources into the Aikito workspace. It does not overwrite the
original Agent configuration files; Agent-native changes occur only during an
explicit synchronization command. After applying adoption, run `aikito sync`.
It checks the complete workspace plan before writing and stops if any scope has
a conflict. Use `aikito sync --dry-run --verbose` when a detailed read-only
review is useful.

## Workspace Import

`aikito import workspace <source> --dry-run` reads the source and target without
writing. The import never changes the source and does not delete content that
exists only in the target. Any conflict or blocking finding stops the entire
import before changes are applied. An interrupted import can be recovered on
the next run through its transaction journal.

Possible plaintext credentials in imported files produce path-only warnings;
they do not block the import. Review those files before committing or sharing
the workspace. Use `--verbose` to inspect unchanged, included, and skipped
source items as well as planned changes.

## Conflict and Drift Protection

The response depends on the resource and command. A matching file alone does
not prove that Aikito owns it.

| Situation | Default response | Reviewed next step |
| --- | --- | --- |
| Copied project skill changed locally | Block project sync and preserve the copy | Use `aikito diff`; keep the local edit in the canonical skill or explicitly run `aikito sync project <name> --force` to replace the copy. |
| Unmanaged project skill directory matches the canonical skill | Leave files unchanged and report that ownership is unproven | `aikito sync project <name> --force` can explicitly claim the matching copy. |
| Unmanaged global skill directory or foreign skill symlink | Report a conflict and preserve the target | Inspect and resolve the target manually; global sync has no `--force` option. |
| Existing instruction or memory target is unmanaged | Report a conflict and preserve the target | Inspect both sources and resolve manually; `--force` cannot overwrite instructions or memory. |
| Unmanaged subagent definition occupies a selected target | Report a conflict and preserve the definition | Review it; `aikito sync subagents --force <agent>/<subagent>` authorizes only that target. |
| Managed MCP server was edited outside Aikito | Block MCP sync and preserve the configuration | Review the server; `aikito sync mcp --force` can replace the conflicting managed server node. |
| Unmanaged MCP server or unrelated configuration | Preserve it; a same-name collision blocks sync | Resolve the collision explicitly. Other servers and non-MCP settings remain untouched. |

Start with `aikito sync --dry-run --verbose` to inspect the complete workspace
plan. After resolving a conflict, preview again before syncing. The
workspace-wide `aikito sync` command has no `--force`; authorization is scoped
to the affected resource command. See the
[command-level `--force` rules](architecture/invariants-execution.md#inv-auth-06).

Owned project and global skill links are removed on deselection only when they
point to the exact canonical resource. Deselected copied project skills remain
in the checkout. Unmanaged links and files are preserved. See the
[ownership](architecture/invariants-ownership.md),
[skill](architecture/invariants-skills.md),
[instruction](architecture/invariants-instructions.md), and
[memory](architecture/invariants-memory.md) invariants for exact transitions.
Subagent `--prune` removes only orphan definitions with Aikito ownership
markers; see the [subagent invariants](architecture/invariants-subagents.md).

MCP changes are planned per server node and merged into each physical config
file, preserving unrelated settings. Write failures trigger rollback; if
recovery cannot complete, Aikito retains backups and reports manual recovery
steps. See the [MCP invariants](architecture/invariants-mcp.md).

## Credentials

Canonical MCP configuration should contain environment-variable references,
not plaintext credentials. Adoption converts recognized secrets to references,
but users must still inspect imported configuration before committing it.

- Sensitive credential tokens, authorization headers, and environment variables are redacted in all plan summaries, CLI outputs, error messages, and public structured views ([INV-MCP-05](architecture/invariants-mcp.md#inv-mcp-05)).
- Configuration files containing sensitive credentials or marked as sensitive suppress standard whole-file backups to prevent plaintext secrets leaking into backup directories, and enforce secure filesystem permissions (`0600` on POSIX, restricted ACLs on Windows) ([INV-MCP-06](architecture/invariants-mcp.md#inv-mcp-06)).

## Platform Support and Constraints

Aikito provides native support across macOS, Linux, and Windows (PowerShell and
Command Prompt):

- **Symbolic Links**: On POSIX systems and Windows, Aikito uses symbolic links to
  connect runtime Agent configurations with canonical resources. On Windows,
  creating unprivileged symbolic links requires enabling Windows Developer Mode
  (or running with Administrator privileges). When Developer Mode is disabled,
  Aikito cleanly refuses synchronization upfront and displays actionable steps to
  enable it.
- **Credential File Permissions**: On POSIX systems, credential-bearing configuration
  files are restricted to owner read/write (`0600`). On Windows, Aikito hardens NTFS
  Access Control Lists (`icacls`) by disabling inheritance and granting read/write
  access strictly to the active user account, stripping broad group permissions.



## Managed Project Directories

Project `.agents/skills/` is shared at entry level. Aikito manages only selected
skill names, preserves other project-owned entries, and reports a conflict only
when a selected name is already owned by the project. Project `.agents/memory/`
manages selected memory references and project `notes/`. Pre-existing unmanaged entries
evaluate to conflicts and are strictly preserved ([INV-MEM-07](architecture/invariants-memory.md#inv-mem-07)).
Matching file contents alone never prove ownership ([INV-OWN-01](architecture/invariants-ownership.md#inv-own-01)),
and synchronization never deletes unknown content.

## Recovery Practice

Before applying changes to an established setup:

1. Run the available preview or dry-run command.
2. Review every conflict and target path.
3. Confirm the adoption backup location when applicable.
4. Apply one resource class at a time.
5. Run the matching status command immediately afterward.
