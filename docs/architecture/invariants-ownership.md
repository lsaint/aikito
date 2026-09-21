# Ownership and Binding Invariants

Ownership evidence rules govern how Aikito determines whether a filesystem entry is managed. Binding identity rules define workspace-global association semantics.

## Ownership Evidence
### INV-OWN-01: Dimensional Independence `[current]` {: #inv-own-01 }

Existence, ownership, desired content matching, and selection state are orthogonal dimensions:

- An entry may be `OWNED` without being eligible for deletion (e.g. deselected copy skill).
- An entry may match desired content ($R = C$) without establishing ownership (e.g. unmanaged pre-existing directory).
- An operation returning `NOOP` does not imply ownership was established or recorded.

### INV-OWN-02: Evidence Evaluation Order `[current]` {: #inv-own-02 }

Evidence must be evaluated in strict priority order. Path traversal failures, escaping symlinks, or conflicting evidence cannot be superseded by matching content:

1. **Target Directory Entry Verification**: Verify workspace, project binding, resource identity, target path, and directory entry type using `lstat` and `readlink` semantics. Broken symlinks must be identified as symlink entries *before* resolving target paths. Deletion must never follow the target of a symlink.
2. **Symlink Target Inspection**: For symlinks, evaluate the literal link target. A symlink constitutes ownership evidence if and only if it resolves into the canonical path of this resource in the active workspace. A symlink pointing to arbitrary workspace locations or external paths is unmanaged (`FOREIGN`/`UNKNOWN`). Deletion acts strictly on the link entry, never on referenced content.
3. **Active Copy Management Record**: For directory copies, inspect valid active management records. A valid record requires:
   - Matching workspace identity and project binding;
   - Matching physical checkout path and resource identity;
   - Matching normalized target entry path and representation type (`copy`);
   - Baseline fingerprint $B$ recorded at last successful sync.
   Records from other checkouts, other resources, or other tools cannot be reused.
4. **Fingerprint Comparison**: Compare current runtime fingerprint $R$ against baseline $B$ to detect local drift. Compare runtime fingerprint $R$ against canonical fingerprint $C$ to detect upstream updates. Baseline origin must distinguish recorded write success from explicit state reconciliation.
5. **Rejection of Pseudo-Ownership**: Identical directory contents, matching directory names, or inclusion in project `skills` configuration never establish ownership on their own.

### INV-OWN-03: Symlink Ownership Semantics `[current]` {: #inv-own-03 }

- **Target Model (`[current]`)**: A symlink entry is owned if and only if its literal target resolves to the canonical path of *this specific resource* (`canonical_root / resource_name`). A symlink pointing to an arbitrary workspace location or a different workspace skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`). Deletion acts strictly on the link entry, never on referenced content.
- **Intentional Tightening**: Previously, `_symlink_points_within()` evaluated root-level parent directories, allowing any symlink resolving inside `workspace/skills` (even pointing to another skill) to be unlinked upon deselection. This has been corrected to exact canonical targets (`canonical_root / resource_name`). Deselected links pointing to other workspace resources are preserved rather than removed, and reported as unmanaged/conflicts.
- **Broken Links**: A broken symlink is owned if and only if its literal target points specifically to the canonical path of *this specific resource* (`canonical_root / resource_name`), retaining proof of prior Aikito management and rendering it eligible for cleanup upon deselection. A broken symlink pointing to an arbitrary workspace location or another skill is an unmanaged error/mismatch (`FOREIGN`/`UNKNOWN`).
- **Inspection Safety**: Canonical roots and intermediate parent directories must be real directories, not symlinks, junctions, or reparse points escaping the trusted workspace boundary. Symlinks inside copy sources or targets are rejected.

### INV-OWN-04: Copy Lifecycle and State Record Invariants `[current]` {: #inv-own-04 }

Copy management records follow an explicit two-state lifecycle (`active` vs. `inactive`):

- **`active`**: The copy was successfully synchronized by Aikito and is currently selected. Eligible for automated updates when $R = B$. Baseline fingerprint $B$ reflects the last successful write or verified convergence, recording an origin of `write`, `reconcile`, `claim`, or `reactivate`.
- **`inactive`**: The copy was previously synchronized but is currently deselected. Historical baseline $B$ is retained solely for origin auditing and does not grant automated update or deletion rights.
- **Link Separation**: Symlink ownership is derived solely from live directory entry and symlink target facts; symlinks do not create or maintain active copy management records.
- **Deselection Protection**: Deselected copy skills are **never deleted or unlinked** by Aikito. Deselection marks the management record `inactive` and leaves target files completely untouched.
- **Corrupt / Missing Record**: Any corrupt, mismatched, or unreadable management record reverts to `UNKNOWN`. Aikito halts destructive actions and refuses automated updates; `--force` cannot bypass record corruption.

### INV-OWN-05: Directory Fingerprint Specification `[current]` {: #inv-own-05 }

Directory fingerprints must capture the complete directory tree: file relative paths, file types (regular file, symlink, directory), byte contents, empty directories, and POSIX executable permission bits. File timestamps (`mtime`) and filesystem inode numbers are excluded from content equality comparisons, but may be used during preflight to detect concurrent entry replacement.

Any unsupported filesystem entry (FIFO, socket, device node) or internal symlink inside a copy source or target causes fingerprinting/inspection to fail and blocks synchronization.

### INV-BIND-01: Global Binding Identity Definition `[current]` {: #inv-bind-01 }

Global resources lack project checkouts and reside in host-level paths (e.g. `~/.agents/`, `~/.claude/`). Their binding identity tuple is defined as:

```text
(workspace_identity, resource_identity, normalized_target_path)
```

For consumer links, the binding also associates the set of consumer agent platforms. This replaces project-scoped `(workspace, project, physical_checkout)` keys without introducing duplicate metadata stores.

### INV-BIND-02: Multi-Workspace Global Ownership and Conflict Resolution `[current]` {: #inv-bind-02 }

When multiple independent Aikito workspaces configure the same host agent target (e.g. `~/.agents/skills/`), ownership cannot be assumed by the active workspace unless the target link explicitly resolves to canonical resources within the active workspace. Targets pointing to other workspaces or external locations evaluate to `UNKNOWN` / `FOREIGN`. Aikito preserves these entries, emits diagnostic findings, and refuses automatic takeover or deletion.

### INV-BIND-03: Scope Reduction for Link-Only Resources `[current]` {: #inv-bind-03 }

Global skills and instructions operate strictly in `link` mode (`mode="link"`); copy mode is intentionally not supported. Therefore, global resources require NO content baseline records ($B$), NO directory fingerprint tracking in state documents, and NO copy reconciliation lifecycle. Their ownership and state transitions depend exclusively on live filesystem directory entries and symlink destination verification ([INV-OWN-02](#inv-own-02), [INV-OWN-03](#inv-own-03)).

