# Remote Store Boundary

The [HTTP transport](http-transport.md) carries this internal contract through a
real socket without changing reconciliation or storage semantics. Remote Protocol
is independent of HTTP; neither HTTP transport nor `RemoteProtocolHandler`
constitutes a production server.

The internal reconciliation engine uses `workspace.remote_store.RemoteStore`
and portable payloads. `FilesystemRemote` and the test-only `InMemoryRemote`
implement the same contract. There is no public reconciliation CLI or service;
the acceptance driver is test orchestration.

A [local remote binding](remote-binding.md) fixes the center identity and supplies
credential references through a factory. HTTP access errors remain local
`StoreError` values; authentication and endpoint details never enter the store
contract or reconciliation.

## Commit identity contract

`RemoteStore` defines `commit(request: CommitRequest) -> CommitResult` and
`resolve_commit(sync_id, client_id, request_id, mutation_digest) -> CommitResult | None`,
alongside read, fetch, attachment and recovery operations. The former
`commit(expected, mutations)` interface is removed: every accepted batch must
publish a receipt. A result is a historical confirmation, not a snapshot to use
for subsequent planning. Clients validate it against the request and read the
store again when they need its current state.

`CommitRequest` carries an opaque client ID, stable request ID, optional
`ReceiptCursor`, complete expected snapshot, nonempty mutation tuple and
mutation digest. Mutations are sorted by opaque ID; duplicate IDs and empty
batches are rejected. Snapshots and typed payloads remain immutable. IDs and
fingerprints are not parsed for semantic meaning.

`workspace.remote_wire` supplies `build_commit_request`, canonical request
encoding/decoding, digest validation, accepted snapshot prediction and receipt
construction/validation. Its internal version-1 encoding supports durable
pending persistence; it is not a frozen HTTP wire format. Request digests
cover the encoding version and domain, client ID, previous receipt, complete
expected snapshot, before/after descriptors and the exact existing canonical
payload bytes. Request ID and the claimed digest are excluded. Descriptor
encoding retains fingerprints, transport hashes, encoded payload byte sizes, ordered references and the
current optional opaque `mode_fingerprint`. Absence is `null` and
differs from a present member with an empty fingerprint. Decoding rejects
unknown fields/versions, duplicate JSON fields/IDs, invalid payload/hash pairs,
noncanonical bytes and forged digests. Backend boundaries must independently
recompute digests, including before returning a cached receipt.
`ResourceMutation` does not interpret client-shaped IDs. The existing skill
executable-state check belongs to the plaintext filesystem backend; client
capture and materialization retain their semantic validation.

The durable JSON decoder accepts UTF-8 text/bytes, rejects duplicate fields at
every level, nonfinite JSON numbers, unpaired Unicode surrogates and more than
64 container levels. Opaque Unicode identities retain their exact codepoints;
they are not normalized. Base64 fields require standard alphabet, padding and
zero unused pad bits, with exactly one representation of each byte sequence.
Request boundaries reconstruct nested snapshots, descriptors, cursors, payloads
and tree entries to recheck their invariants even if constructors were bypassed.
Booleans cannot satisfy numeric revision requirements by comparing equal to
integers. Receipt validation and encoding also recheck model fields.

Pending envelopes keep independently versioned schemas and checksums. Their
safe/excluded scopes must be sorted, unique and disjoint, and the embedded
request must have canonical encoding. Envelope JSON whitespace and field order
are immaterial; the embedded original replica-state text is retained exactly
for identity/history checks. Invalid pending or replica/center encoding
blocks without erasing or rewriting state. Successful encoding bytes and
encoding versions remain unchanged by this hardening. These are internal
persistence requirements; HTTP message representation remains a later step.

`test_workspace_commit_encoding.py` covers these boundaries and independently
checks that result digests bind the version/domain and complete predicted
snapshot, including unchanged descriptors. The three OS smoke jobs run
`workspace_remote_commit_smoke.py` in generation and verification processes,
checking durable request, receipt and pending records plus malformed-input
rejection reports after restart.

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
requires durable client identity and pending recovery before new requests.

## Durable receipts and pending state

`ReceiptHistory` holds an immutable latest-receipt map for one center. New
requests check that history before CAS and validate the previous cursor only
after CAS succeeds. Lookup of another request returns `None`; it does not
compare that other request's digest. Receipt records have their own persistence
version and bind center/client identity and accepted revision. Invalid or future
receipt state blocks lookup rather than masquerading as a missing request.

The filesystem manifest carries resources, payload hashes, revision and receipts
in the same journaled state update. Ordinary read/fetch/resolve never recover
a pending journal. Resolution takes the backend writer lock and raises
`RecoveryRequired` until explicit recovery. Process interruption before the
journal is committed rolls back resources and receipts together; interruption
after the committed marker preserves both. Errors during publication or after
publication/cleanup are `CommitOutcomeUnknown`, not definite rejection. The
memory backend publishes its snapshot, payloads and receipt history together
under its mutex and makes no durability promise across process restart.

`replica_state.ReplicaState` retains version-2 read compatibility and adds an
optional receipt cursor and completion marker. The marker binds sync ID,
request ID and mutation digest; it must agree with the cursor. Existing state
without these fields remains readable without being rewritten. Center identity
and replica revision validation still belongs to the client.

`pending_commit.PendingCommitStore` provides persist/load/complete/clear. Its
versioned `pending.json` envelope stores the exact canonical request, original
replica-state text and safe/excluded resource IDs, protected by an envelope
checksum. It validates replica/client identity, previous cursor, original
revision and mutation scope. Current files never reconstruct an old request.
Downloads are not retained; the request itself contains upload after descriptors
and payloads needed for exact retry. Corrupt, unsupported or mismatched pending
state blocks further writes and remains available for repair.

Every store operation holds `WorkspaceWriterLock` and recovers the local journal
first. Local transactions use the configured inbox path; recovery restores the
journal's saved path policy even if that configuration changes. First pairing
writes replica identity and pending through one local
journal before any caller may send the request. Persist refuses to replace an
existing pending record. Completion validates the receipt and supplied Base,
then commits caller-verified local changes, per-resource Base, cursor and marker
together. It cannot advance excluded Base resources or substitute different
upload descriptors. A matching completion marker prevents reapplying local
writes after restart, even without a remote connection.

Cleanup requires either the matching local completion marker or an explicit
`SnapshotExpired` rejection supplied by the caller. `None` lookup and transport
errors do not permit cleanup. Removal syncs the containing directory where
supported. Completion and cleanup are deliberately separate durable steps.
Preview blocks when pending exists and does not modify it. Applying a round
recovers the local journal first, then resolves pending before new planning.

## Receipt-aware reconciliation

Reconciliation stages and validates downloads and captures uploads before
persisting the exact request. First pairing persists replica identity in that
same step. Only a nonempty upload batch calls `commit`; pure downloads and NOOP
rounds update local state against their fetched/planned revision without a
request ID or pending record. Normal successful completion uses the predicted
accepted snapshot after verifying the receipt, preserving the existing local
write preconditions and final snapshot validation. Resources, Base, cursor and
completion marker share one journal, followed by separate pending cleanup.

Restart checks completion evidence before accessing the remote. A matching
marker permits offline cleanup. Otherwise `commit_recovery.recover_pending`
queries the stored identity and validates the returned receipt. `None` permits
one exact resend per invocation. Definite CAS rejection clears pending without
changing Base; unavailable, unknown or invalid results retain it and stop.
Backend journal recovery is explicit when lookup raises `RecoveryRequired`.

Accepted recovery updates only uploaded Base entries and the receipt cursor,
revision and completion marker. It writes no local resources and does not fetch
old payloads, replay downloads, or advance old NOOP/conflicting/blocked entries.
Pending is then cleared, and the engine plans against current local resources
and the latest remote snapshot. User edits made during the failed round remain
available to that plan. A fresh CAS rejection permits one replan in the same
invocation; further rejection stops. Standalone journals without pending still
invalidate the old plan and require a new invocation.

## Deterministic fault acceptance

The test-only `workspace_unreliable_remote.UnreliableRemote` forwards the portable
store interface and schedules one-shot faults. It retains delayed requests as
canonical bytes and audits each commit and actual delivery separately. Gates
control arrival and response release with bounded waits; no sleeps or random
failure probabilities determine execution order. Concurrent duplicates enter
the real backend critical section and must return the same receipt.

`test_workspace_unreliable_remote.py` runs shared boundary tests on memory and
filesystem stores: failure before send, accepted response loss, resolve failure,
concurrent duplicate delivery, and delayed original/retry delivery after a
missing receipt. The delayed cases cover both acceptance once and CAS rejection
of both deliveries after another client advances revision. Further cases pause
the accepted response while editing a download target or shared TOML file,
confirm an old receipt after other clients delete historical payloads, and
verify a stale replica cannot replace an unknown winner's recovery receipt.

All three OS smoke workflows invoke `workspace_unreliable_remote_smoke.py` on
both stores. Its historical round includes upload, deletion, download, NOOP
and credential-blocked resources. Restart confirms only uploads, then replans
against current content while preserving user edits and excluded Base entries.
The filesystem run also kills real client subprocesses at five local boundaries:
replica-state publication, journal committed marker, journal cleanup, and before
and after durable pending removal. Recovery rolls back uncommitted local writes,
or retains completion and permits pending cleanup with the remote offline.
Already-cleared pending resumes the normal lifecycle, which may need the remote.
These tests complement `workspace_remote_receipts_smoke.py`, which separately
kills the backend at five resource/manifest/receipt journal boundaries.

## Snapshot and payload contract

The implemented data interface is `read() -> RemoteSnapshot`,
`fetch(expected, ids) -> Mapping[str, ResourcePayload]`,
`commit(request) -> CommitResult`, and `recover() -> bool`.
The reference backends retain no historical payload content: a changed center
identity or revision makes fetch fail with `SnapshotExpired`, including an
empty fetch. Clients discard staging and replan. Operations either return a
result from one coherent snapshot or fail; a newer payload cannot be labeled
as content from an older snapshot.

| Value | Required semantics |
| --- | --- |
| Snapshot | Opaque `sync_id`, nonnegative revision, resource descriptors indexed by opaque ID; no physical `ResourcePart` or paths. Descriptors carry a logical fingerprint, transport hash, encoded payload byte size, and an optional opaque content version. |
| Payload | Versioned, deterministic, portable content with no disk paths, handles, or shared directory requirements. The backend verifies the transport hash without interpreting the content. |
| Mutation | Opaque resource ID, expected before descriptor or absence, and replacement descriptor plus payload, or an explicit delete with no payload. An empty fingerprint denotes presence and differs from absence. Duplicate IDs are invalid. |
| Ownership | Snapshots, descriptors, mutations, and returned payloads are immutable or defensively copied at each boundary, including nested typed values. Caller mutation cannot affect stored or previously read state. |
| Commit | Validate the request, center identity, latest receipt, CAS and previous cursor; check the whole nonempty batch before atomically publishing resources, revision and receipt. Definite rejection leaves all three unchanged. |
| Revision | A nonempty accepted batch advances revision exactly once; matching retries return the original receipt without advancing. Empty batches are invalid. Clients omit semantic NOOP mutations. |
| Fetch | All requested IDs must be present and have intact content. No partial success or implicit missing values; duplicates in the requested collection are treated as a set. |
| Recovery | Read and fetch never recover or create state. Pending transactions raise `RecoveryRequired`; explicit backend recovery owns locking and journals. Successful recovery requires replanning. A backend without recovery work returns false. |

Errors distinguish `SnapshotExpired` (definite conditional rejection),
`InvalidContent` (bad mutation, missing or corrupt payload), `RecoveryRequired`,
and `StoreUnavailable`. Commit also distinguishes
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
stored identity but not a client-specific ID format. The contract uses the
replica ID as an opaque client ID for receipt retention. A filesystem backend keeps
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

1. Read descriptors, scan the local workspace, and compare against per-resource
   Base. Select a dependency-safe subset within request and response wire budgets
   before fetching any payload. New references wait for missing providers;
   provider deletion waits for reference removal. Only cycles are atomic groups.
   Remote config fields needed to validate shared TOML merges count toward the
   same download budget. `DEFERRED` resources neither advance Base nor enter the
   pending safe scope. Unfit groups are `BLOCKED`, not global findings.
2. Validate selected downloaded content and retain a bounded `(id, content_hash)`
   cache. Replanning under the local writer lock checks the current snapshot and
   reuses verified content without duplicate fetches. Stage selected downloads
   locally and capture selected uploads; preserve shared TOML field isolation.
3. For uploads/deletes, check the complete protocol-encoded request against the
   internal 64 MiB request limit before persisting anything. Oversized commits
   fail without pending state or a commit call; see
   [commit capacity preflight](remote-protocol.md#commit-capacity-preflight).
   Persist stable replica identity and the complete pending request before
   sending. Commit and verify the receipt. Stale fetch or explicit
   CAS rejection discards staging; uncertainty retains pending and old Base.
   Rounds without uploads skip pending and commit entirely.
4. Commit local resources, Base, cursor and completion marker together under the
   local writer lock, then durably clear pending. A local rollback after remote
   acceptance preserves old Base and pending. On restart, recover the local journal,
   resolve pending conservatively, and replan against current content as described
   in [Receipt-aware reconciliation](#receipt-aware-reconciliation).

These are two separate transactions, not a distributed atomic commit. Center
address changes do not change identity. Conflict/blocked resources retain Base,
safe independent resources advance, and local-only fields remain local.

An invocation repeats bounded rounds, returning accumulated applied items,
remaining deferred/conflicting/blocked items, round count and any stop reason.
It converges when inputs stabilize, groups fit and storage stays available;
continuous competition instead stops at bounded retry/round limits. A no-progress
round stops with completed work intact. Conflicts and blocked resources alone
are partial success. Explicit resolutions survive deferral and are removed only
when applied. Replica state persists unfinished first-pairing IDs so template
ancestors and preservation rules survive multiple rounds and restarts. Pairing
progress commits with Base; pending recovery confirms only uploaded IDs.

The protocol and commit encoding remain version 1. These internal features are
unpublished: descriptor byte size changes the encoding and golden vectors in
place, without a compatibility decoder or migration. Size is a nonnegative
integer and must match the encoded payload, independently of its transport hash.
The descriptor manifest still limits resource counts, even though aggregate
payload size no longer limits the center; see [capacity](http-transport.md#capacity-and-framing).

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
through the existing recoverable center transaction. Receipt-aware commits
reject empty batches and all accepted batches publish receipts. Recovery is explicit and requires
replanning when it repaired a transaction.

The existing version-1/version-2 center schema, identity, layout, and journal
remain supported under the skill compatibility rules above. New manifests also
record `payload_hashes`, protecting executable metadata as well as content.
Legacy manifests without hashes are verified semantically and captured read-only;
a later nonempty commit records hashes. An empty commit is invalid and does not rewrite them.
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
rejection, invalid empty commits and stale empty fetches, corruption, deletion versus empty-fingerprint
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

The network-safe request, receipt, pending recovery and fault-injection contract
is implemented for both reference backends. This internal
boundary introduces no public reconciliation CLI, hosted service, account,
network transport, or encryption. The memory store remains test-only. A future HTTP implementation must run the shared store and reconciliation
acceptance suites. Remote Protocol v1 is an unpublished internal compatibility
contract while the HTTP transport and E2EE remain open; authorized readable
scopes need their own key and privacy design.

## Remote Protocol boundary

The Remote Protocol is the serialized operation contract between a client
adapter and a backend. It is a distinct layer from the domain contract, from
byte delivery, and from any particular backend:

| Layer | Responsibility |
| --- | --- |
| `RemoteStore` | Content-blind domain contract: identity/revision CAS, complete batch publication, snapshot-bound fetch, receipts and recovery. |
| Remote Protocol | Serialized operation contract: canonical envelope, operation bodies, versioning, error codes and strict validation. |
| Transport | Byte delivery only: `exchange(bytes) -> bytes`, no resource, revision, path or commit semantics. |
| `FilesystemRemote` | One local backend implementation with its own path mapping, lock and journal. |

The envelope carries its own `protocol` version and names one operation. Each
operation body may carry an independent `version`; commit bodies carry
`COMMIT_ENCODING_VERSION`. The decoder dispatches on `operation` before reading
any body version, so the two version domains stay independent and a later
protocol revision can still carry commit encoding v1.

The protocol is content-blind. It validates envelope structure, canonical
encoding, digests and transport integrity, but it does not interpret resource
IDs, recompute semantic fingerprints, inspect references, or read plaintext
TOML, Markdown or credentials. `FilesystemRemote` may add plaintext defenses as
a backend implementation; those never become a general protocol requirement.

The protocol never carries a local workspace path. `validate_replica(local)` is
a client-side attachment check; a backend with no local constraint implements it
as a no-op, and the local assembly keeps any same-machine non-overlap check
without sending the path over the wire.

Committed response loss, a malformed response, an operation mismatch, an
unknown message version or an unconfirmable commit receipt are all treated as an
unknown commit outcome by the client adapter, never as a definite rejection.
Only a transport that proves non-delivery, or a decoded backend `StoreError`,
selects a specific result. See [Remote Protocol](remote-protocol.md) for the
current operation and error contract.

## Network safety invariants

These permanent Rule IDs are registered in [Engineering Invariants](invariants.md).
All rules below are `[current]` and apply to the shared reconciliation contract.

| Rule ID | Invariant |
| --- | --- |
| `INV-SYNC-01` | Base advances per resource only after confirmation: uploads use accepted after descriptors, downloads require successful local application, and conflict/blocked/deferred resources retain their old Base. Replica revision records confirmed progress, not complete convergence. |
| `INV-SYNC-02` | Every new logical commit uses a fresh request ID scoped by center and client. Retry and restart reuse the exact persisted request, including its original ID and digest. |
| `INV-SYNC-03` | The latest matching request returns its first accepted result before CAS, without reapplying mutations or incrementing revision. A different digest for that ID is an identity error. An identical old request whose receipt was replaced fails CAS. |
| `INV-SYNC-04` | An unresolved pending request prevents new mutation commits. Delivery uncertainty, unavailable resolution, invalid responses and corrupt state retain pending and do not advance Base. |
| `INV-SYNC-05` | NOT FOUND reports only the absence of an accepted latest receipt. It permits a bounded resend of the same request; it never permits a new identity or pending removal. Delayed delivery and retry accept at most once. |
| `INV-SYNC-06` | Remote resources, revision and latest receipt publish in one transaction. Resolution serializes with publication and refuses unresolved backend journals rather than reporting false NOT FOUND. |
| `INV-SYNC-07` | Local resources, per-resource Base, receipt cursor and completion marker commit in one local transaction. Pending is durable before send and is cleared durably after completion or explicit CAS rejection. These are independent transactions, not distributed ACID. |
| `INV-SYNC-08` | Recovery restores the local journal first. A matching completion marker permits cleanup without contacting the remote or reapplying writes. Otherwise confirmation advances only uploaded Base entries, then current files and the current remote are replanned. |
| `INV-SYNC-09` | A historical receipt is not a current snapshot and does not promise downloadable historical payloads. Recovery does not replay old downloads, depend on old staging, or overwrite intervening local edits. An accepted upload advances Base to its after descriptor even when local content has since changed. |
| `INV-SYNC-10` | Each client retains only its latest receipt. A new commit must supply the matching previous cursor; stale or missing client history blocks replacement. Other clients cannot replace that receipt. A client identity has one active owner, and center restoration requires a new sync ID. |
| `INV-SYNC-11` | Clients and backends independently validate versioned canonical request/result digests. Digests bind all commit semantics and the complete predicted descriptor map. Invalid encoding, identities or history block without erasing durable state. |
| `INV-SYNC-12` | Empty commit batches are invalid and cannot publish resources, revision or receipts. Rounds without uploads create no request or pending and call no commit; local progress uses the planned remote revision. First upload pairing persists stable replica identity with pending before send. |
| `INV-SYNC-13` | Commit and resolution use opaque identities, descriptors and payloads without center paths or client-held center locks. Local attachment checks and filesystem journals belong to backend lifecycle. The contract adds no public cloud, authentication, daemon, database or HTTP surface. |

Verification is shared between filesystem and memory backends in
`test_workspace_remote_receipts.py`, `test_workspace_remote_store_contract.py`,
`test_workspace_reconcile_pending.py` and `test_workspace_unreliable_remote.py`.
`test_workspace_remote_receipt_crashes.py` adds filesystem transaction checkpoints;
`test_workspace_commit_encoding.py` checks canonical integrity. Independent-process
smokes exercise remote and local crash recovery, receipt persistence, and encoding
reload on all three OS workflows. `workspace_revision_smoke.py` also verifies that
legacy batch calls and bare snapshots are refused and NOOP preserves revision; its
`contract-cleanup.json` artifact is asserted by each workflow.
