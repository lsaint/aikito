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

`aikito adopt` is a read-only preview unless `--apply` is supplied. Review all
detected resources and resolve instruction conflicts before applying a plan.

`aikito adopt --apply` creates timestamped backups under:

```text
~/.aikito/backups/adopt_<timestamp>
```

Adoption imports resources into the Aikito workspace. It does not overwrite the
original Agent configuration files; Agent-native changes occur only during an
explicit synchronization command.

## Conflict and Drift Protection

- Unmanaged targets are reported as conflicts rather than silently replaced.
- Deselected skills are removed only when a workspace symlink or unchanged
  canonical copy proves they were managed by Aikito.
- Managed-entry fingerprints expose local drift.
- Copied project skill drift is shown by `aikito diff` and blocks project sync
  unless the user supplies `--force` after review.
- Conflicting instruction sources require user judgment.
- Explicit force or prune options should be scoped to a reviewed target.

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
ownership, and synchronization never deletes unknown content.

## Recovery Practice

Before applying changes to an established setup:

1. Run the available preview or dry-run command.
2. Review every conflict and target path.
3. Confirm the adoption backup location when applicable.
4. Apply one resource class at a time.
5. Run the matching status command immediately afterward.
