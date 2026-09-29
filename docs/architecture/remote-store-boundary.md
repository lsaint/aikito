# Remote Store Boundary

This internal design fixes the storage contract before backend migration.
Portable payloads are implemented internally; there is no public CLI or service.
The current engine still
uses `FilesystemRemote`; the acceptance driver is test orchestration, not a
production `RemoteStore` implementation.

## Contract

The future interface is `read() -> RemoteSnapshot`,
`fetch(expected, ids) -> Mapping[str, ResourcePayload]`,
`commit(expected, mutations) -> RemoteSnapshot`, and `recover() -> bool`.
The first implementation retains no historical content: a changed center
identity or generation makes fetch fail with `SnapshotExpired`, including an
empty fetch. Clients discard staging and replan. Operations either return a
result from one coherent snapshot or fail; a newer payload cannot be labeled
as content from an older snapshot.

| Value | Required semantics |
| --- | --- |
| Snapshot | Opaque `sync_id`, nonnegative generation, resource descriptors indexed by opaque ID; no physical `ResourcePart` or paths. Descriptors carry a logical fingerprint, transport hash, and an optional opaque content version. |
| Payload | Versioned, deterministic, portable content with no disk paths, handles, or shared directory requirements. The backend verifies the transport hash without interpreting the content. |
| Mutation | Opaque resource ID, expected before descriptor or absence, and replacement descriptor plus payload, or an explicit delete with no payload. An empty fingerprint denotes presence and differs from absence. Duplicate IDs are invalid. |
| Ownership | Snapshots, descriptors, mutations, and returned payloads are immutable or defensively copied at each boundary, including nested typed values. Caller mutation cannot affect stored or previously read state. |
| Commit | Check expected identity and generation even for an empty batch; check every before descriptor, hash, and mutation before publishing the complete batch atomically. Rejection leaves content and generation unchanged. |
| Generation | A nonempty accepted mutation batch advances generation exactly once; an empty accepted batch leaves it unchanged. Clients omit semantic NOOP mutations. |
| Fetch | All requested IDs must be present and have intact content. No partial success or implicit missing values; duplicates in the requested collection are treated as a set. |
| Recovery | Read and fetch never recover or create state. Pending transactions raise `RecoveryRequired`; explicit backend recovery owns locking and journals. Successful recovery requires replanning. A backend without recovery work returns false. |

Errors distinguish `SnapshotExpired` (definite conditional rejection),
`InvalidContent` (bad mutation, missing or corrupt payload), `RecoveryRequired`,
and `StoreUnavailable`. A future network transport also needs
`CommitOutcomeUnknown`: an uncertain response is not proof that the center
rejected the batch, and clients must resolve the result before retrying.
Exception names describe the required future categories; current filesystem
errors remain internal `WorkspaceCoreError` / `WorkspaceReconcileError`.

Transport hashes are over the versioned canonical payload encoding. They do
not replace logical fingerprints. Encodings must distinguish bytes, dates,
times, datetimes, typed arrays/tables, and literal dotted TOML keys. Tree
encoding sorts normalized relative paths, preserves explicit empty directories,
and carries executable flags. Absolute paths, `..`, symlinks, special nodes,
duplicate paths, and unsafe target-platform collisions are rejected by clients
before materialization. Existing scanner exclusions remain in force.

Existing non-tree fingerprint rules remain unchanged: memory/inbox/instructions
use exact bytes (including line endings), Agent/MCP/subagent resources use their
existing parsed semantics and references, and shared TOML fields use typed
values independently of source formatting. Set members/project providers use
presence with an empty fingerprint, not a hash of the shared file. Payload
encoding must preserve each distinction, retain actual TOML key components,
and reproduce these fingerprints after decoding on another platform.

## Responsibility boundary

| Store contract | Client / reconciliation engine | Optional plaintext backend defense |
| --- | --- | --- |
| Identity/generation compare-and-swap, complete batch publication, transport hashes, snapshot-bound fetch, immutable boundary values | Logical ID decoding, semantic fingerprints, references, credentials, selected resource policy, target path safety, post-write verification | Decode logical IDs for physical layout, parse plaintext, recompute fingerprints, reject invalid references or credentials |

Resource IDs, `sync_id`, `replica_id`, and logical fingerprints are opaque
strings to the store; it must not validate their logical meaning or require a
particular naming syntax. A backend can require consistency with previously
stored identity but not a client-specific ID format. Replica identity is local
client state and need not appear in store requests. A filesystem backend keeps
its own path mapping, writer lock, manifest, and journal. It must also enforce
workspace/center non-overlap at its attachment boundary.

Client credential, reference, and semantic checks run while planning and again
on downloaded content before any local write. Backend-specific defenses cannot
substitute for those client checks or become requirements for other backends.
The protocol therefore permits opaque encrypted content in a later transport.

## Application sequence

1. Read the center, scan the local workspace, and plan the safe subset.
2. With no center lock exposed to the engine, fetch all planned downloads
   against that snapshot. Validate and stage them locally. Verify selected
   upload content still matches the plan; preserve shared TOML field isolation.
3. Conditionally commit uploads/deletes to the center. A stale fetch or rejected
   commit discards staging and leaves local resources and Base untouched.
4. Commit local resources and Base together under the local writer lock. If
   this fails after the center accepted the batch, preserve the old Base and
   use explicit local recovery followed by replanning.

These are two separate transactions, not a distributed atomic commit. Center
address changes do not change identity. Conflict/blocked resources retain Base,
safe independent resources advance, and local-only fields remain local.

## Skill fingerprint decision and compatibility

Stage zero chooses **content-only** skill fingerprints on all platforms. A tree
hash is SHA-256 of sorted newline-separated records `f <path> <byte hash>` and
`d <path>` for explicit empty directories. POSIX execute bits are no longer
included. A permission-only edit is not a resource edit and cannot trigger
synchronization. Native copies still preserve permissions where supported.
Portable payloads carry executable flags separately, outside the logical
fingerprint; round-trip tests verify them independently of semantic equality.

New center and replica state records include `skill_fingerprint: "content-v1"`.
State version remains 2 because the resource/value schema is unchanged. A
historical record without the marker and with skill resources is refused,
even if those particular skills have no scripts: its old Base cannot establish
which permission view originally produced the hash. No Base is reset and no
manifest is rewritten. Preserve both, then explicitly create a new center and
pair fresh replicas after reviewing resources; copying old Base into a new
pair is not a migration. Historical records with no skills remain readable and
gain the marker on the next state write. Unknown schemes are always refused.

Reconciliation is internal and has no public CLI, so no automatic user-state
migration is introduced. `workspace_reconcile_boundary_smoke.py` simulates both
permission views on each OS, exercises upload/download/reverse edits and NOOP,
and verifies legacy refusal is read-only. It does not merely compare separate
native CI runs.

## Portable payload implementation

`workspace.payload` defines immutable `FilePayload`, `TomlPayload`,
`TreePayload` / `TreeEntry`, and `MemberPayload`, plus `ResourceDescriptor`
and `ResourceMutation`. None contains physical resource parts or paths.
The client validates semantic fingerprints/references in memory, separately
from the canonical version-1 JSON transport hash. Encoded bytes use base64;
typed fields use canonical TOML text plus actual key components. Nested typed
values are defensively reconstructed rather than shared through mutable objects.
Deletion carries a before descriptor and no replacement/payload; the empty
fingerprint of a present member differs from absence.

`workspace.payload_io.capture_resources` accepts only the authorized IDs,
rejects changed source content and credentials, and returns a read-only mapping.
`capture_mutations` also checks each planned before/after fingerprint. Shared
field payloads never contain a copy of their source TOML file or neighboring
blocked fields. Credential checks for bytes, typed fields, and trees perform no
temporary writes. Existing Import keeps its path-based adapter and formatting.

`materialize_resources` validates the full downloaded batch, including client
credential and semantic checks, before writing to an owned empty staging
directory. `prepare_payload_writes` then calls the existing shared writer in
sync mode, composing each shared TOML target once. The caller still checks
transport hashes at decode, owns the local writer lock, and performs the
resource/Base transaction and post-write validation. Stage one does not yet
route the reconciliation engine through the new payloads; that is backend
migration work.

Tree paths reject traversal, absolute/drive paths, separators unsafe on Windows,
reserved Windows names, symlinks/special nodes, duplicates, and case/Unicode
collisions, including directory-prefix aliases. Empty-directory nodes cannot
have descendants. Scanner exclusions apply before capture. The additional
reserved artifact `.aikito-executable.json` stores executable paths on Windows;
it is excluded from logical fingerprints and portable tree entries. The local
transaction copies only its validated canonical metadata, rejecting extra
fields and unsafe nodes. Windows recapture reads these logical flags; POSIX
materialization applies them as native modes and writes no metadata artifact.
Missing paths left after a local deletion are ignored during recapture, so a
stale metadata entry cannot recreate a deleted file. Permissions alone still do
not trigger reconciliation.

`workspace_payload_smoke.py` exchanges all admitted resource kinds through
encoded payloads and writes a distinct local workspace, retaining its inbox
path and excluding a credential-blocked field. It runs as a real script in all
three OS workflows. `test_workspace_payload.py` covers typed/time values,
mutation presence/deletion, corruption, client checks, immutable copies,
unsafe paths/nodes, and Windows-to-POSIX flag preservation through local copies.

## Acceptance classification

| Suite or scenario | Classification |
| --- | --- |
| `exercise_behavior(base, backend)` in `workspace_reconcile_acceptance.py` | Shared engine behavior: all admitted resources, convergence, conflict Base, safe subset, stale local/center plans, host-local inbox paths, local journal recovery, deletion, replica relocation, repeat NOOP. The injected driver exposes no center directory. |
| `exercise(base)` wrapper and `FilesystemBackend` | Filesystem orchestration and center relocation; storage checkpoints inspect files only within the driver. |
| `test_workspace_reconcile_boundary.py` | Portable skill behavior and schema compatibility. The injected center-change test pins refusal between read and download staging with no local/Base write. |
| Existing conditional batch / competing-writer / empty-batch tests in `test_workspace_reconcile.py` | Store contract behavior, currently exercised through the filesystem API; migrate to the shared contract suite with portable mutations. |
| Credential / reference / shared TOML / conflict / local safety tests in `test_workspace_reconcile*.py` | Client behavior; run against both backends after migration. Assertions that a direct center commit parses/rejects plaintext belong to filesystem defense instead. |
| Center manifest parsing, `resource_for_id`, layout checks, `verify_contents`, center journal recovery and root overlap tests | Filesystem-specific defense and lifecycle. Local journal recovery remains shared engine behavior. |

When portable payloads and `RemoteStore` land, shared contract tests must cover
opaque IDs, caller mutation isolation, invalid entire batches, missing/corrupt
fetch content, read/fetch/commit purity, stale empty requests, and center
identity mismatch. In particular: read S, accept a different batch, fetch
against S must raise `SnapshotExpired`; resources, generation, and local Base
must be unchanged by the failed fetch. Stage zero's existing API test guarantees
the weaker current behavior (conditional commit refuses after staging);
snapshot-bound **fetch itself** is an implementation gate for backend migration,
not a claim about the current path-based `content()` method.

No in-memory backend is implemented here. The same behavior scenarios will be
injected with that backend after engine migration; no backend branches belong
inside shared acceptance assertions.
