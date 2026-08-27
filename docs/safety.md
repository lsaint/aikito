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

This guard applies before workspace files are written. `--force` can refresh
templates in a recognized Aikito workspace, but it does not bypass these
directory safety checks.

`aikito init project` refuses to replace unmanaged agent-native instruction, skill,
or memory resources. It also refuses to bind an existing project name to a
different code directory.

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

## Platform Constraints

Native Windows is not supported because the synchronization and credential
safety model relies on symbolic links and POSIX file permissions. Windows users
should use WSL2.

## Managed Project Directories

The target project's `.agents/skills/` and `.agents/memory/` directories are
owned exclusively by Aikito. Project synchronization rejects entries that are
not selected by the project configuration without deleting them. Never place
unrelated files there.

## Recovery Practice

Before applying changes to an established setup:

1. Run the available preview or dry-run command.
2. Review every conflict and target path.
3. Confirm the adoption backup location when applicable.
4. Apply one resource class at a time.
5. Run the matching status command immediately afterward.
