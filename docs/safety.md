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
must not be removed from `agents.toml` so other hosts can continue using them.
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

## Conflict and Drift Protection

- Unmanaged targets are reported as conflicts rather than silently replaced.
- Deselected project skills are removed only when a workspace symlink proves they were
  managed by Aikito ([INV-OWN-03](architecture/invariants.md#inv-own-03),
  [INV-TR-14](architecture/invariants.md#3-state-transition-table)). Deselected copy skills
  and unmanaged entries in project checkouts are preserved as project-owned ([INV-TR-17](architecture/invariants.md#3-state-transition-table)).
  *(Note: Global synchronization maintains a legacy compatibility heuristic where deselected
  runtime skills identical to canonical are cleaned up via `allow_matching_copies=True`;
  see [INV-OWN-03](architecture/invariants.md#inv-own-03).)*
- Managed-entry fingerprints expose local drift.
- Copied project skill drift is shown by `aikito diff` and blocks project sync
  unless the user supplies `--force` after review ([INV-AUTH-01](architecture/invariants.md#inv-auth-01)).
- Conflicting instruction sources require user judgment.
- Explicit force or prune options are strictly scoped to reviewed targets ([INV-AUTH-02](architecture/invariants.md#inv-auth-02)).
- For formal verification rules and state transition tables, see the
  [Engineering Invariants](architecture/invariants.md).

## Credentials

Canonical MCP configuration should contain environment-variable references,
not plaintext credentials. Adoption converts recognized secrets to references,
but users must still inspect imported configuration before committing it.

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
when a selected name is already owned by the project. `.agents/memory/` remains
exclusively managed by Aikito. Matching file contents alone never prove copy
ownership ([INV-OWN-01](architecture/invariants.md#inv-own-01)), and synchronization never deletes unknown content.

## Recovery Practice

Before applying changes to an established setup:

1. Run the available preview or dry-run command.
2. Review every conflict and target path.
3. Confirm the adoption backup location when applicable.
4. Apply one resource class at a time.
5. Run the matching status command immediately afterward.
