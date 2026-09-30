# Remote Store Boundary

The internal reconciliation engine currently uses the temporary
`workspace.remote_store.LegacyRemoteStore` protocol and portable payloads.
`FilesystemRemote` implements that interface;
there is no public reconciliation CLI or service. The acceptance driver remains
test orchestration, separate from the production store contract.

## Commit identity contract

`RemoteStore` defines `commit(request: CommitRequest) -> CommitResult` and
`resolve_commit(sync_id, client_id, request_id, mutation_digest) -> CommitResult | None`,
alongside the existing read, fetch, attachment and recovery operations. This is
the contract for the staged network safety work. Backends and reconciliation
still use `LegacyRemoteStore`; they do not yet persist receipts or recover
unknown outcomes. The legacy interface will be removed after migration.

`CommitRequest` carries an opaque client ID, stable request ID, optional
`ReceiptCursor`, complete expected snapshot, nonempty mutation tuple and
mutation digest. Mutations are sorted by opaque ID; duplicate IDs and empty
batches are rejected. Snapshots and typed payloads remain immutable. IDs and
fingerprints are not parsed for semantic meaning.

`workspace.remote_wire` supplies `build_commit_request`, canonical request
encoding/decoding, digest validation, accepted snapshot prediction and receipt
construction/validation. Its internal version-1 encoding supports future
pending persistence; it is not a frozen HTTP wire format. Request digests
cover the encoding version and domain, client ID, previous receipt, complete
expected snapshot, before/after descriptors and the exact existing canonical
payload bytes. Request ID and the claimed digest are excluded. Descriptor
encoding retains fingerprints, transport hashes, ordered references and the
current optional opaque `mode_fingerprint`. Absence is `null` and
differs from a present member with an empty fingerprint. Decoding rejects
unknown fields/versions, duplicate JSON fields/IDs, invalid payload/hash pairs,
noncanonical bytes and forged digests. Backend boundaries must independently
recompute digests, including before returning a cached receipt.
`ResourceMutation` does not interpret client-shaped IDs. The existing skill
executable-state check belongs to the plaintext filesystem backend; client
capture and materialization retain their semantic validation.

`CommitResult` binds the center, client, request ID, mutation digest, accepted
revision and result digest. The result digest covers a separate versioned domain,
these identity fields and the complete accepted descriptor map. A client can
derive that map from the persisted request without fetching historical payloads.
The receipt records the first accepted result, not the current remote state;
newer remote commits cannot invalidate a correctly bound historical receipt.

Each client retains only its latest accepted receipt. The new backend contract
requires one critical section: validate the center, return a matching latest
receipt before CAS, otherwise check revision CAS, the previous receipt cursor
and the batch, then publish resources/revision/receipt atomically. A matching
request ID with a different digest raises `RequestIdentityMismatch`. Original
requests whose receipts were replaced fail CAS rather than returning a historical
result; detection of arbitrary historical request ID reuse is not promised.
Clients must use fresh IDs for new logical commits.

The cursor identifies the last receipt completed by the client. A new batch may
replace its receipt only if the cursor matches. A missing receipt permits only
a `None` cursor. A mismatch raises `ReplicaHistoryMismatch` and blocks automatic
replanning or pairing. This detects divergent stale replica history; it cannot
detect a complete clone carrying the same unresolved request. Each client ID
must have one active owner. Center restoration must use a new sync ID rather
than rolling back revision or receipts within the same identity.

Resolution serializes with publication: a center mismatch raises
`StoreIdentityMismatch`; an unavailable or unrecovered store raises a store
error. Matching request ID with a different digest raises
`RequestIdentityMismatch`. A matching identity/digest returns its original
receipt. No receipt or a different latest request returns `None`, which says
nothing about future delayed delivery and never authorizes discarding pending.

`CommitOutcomeUnknown` means delivery may have committed. A lost or unverifiable
response requires retention of the exact request and its identity; it is not
a definite rejection. A backend rejecting a wrong center before publication
raises `StoreIdentityMismatch`, and pairing remains blocked. Receipt validation
helpers raise `InvalidContent` for invalid responses; callers must still retain
pending because validation failure does not prove rejection. This contract alone
does not add pending persistence, retries or network safety to the legacy engine.

## Legacy backend contract

The implemented data interface is `read() -> RemoteSnapshot`,
`fetch(expected, ids) -> Mapping[str, ResourcePayload]`,
`commit(expected, mutations) -> RemoteSnapshot`, and `recover() -> bool`.
The first implementation retains no historical content: a changed center
identity or revision makes fetch fail with `SnapshotExpired`, including an
empty fetch. Clients discard staging and replan. Operations either return a
result from one coherent snapshot or fail; a newer payload cannot be labeled
as content from an older snapshot.

| Value | Required semantics |
| --- | --- |
| Snapshot | Opaque `sync_id`, nonnegative revision, resource descriptors indexed by opaque ID; no physical `ResourcePart` or paths. Descriptors carry a logical fingerprint, transport hash, and an optional opaque content version. |
| Payload | Versioned, deterministic, portable content with no disk paths, handles, or shared directory requirements. The backend verifies the transport hash without interpreting the content. |
| Mutation | Opaque resource ID, expected before descriptor or absence, and replacement descriptor plus payload, or an explicit delete with no payload. An empty fingerprint denotes presence and differs from absence. Duplicate IDs are invalid. |
| Ownership | Snapshots, descriptors, mutations, and returned payloads are immutable or defensively copied at each boundary, including nested typed values. Caller mutation cannot affect stored or previously read state. |
| Commit | Check expected identity and revision even for an empty batch; check every before descriptor, hash, and mutation before publishing the complete batch atomically. Rejection leaves content and revision unchanged. |
| Revision | A nonempty accepted mutation batch advances revision exactly once; an empty accepted batch leaves it unchanged. Clients omit semantic NOOP mutations. |
| Fetch | All requested IDs must be present and have intact content. No partial success or implicit missing values; duplicates in the requested collection are treated as a set. |
| Recovery | Read and fetch never recover or create state. Pending transactions raise `RecoveryRequired`; explicit backend recovery owns locking and journals. Successful recovery requires replanning. A backend without recovery work returns false. |

Errors distinguish `SnapshotExpired` (definite conditional rejection),
`InvalidContent` (bad mutation, missing or corrupt payload), `RecoveryRequired`,
and `StoreUnavailable`. The new commit contract adds
`CommitOutcomeUnknown`: an uncertain response is not proof that the center
rejected the batch, and clients must resolve the result before retrying.
Filesystem errors map to these store categories. The reconciliation entrypoints
wrap store and client validation failures as `WorkspaceReconcileError`, retaining
the original cause. Local transaction I/O failures retain their existing behavior.

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

`revision` names the center's batch commit version within one `sync_id`, separate
from an individual descriptor's `content_version`. Replica revision records the
confirmed remote progress; its per-resource Base may retain older values for
conflicts and blocked resources and does not assert complete convergence.

Center and replica state require a nonnegative integer `revision`. Missing,
boolean, negative, or noninteger values are rejected. State versions and journal
recovery rules remain unchanged; there is no legacy field fallback or migration.
Skill copy state, plans, authorization summaries, and journals also use
`revision`, independently of the remote counter. Local skill copy state alone
accepts a legacy `generation` when `revision` is absent; reads leave the file
unchanged, and successful saves emit only `revision`.

| Store contract | Client / reconciliation engine | Optional plaintext backend defense |
| --- | --- | --- |
| Identity/revision compare-and-swap, complete batch publication, transport hashes, snapshot-bound fetch, immutable boundary values | Logical ID decoding, semantic fingerprints, references, credentials, selected resource policy, target path safety, post-write verification | Decode logical IDs for physical layout, parse plaintext, recompute fingerprints, reject invalid references or credentials |

Resource IDs, `sync_id`, `replica_id`, and logical fingerprints are opaque
strings to the store; it must not validate their logical meaning or require a
particular naming syntax. A backend can require consistency with previously
stored identity but not a client-specific ID format. The new commit contract
uses the replica ID as an opaque client ID for receipt retention; the legacy
backend interface does not transmit replica identity. A filesystem backend keeps
its own path mapping, writer lock, manifest, and journal. It must also enforce
workspace/center non-overlap at its attachment boundary. The local lifecycle
hook `validate_replica(local)` checks this without exposing a center path to the
engine; its local path is never a snapshot, payload, or transport request. A
backend with no local attachment constraint may implement this hook as a no-op.
`workspace.resource_state.local_resource_for_id` decodes client logical IDs for
local writing. Filesystem-specific `resource_for_id` and `verify_contents` remain
backend defenses; neither is imported by the engine.

Client credential, reference, and semantic checks run while planning and again
on downloaded content before any local write. Backend-specific defenses cannot
substitute for those client checks or become requirements for other backends.
The protocol therefore permits opaque encrypted content in a later transport.

## Application sequence

1. Read descriptors, scan the local workspace, and plan the safe subset. The
   preview fetches only candidate local downloads and remote config fields
   required to check a shared TOML merge. Conflict choices may add a second,
   incremental fetch. A NOOP preview makes no fetch request. Config field IDs
   do not distinguish literal dotted keys from nested keys, so a config change
   requires the paths of all remote config fields in that shared file.
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
`d <path>` for explicit empty directories. Execute bits stay outside that hash.
Skill resources carry a separate mode fingerprint over the sorted paths of
executable files; all other files are implicitly nonexecutable. A mode-only edit
syncs as a skill update. Portable payloads carry and validate those flags.

New center and replica state records include `skill_fingerprint: "content-v1"`.
State version remains 2 because the content fingerprint scheme is unchanged. A
historical record without the marker and with skill resources is refused,
even if those particular skills have no scripts: its old Base cannot establish
which permission view originally produced the hash. No Base is reset and no
manifest is rewritten. Preserve both, then explicitly create a new center and
pair fresh replicas after reviewing resources; copying old Base into a new
pair is not a migration. Historical records with no skills remain readable and
gain the marker on the next state write. Unknown schemes are always refused.
An existing `content-v1` skill Base without a mode fingerprint is seeded from
the live mode when both sides agree. When they differ, reconciliation reports a
resource conflict instead of guessing which side changed first; resolution
records the chosen mode in Base.

Reconciliation is internal and has no public CLI. The mode Base can be filled
from agreeing current states without a separate migration command.
`workspace_reconcile_boundary_smoke.py` exercises
mode-only upload, cross-platform download, reverse content edits and NOOP,
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
resource/Base transaction and post-write validation. The engine now uses these
adapters for all uploads and downloads. Apply requires an explicit `RemoteStore`
and never constructs a backend from a path embedded in the plan.

Tree paths reject traversal, absolute/drive paths, separators unsafe on Windows,
reserved Windows names, symlinks/special nodes, duplicates, and case/Unicode
collisions, including directory-prefix aliases. Empty-directory nodes cannot
have descendants. Scanner exclusions apply before capture. The additional
reserved artifact `.aikito-executable.json` stores executable paths on Windows;
it is excluded from logical fingerprints and portable tree entries. The local
transaction copies only its validated canonical metadata, rejecting extra
fields and unsafe nodes. Windows recapture reads these logical flags; POSIX
materialization applies them as native modes and writes no metadata artifact.
Missing paths left after a local deletion are ignored during mode scanning and
recapture, so a stale metadata entry cannot block sync or recreate a deleted
file. The next local write of that skill regenerates metadata from the actual
payload. A permission-only edit changes the skill mode fingerprint and triggers
reconciliation.

`workspace_payload_smoke.py` exchanges all admitted resource kinds through
encoded payloads and writes a distinct local workspace, retaining its inbox
path and excluding a credential-blocked field. It runs as a real script in all
three OS workflows. `test_workspace_payload.py` covers typed/time values,
mutation presence/deletion, corruption, client checks, immutable copies,
unsafe paths/nodes, and Windows-to-POSIX flag preservation through local copies.

## Acceptance classification

| Suite or scenario | Classification |
| --- | --- |
| `exercise_behavior(base, backend)` in `workspace_reconcile_acceptance.py` | Shared engine behavior on both backends: all admitted resources create/update/delete/recreate, credential-safe subsets, convergence, conflict Base, stale local/center plans, host-local inbox paths, local journal recovery, center-accepted/local-failed transactions, replica relocation, repeat NOOP. Shared assertions access no center directory. |
| `exercise(base)` wrapper and `FilesystemBackend` | Filesystem orchestration and center relocation; storage checkpoints inspect files only within the driver. |
| `test_workspace_reconcile_boundary.py` | Portable skill behavior and schema compatibility. The injected center-change test pins refusal between read and download staging with no local/Base write. |
| Existing conditional batch / competing-writer / empty-batch tests in `test_workspace_reconcile.py` | Shared portable storage guarantees now run in test_workspace_remote_store_contract.py on filesystem and memory stores; plaintext semantic defenses remain filesystem-specific. |
| Credential / reference / shared TOML / conflict / local safety tests in `test_workspace_reconcile*.py` | Client behavior: test_workspace_reconcile_resources.py parameterizes standalone/shared TOML, typed values, memberships, references, credentials, local recovery and conflict resolutions across both backends. Hash and read/fetch race cases in test_workspace_remote_store.py also run on both. Direct center plaintext parsing/rejection belongs to filesystem defense. |
| test_workspace_reconcile_filesystem.py plus existing center manifest parsing, `resource_for_id`, `verify_contents` and root overlap tests | Filesystem-specific defense and lifecycle. Local journal recovery remains shared engine behavior. |

`test_workspace_remote_store.py` checks immutable boundary values, rejected whole
batches, missing/corrupt fetch content, read/fetch purity, stale empty requests,
identity mismatch, client hash validation, and local/Base preservation on
conditional rejection. `workspace_remote_store_smoke.py` exposes only the five
protocol methods through a facade: the engine can access no root, lock, content
path, or journal. The script runs on Ubuntu, macOS, and Windows with local file
assertions and separate filesystem center assertions. The facade still delegates
to the real filesystem store; it is not the stage-three in-memory backend.
`test_workspace_reconcile_fetch_scope.py` verifies 60-resource NOOP previews
and applications issue zero fetch calls on both backends. A single changed
memory note fetches only that note, config changes fetch only that shared
file's fields, and conflict resolution fetches only the newly chosen download.
The real RemoteStore smoke also fails if a final NOOP fetches content.

Filesystem read/fetch optimistically capture manifest and payload content,
checking the manifest again and refusing pending journals; they create no lock
or staging files and never recover. Fetch compares the complete expected
snapshot before returning any content, including for empty requests. Commit
owns its cross-process lock, validates and stages the whole batch, and publishes
through the existing recoverable center transaction. Empty commits still check
the snapshot but do not advance revision. Recovery is explicit and requires
replanning when it repaired a transaction.

The existing version-1/version-2 center schema, identity, layout, and journal
remain supported under the skill compatibility rules above. New manifests also
record `payload_hashes`, protecting executable metadata as well as content.
Legacy manifests without hashes are verified semantically and captured read-only;
a later nonempty commit records hashes. An empty commit does not rewrite them.
Stored hash mismatch is refused without resetting state or Base. The historical
path-based `content()` and three-argument commit are replaced by fetch and
portable mutation commit; Import keeps its independent local path adapter.

## Test-only memory backend

`tests/workspace_memory_remote.py` implements `InMemoryRemote`, outside the
installed product package. It stores only canonical encoded payload bytes and
immutable descriptors, with an in-process mutex protecting snapshot comparison,
full-batch validation, and one revision publication. It has no root, Path,
center manifest, journal, staging directory, or filesystem attachment constraint.
`recover()` returns false and claims no persistent recovery capability.

The memory store treats IDs, identity, fingerprints, and references as opaque.
It checks transport hashes and before descriptors but never decodes logical IDs,
recomputes semantic fingerprints, scans credentials, or validates reference
meaning. Opaque content and even credentials or invalid logical references may
be stored with a valid transport hash; client checks must reject unsafe downloads
before local materialization. This is a content-blind contract test, not an
implementation of encryption or a hosted service.

`test_workspace_remote_store_contract.py` runs the same storage guarantees on
filesystem and memory stores: atomic batches, concurrent writers, stale/identity
rejection including empty requests, corruption, deletion versus empty-fingerprint
presence, independent immutable reads, and typed-value ownership. Additional
memory tests prohibit filesystem calls and temporary staging during store
operations and inject corrupted stored bytes or missing payloads. Filesystem
semantic and manifest defenses remain separate in `test_workspace_remote_store.py`.
The client hash, credential, reference, and read/fetch race tests run on both.

`workspace_memory_remote_smoke.py` injects `InMemoryBackend` into the existing
`exercise_behavior` scenario without engine changes or backend branches in its
assertions. Two independent workspaces exchange all admitted resource kinds,
converge, preserve host-local inbox paths and conflict Base, reject stale plans,
recover local transactions, relocate a replica, and repeat NOOP. CI runs the
script on all three OS workflows and asserts local output files and absence of
a center directory. Filesystem center relocation remains its own wrapper.

## Completed boundary and validation

The full two-replica behavior scenario runs once per backend in the CI smoke
jobs. The filesystem smoke also checks center relocation. Pytest covers focused
client and storage behavior without rerunning the full scenario.
`test_workspace_reconcile_resources.py` uses the same two-backend fixture for
client behavior and never accesses center paths. Manifest compatibility,
unmanaged center files, shared-value layout, and center journal interruption
live in `test_workspace_reconcile_filesystem.py`. Existing legacy filesystem
regressions remain useful as backend-specific checks alongside the shared suites.

The acceptance script deliberately fails local download and Base writes after
a simultaneous upload has been accepted by the center. The accepted revision
and uploaded content remain at the center; the local resources and old Base
roll back together, and replanning converges without another semantic upload.
The separate KeyboardInterrupt scenario verifies explicit local journal recovery.
Memory storage does not imitate a filesystem recovery capability.

Ubuntu, macOS, and Windows workflows call both real acceptance scripts and assert
local resources, replica state, and the results of failure/recovery scenarios.
Filesystem center assertions remain separate; the memory script asserts no
center directory exists. Local verification does not imply those remote CI
jobs have run. Release and push are independent actions, not part of validation.

Remote Store Boundary stages zero through four are implemented. This internal
boundary introduces no public reconciliation CLI, hosted service, account,
network transport, or encryption. The memory store remains test-only. A future
HTTP implementation must adopt the new request/result identity contract and
implement uncertain-commit resolution and retries before production network use;
E2EE and authorized
readable scopes need their own key and privacy design.
