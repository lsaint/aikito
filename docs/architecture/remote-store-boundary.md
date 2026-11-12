# Remote Store Boundary

`workspace.remote_store.RemoteStore` is the content-blind contract used by the
internal reconciliation engine. `FilesystemRemote` and the test-only
`InMemoryRemote` implement it; public Remote commands use an encrypted adapter.
The acceptance drivers are test orchestration, not a production service.

Related contracts: [Remote Protocol](remote-protocol.md) defines serialized
operations, [HTTP transport](http-transport.md) delivers bytes, and
[Remote Binding](remote-binding.md) pins identity and supplies credential
references. Authentication and endpoint details stay outside reconciliation;
HTTP access failures remain local `StoreError` values.

## Responsibility boundary

| Layer | Owns |
| --- | --- |
| Store | Identity/revision CAS, atomic batches and receipts, snapshot-bound fetch, transport hashes, immutable boundary values. |
| Client | Logical IDs, semantic fingerprints, references, resource policy, credential policy, safe paths and post-write verification. |
| Remote Protocol | Envelopes, operation bodies, versions, error codes, canonical encoding and integrity validation. |
| Transport | `exchange(bytes) -> bytes`; no resource, revision, path or commit semantics. |
| Backend lifecycle | Attachment constraints, physical layout, locks, journals and explicit recovery. A plaintext backend may add semantic defenses. |

IDs, `sync_id`, `replica_id`, fingerprints and references are opaque to the store.
It may enforce consistency with stored identity, but cannot require client naming
syntax, parse TOML/Markdown, inspect credentials or understand reference meaning.
Client checks run during planning and again on downloaded content; backend
checks cannot replace them or become requirements for other backends.

`validate_replica(local)` checks local attachment constraints, including
workspace/center non-overlap for the filesystem backend. The path never enters a
snapshot, payload or request; unconstrained backends may use a no-op hook. The
engine uses `local_resource_for_id`, not filesystem `resource_for_id` or
`verify_contents`, and never holds a center lock.

A center's batch `revision` is distinct from resource-level `content_version`.
Replica revision records confirmed progress; individual Base entries may lag.
Center/replica state, plans, skill copy state, authorization summaries and
journals use independent nonnegative integer revisions. Missing, boolean,
negative or noninteger revisions are rejected. Only local skill copy state reads
legacy `generation` when `revision` is absent: reads do not rewrite it; saves
write `revision`. Remote state has no legacy field fallback or automatic migration.

## Snapshot and payload contract

| Operation or value | Contract |
| --- | --- |
| `read() -> RemoteSnapshot` | Opaque `sync_id`, revision and descriptor map; no physical `ResourcePart` or paths. |
| Descriptor | Logical fingerprint, transport hash, canonical encoded payload byte size, ordered references and optional opaque `mode_fingerprint`. |
| `fetch(expected, ids)` | Return every requested payload from the exact expected snapshot, or fail. IDs are treated as a set; missing/corrupt content never produces partial success. |
| Mutation | ID, expected before descriptor or absence, and replacement descriptor/payload or explicit deletion. A present empty fingerprint differs from absence. Duplicate IDs are invalid. |
| `commit(request) -> CommitResult` | Validate the whole nonempty batch, then atomically publish resources, revision and receipt. Definite rejection changes none of them. |
| `resolve_commit(sync_id, client_id, request_id, mutation_digest)` | Return the matching latest receipt or `None`; serialize with publication and enforce identity/history checks. |
| `recover() -> bool` | Explicit backend-owned recovery. Read/fetch do not recover or create state. Replan after a repair; return false when no work exists. |

Boundary values, including nested typed values, are immutable or defensively
copied. A nonempty accepted batch advances revision once; matching retries do
not. Clients omit semantic NOOP mutations. Reference backends retain no historical
payloads: identity/revision changes invalidate fetch, even with an empty ID set.
No newer content may be labeled as belonging to an older snapshot.

Errors distinguish `SnapshotExpired` (definite conditional rejection),
`InvalidContent`, `RecoveryRequired`, `StoreUnavailable`, and
`CommitOutcomeUnknown` (delivery may have committed). Reconciliation wraps store
and client validation failures as `WorkspaceReconcileError` with the original
cause; local transaction I/O errors retain their existing behavior.

## Commit identity contract

Every logical commit has a fresh request ID scoped to center and client. Retry
and restart reuse its exact persisted bytes and identity; the old batch-only
commit API is removed.

`CommitRequest` contains client/request IDs, optional previous `ReceiptCursor`,
the complete expected snapshot, sorted nonempty mutations and mutation digest.
`workspace.remote_wire` builds and validates requests, predicts accepted
snapshots and constructs receipts. The internal commit encoding is version 1,
independent of the protocol envelope version.

The request digest binds encoding version/domain, client ID, previous cursor,
expected snapshot, before/after descriptors and exact canonical payload bytes;
it excludes request ID and the claimed digest. `CommitResult` binds center,
client, request ID, mutation digest, accepted revision and a result digest over
these fields and the complete predicted descriptor map, including unchanged
entries. It is historical confirmation, not a current snapshot. Clients can
validate it from pending without fetching old payloads; later commits do not
invalidate it.

Each client retains only its latest receipt. Within one backend critical section:

1. Validate the center, then return a matching latest receipt **before CAS**.
   A matching request ID with a different digest raises `RequestIdentityMismatch`.
2. Otherwise check revision CAS, then the previous cursor and full batch.
   Missing history permits only a `None` cursor; a mismatch raises
   `ReplicaHistoryMismatch` and blocks automatic replanning/pairing.
3. Publish resources, revision and the replacement receipt together.

An old request whose receipt was replaced fails CAS; arbitrary historical ID
reuse is not detected. Each client identity needs one active owner: cursors
cannot detect a full clone carrying the same unresolved request. Restoring a
center requires a new `sync_id`, not rolled-back revisions or receipts.

Resolution returns the original receipt only for a matching latest request and
digest. A different latest request returns `None` without comparing its digest.
Missing receipts say nothing about delayed delivery and never authorize cleanup.
Identity mismatch raises `StoreIdentityMismatch`; unrecovered/unavailable stores
raise store errors. Lost or unverifiable responses retain pending, including
when receipt validation raises `InvalidContent`.

### Encoding validation

Clients and backends independently recompute canonical digests, including before
returning cached receipts. Decoders reject unknown fields/versions, duplicate
JSON keys/IDs, forged hashes, invalid payloads and noncanonical request bytes.
JSON accepts UTF-8 text/bytes but rejects nonfinite numbers, unpaired surrogates
and nesting beyond 64 levels. Opaque Unicode identities preserve exact
codepoints. Base64 requires standard alphabet, padding and zero unused pad bits.
Booleans are not integer revisions. Boundary validation reconstructs nested
models even if constructors were bypassed; receipt encoding also revalidates.

Pending envelopes have independent schema versions/checksums. Their safe/excluded
scopes are sorted, unique and disjoint. Envelope whitespace/key order is
irrelevant, but embedded canonical requests and original replica-state text are
preserved exactly. Invalid pending, replica or center state blocks without
rewriting/erasing it. These checks preserve successful encoding bytes and versions.

## Durable receipts and pending state

`ReceiptHistory` is an immutable latest-receipt map bound to center/client
identity and accepted revision. Malformed/future receipt state blocks lookup
instead of returning false NOT FOUND.

`ReplicaState` version 2 keeps per-resource Base and, after acceptance, a receipt
cursor and completion marker. Before acceptance they may be absent. The marker
binds sync ID, request ID and digest and must agree with the cursor.

`PendingCommitStore.persist/load/complete/clear` operate under
`WorkspaceWriterLock`, recovering the local journal first. The checksummed
`pending.json` contains the exact request, original replica-state text and
safe/excluded resource IDs; it validates client/center identity, cursor, revision
and mutation scope. Current files never reconstruct an old request. Upload
descriptors/payloads support exact retry; downloads are not retained.

First upload pairing persists stable replica identity and pending in one local
journal before sending. Persist refuses replacement of existing pending.
Completion validates the receipt and supplied Base, then commits verified local
changes, Base, cursor and marker together. It cannot advance excluded resources
or substitute different upload descriptors. Recovery uses the journal's saved
inbox path policy, even after configuration changes.

Completion and cleanup are separate durable steps. Clear requires matching local
completion evidence or an explicit `SnapshotExpired` rejection, and syncs the
containing directory where supported. Transport errors and `None` resolution
never authorize cleanup. Preview refuses pending without modifying it; corrupt
or mismatched state remains available for repair.

## Receipt-aware reconciliation

On restart, recover the local journal before pending:

| Evidence | Action |
| --- | --- |
| Matching completion marker | Clear pending, even with the remote offline; never reapply local writes. |
| Matching accepted receipt | Validate it; confirm only uploaded Base entries, cursor, revision and marker; clear pending and replan current files against the latest remote. |
| `None` resolution | Resend the same complete request at most once per invocation. |
| Definite CAS rejection | Clear pending without advancing Base. A fresh round allows one replan; further rejection stops. |
| Unknown/unavailable/invalid result | Retain pending and stop. |
| `RecoveryRequired` | Explicitly recover the backend before retrying lookup. |

Accepted recovery never replays downloads, fetches historical payloads, advances
old NOOP/conflicting/blocked entries or overwrites intervening local edits.
Standalone journal recovery without pending invalidates an old plan and requires
a new invocation. An accepted remote batch is never rolled back because local
application later fails.

## Application sequence

1. Read descriptors, scan local resources and compare per-resource Base. Select a
   dependency-safe subset within request/response budgets before fetching.
   References wait for providers; provider deletion waits for reference removal.
   Only cycles require atomic grouping. Shared config validation uses the same
   download budget. `DEFERRED` resources do not advance Base or enter pending's
   safe scope; unfit groups are `BLOCKED`, not global findings.
2. Validate selected downloads and retain a bounded `(id, content_hash)` cache.
   Recheck the plan under the local writer lock, reuse verified cached payloads,
   stage downloads and capture uploads with shared-field isolation.
3. Before persisting uploads/deletes, check the complete protocol-encoded request
   against the internal 64 MiB limit. Oversize requests create neither pending
   nor commits; see [capacity preflight](remote-protocol.md#commit-capacity-preflight).
   Persist identity/pending, commit, then verify the receipt. Stale fetch or
   definite CAS rejection discards staging; uncertainty retains pending and Base.
4. Commit local resources, Base, cursor and marker together; durably clear pending
   separately. If local application fails after acceptance, retain old local
   state and pending for [recovery](#receipt-aware-reconciliation).

These are **two independent atomic transactions**, not distributed ACID. Pure
download/NOOP rounds omit request IDs, pending and commit, using the planned
revision. Normal upload completion uses the validated predicted snapshot and
retains local write preconditions and final snapshot verification. Host-local
fields stay local; safe resources advance while conflicts/blocked entries retain
Base. Moving a center does not change its identity.

An invocation accumulates applied work and remaining items across bounded rounds.
Stable inputs and fitting groups converge; repeated competition or no progress
stops with completed work intact. Conflicts/blocked resources are partial success.
Explicit choices survive deferral until applied. Persisted unpaired IDs preserve
first-pairing rules across rounds/restarts; pending recovery confirms only uploads.
Background reconciliation applies only executable changes or changed confirmation
state. Unchanged blocked/unresolved IDs alone do not trigger another round;
normal remote polling and local-change detection continue.

### First pairing and local backups

Encrypted clients adopt the **current remote** when joining an existing Vault;
binding grants no initial upload permission and stores no connection mode.
Back up local-only/differing content before local deletion/replacement under
`.local/sync-join/<UTC timestamp>-<suffix>/<original path>`. Copy shared files once;
one `index.json` maps IDs to paths/TOML fields without duplicating payloads.
Preserve skill executable modes. Backup failure stops application.

Successful rounds return actual index/resource paths, accumulated across rounds;
CLI results and background logs show absolute paths. Previews invent no archive
locations, and failed attempts are not reported as applied backups. After pairing,
users may copy selected backups back as ordinary local creations/edits for normal
bidirectional sync.

The local `pairing_remote` flag and unfinished IDs survive restart. Converged IDs
resume normal comparison. Provisional state from a rejected initial upload is not
a baseline: reconsider if another device populated the Vault. A header-only Vault
at revision 1 can be seeded locally; a previously used, now empty Vault remains
protected. None of this changes wire fields or exposes logical names/fingerprints.

<a id="skill-fingerprint-decision-and-compatibility"></a>

## Skill fingerprint and state validation

The `content-v1` skill fingerprint is SHA-256 over sorted newline-separated
`f <path> <byte hash>` records. All platforms ignore empty directories and execute
bits in that hash. A separate mode fingerprint hashes sorted executable paths;
other files are nonexecutable. Mode-only edits sync as updates; payloads carry
and validate executable flags.

Center/replica state must declare `skill_fingerprint: "content-v1"`, even with no
skills. Missing/unknown schemes or skill Base entries without mode fingerprints
are refused intact; never seed Base from live files or automatically reset it.
Protocol/commit encoding remains v1; internal descriptor/schema changes update
unpublished vectors in place without a compatibility decoder. Sizes are
nonnegative integers matching canonical payload bytes. Manifest resource-count
limits still apply; aggregate payload size is not a center limit. See
[capacity](http-transport.md#capacity-and-framing).

## Portable payload implementation

`workspace.payload` defines immutable `FilePayload`, `TomlPayload`,
`TreePayload`/`TreeEntry`, `MemberPayload`, `ResourceDescriptor` and
`ResourceMutation`, without physical resource parts. Transport hashes cover
canonical version-1 encoding, separately from semantic fingerprints. Bytes use
Base64; typed TOML retains actual key components and distinguishes bytes,
dates/times/datetimes, arrays/tables and literal dotted keys. Nested values are
reconstructed defensively.

Semantic rules remain: memory/inbox/instructions hash exact bytes, including
line endings; Agent/MCP/subagent resources use parsed semantics/references;
shared fields use typed values independent of formatting; set members/project
providers use presence with an empty fingerprint.

`payload_io.capture_resources` captures only authorized IDs and validates source
content; `capture_mutations` checks planned before/after fingerprints. Shared
payloads contain only selected fields, not neighboring blocked fields or whole
TOML files. Client credential checks operate in memory without temporary writes;
encrypted synchronization explicitly permits credential content.
`materialize_resources` validates the complete batch before writing to an owned
empty staging directory. `prepare_payload_writes` composes each shared target
once through the sync writer. The caller verifies transport hashes, owns the
local transaction and checks the final snapshot. Apply requires an explicit
`RemoteStore`, never a backend path from a plan. Import keeps its own path adapter
and formatting policy.

Trees contain sorted regular files, executable flags and implicit directories.
Clients reject traversal, absolute/drive paths, unsafe separators, Windows
reserved names, symlinks/special nodes, duplicates, case/Unicode collisions,
directory-prefix aliases and paths that are both files and parents. Scanner
exclusions apply before capture.

Windows stores logical execute flags in reserved `.aikito-executable.json`,
excluded from fingerprints/tree entries. Local transactions copy only validated
canonical metadata, rejecting extra fields/unsafe nodes. Without the artifact,
recapture falls back to enclosing Git index `100755` modes, ignoring inherited
`GIT_*` variables. POSIX applies native modes without the artifact. Missing paths
in stale metadata cannot block sync or recreate files; the next skill write
regenerates metadata from its payload.

## Remote Protocol boundary

The envelope's `protocol` version and each operation body's `version` are
independent; commit bodies use `COMMIT_ENCODING_VERSION`. Dispatch selects the
operation before its body version. The protocol validates structure, canonical
bytes, digests and transport integrity without interpreting business content or
sending local attachment paths.

Response loss, malformed responses, operation mismatches, unknown versions and
unconfirmable receipts mean unknown commit outcome. Only proven non-delivery or
a decoded backend `StoreError` selects a specific result. HTTP and
`RemoteProtocolHandler` are adapters, not production servers; see the
[operation/error contract](remote-protocol.md).

## Acceptance classification

Storage guarantees and client behavior run against both reference backends;
plaintext defenses and center filesystem details remain backend-specific.

| Coverage | Owners |
| --- | --- |
| Atomic batches, concurrent writers, identity/CAS rejection, empty commits/fetches, immutable typed values, corruption and presence/deletion | `test_workspace_remote_store_contract.py`; filesystem semantic defenses in `test_workspace_remote_store.py`. |
| Resource kinds, fields, memberships, references, credentials, conflicts, local recovery and stale read/fetch plans | `test_workspace_reconcile_resources.py`, `test_workspace_reconcile_boundary.py`, `test_workspace_reconcile_fetch_scope.py`, and shared store/client tests. NOOP fetches no payload; changes/resolutions fetch only required content. |
| Canonical bytes/digests, typed payloads, unsafe paths, execution flags and schema refusal | `test_workspace_commit_encoding.py`, `test_workspace_payload.py`, boundary tests and portable payload/commit/boundary smokes. |
| Receipts, pending completion, crashes and uncertain delivery | `test_workspace_remote_receipts.py`, `test_workspace_reconcile_pending.py`, `test_workspace_remote_receipt_crashes.py`, `test_workspace_unreliable_remote.py` and corresponding smokes. |
| Full two-replica lifecycle | `exercise_behavior(base, backend)` in `workspace_reconcile_acceptance.py`, with separate filesystem/memory wrappers. Shared assertions never access a center directory; filesystem wrapper alone checks center relocation. |

`workspace_remote_store_smoke.py` exposes only the portable interface through a
facade: the engine cannot access center roots, locks, content paths or journals.
It still delegates to the real filesystem backend, not memory storage.

Filesystem read/fetch optimistically capture state/content and recheck the whole
manifest, even for empty fetches. They create no locks/staging or recovery state
and refuse pending journals. Commit owns the cross-process lock, validates/stages
the batch and publishes through its recoverable transaction. Resources, payload
hashes, typed shared values, revision and receipts share the manifest update.
Uncommitted crashes roll back resources/receipts together; a committed marker
preserves both. Publication/cleanup uncertainty is `CommitOutcomeUnknown`.

Only filesystem center schema v2 is accepted, with fingerprint scheme, payload
hashes, shared values and receipts. Old/incomplete state or hash mismatches are
refused without rewriting the manifest or resetting Base. Hashes protect execute
metadata too. Old path-based `content()`/three-argument commit are replaced by
fetch/portable mutations; Import retains its independent local adapter.

## Test-only memory backend

`tests/workspace_memory_remote.py` implements `InMemoryRemote` outside the product
package. It stores canonical payload bytes and immutable descriptors under one
mutex, publishing snapshot, payloads and receipts together. It has no paths,
manifest, staging, filesystem calls or persistent restart guarantee;
`recover()` returns false.

It validates transport hashes and before descriptors without logical parsing,
credential scanning or reference interpretation. Valid transport content may
contain credentials/invalid logical references; client policy must reject unsafe
materialization. This proves content blindness, not encryption or hosted service
behavior. Tests inject missing/corrupt bytes, prohibit filesystem/temp staging,
and verify independent immutable reads.

## Deterministic fault acceptance

`workspace_unreliable_remote.UnreliableRemote` schedules one-shot faults with
bounded gates rather than sleeps/randomness. It retains delayed requests as
canonical bytes and audits attempts separately from delivery. Concurrent
identical requests enter the real backend critical section and return one receipt.

Shared tests cover pre-send failure, accepted response loss, resolve failure,
duplicate delivery, delayed original/retry after NOT FOUND (accept once or both
CAS-reject after another writer), local download/shared-TOML edits during a
paused response, deleted historical payloads and stale-replica receipt replacement.
Restart confirms uploads only and preserves intervening edits/excluded Base.

Real subprocess kills cover five client boundaries: replica-state publication,
committed marker, journal cleanup, and before/after durable pending removal.
Uncommitted local writes roll back; committed completion permits offline cleanup.
Already-cleared pending resumes normal operation, which may require the remote.
Separate receipt smokes kill the backend at five resource/manifest/receipt journal
boundaries. Memory storage does not imitate filesystem recovery.

## Completed boundary and validation

Ubuntu, macOS and Windows smokes run the full two-replica scenario once per
backend; pytest keeps focused cases. Coverage includes all admitted kinds,
create/update/delete/recreate, safe progress during conflicts, host-local inbox
paths, stale-plan zero writes, shared-file recovery, repeat NOOP and completed
replica relocation. A simultaneous accepted upload/local failure retains the
remote batch and rolls back local resources/Base; later planning converges without
another semantic upload. KeyboardInterrupt separately tests journal recovery.

Filesystem manifest/layout/unmanaged-content tests remain in
`test_workspace_reconcile_filesystem.py`; memory smokes assert that no center
directory exists. Cross-process commit smokes reload request/receipt/pending
encodings. `workspace_revision_smoke.py` checks refused legacy calls/bare snapshots
and unchanged NOOP revision (`contract-cleanup.json`). Shared backend acceptance
also applies to external implementations. Local verification does not mean
remote CI ran, and never implies release or push.

## Network safety invariants

These permanent Rule IDs are registered in [Engineering Invariants](invariants.md).
All are `[current]` and apply to the shared reconciliation contract.

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

## Encrypted-only Remote synchronization

`remote sync`, `remote watch` and background services share the encrypted adapter.
Binding accepts empty Stores or encrypted-v1 snapshots; plaintext/mixed stores
are refused without saving a binding. Initialization CAS-publishes the public
format header before business uploads. Other devices recover the Vault key first.

The adapter authenticates the header, completes bounded discovery and exposes
verified logical resources to the planner. Encrypt uploads before persisting
pending. Credential content is allowed; paths, references, semantic fingerprints,
byte limits and post-write verification remain enforced. Workspace files stay
plaintext. Remote operations require the optional encrypted dependency; local-only
CLI and format guards retain zero runtime dependencies.

There is no plaintext sync, in-place representation migration or rollback.
Replacing a Store requires explicit administrative deletion and reconnection;
identity changes never silently reset Base/pending. The internal store contract
does not itself introduce public accounts, services or transport.

## Recoverable remote replacements

Before an UPDATE upload, fetch/validate the old remote payload within the selected
download budget, counting full envelopes and shared config once. Apply reuses the
cache; archiving fetches nothing extra. An old version that cannot fit alone
blocks the operation before pending. NOOP, CREATE uploads and pure DELETE rounds
do not fetch old versions for archives.

Each `.local/sync-conflicts/` round and resource has a unique directory. Readable
files/trees plus `resource.json` retain the original portable payload, TOML field
paths and execute flags, even for resources sharing a physical file. Unsafe
symlinks or archive write failures stop before pending/remote commit.

## Local encrypted foundation

`aikito[encrypted]` adds only `cryptography`: HKDF-SHA256 Collection keys, opaque
IDs/keyed fingerprints, AES-256-GCM envelopes, recovery headers and authenticated
metadata are local primitives. Four portable payload kinds retain client checks
after decryption; unchanged verified resources reuse ciphertext.

`remote key recover` accepts a public header, Store `sync_id` and initialized
workspace. Recovery codes use secure terminal input or `--recovery-fd`, never
arguments/environment variables. Offline header recovery neither contacts a Store
nor enables sync.

Device keys live outside the workspace in
`encrypted-remote/keys/<vault_id>.key` under user configuration: POSIX `0600` or
owner-only Windows ACLs. Unsafe permissions, symlinks, hard links, corruption or a
different existing key are refused; saving the same key is idempotent.
`remote key storage --backend native|file` authenticates the bound Store first.
Native storage uses macOS SecurityTool over stdin, current-user Windows DPAPI,
or Linux Secret Service (`secret-tool`/`gdbus`), with no fallback when unavailable.

Default files contain Base64 VRK; owner-only v2 native records instead store
Vault/backend identity, keyed commitment and optional DPAPI blob, with no plaintext
VRK. Verify by read-back before atomic publication. Verified `--file-storage`
recovery can replace an unavailable native record only for the same key;
unreferenced native items are not deleted automatically. Homebrew uses its Python
runtime with the core cryptography Formula and tests AES-GCM/native HPKE.

Home backups may contain keys. Losing all devices and the recovery code makes the
Vault unrecoverable. Regenerating a code preserves VRK, so an old code/header pair
still works; revocation needs future VRK rotation. The frozen
`tests/fixtures/remote_protocol_vectors/encrypted-v1.json` is shared with an
independent external consumer and contains test keys only.

## Bounded encrypted discovery

`remote key inspect` uses an identity-pinned read/fetch-only reader and
authenticates the format header with the device key before interpreting logical
resources. Missing keys or unsupported headers stop before business downloads.

Each round downloads at most 32 business resources. Header/business requests share
full-manifest request/response limits; unchanged verified descriptors reuse a
local authenticated map. An individually unfit resource fails explicitly.
Discovery does not fetch reference closure outside its selected scope.

Private-tag resources come first; authenticated project members introduce project
Memory tags. Unknown tags fail closed afterward. Descriptor reference targets
must exist in the opaque manifest, decrypted references must match descriptors,
and the complete logical view must pass `missing_references`. Unknown types are
not imported. Incomplete views authorize no absence decisions, pairing or writes.

The local checkpoint `.local/state/aikito/encrypted-discovery.json` contains
verified logical descriptions/opaque descriptors, never payloads or keys. Its
HKDF-SHA256 HMAC key uses the local-only `aikito.discovery-state` domain and Vault
salt; the MAC binds canonical version, `sync_id`, `vault_id` and entries. Validate
before reuse, filter against current descriptors and atomically save successful
rounds under the workspace lock. Failed fetch/tamper/identity checks preserve
progress. This map is neither Replica Base nor a remote protocol format;
inspection creates no sync capability or representation migration.

## Encrypted planner and wire requests

Discovery checkpoint v3 also authenticates plaintext payload hash/size descriptors;
older maps are rejected. The codec gives the planner a complete authenticated
logical snapshot. Discovery and application are separately bounded; unchanged
in-memory headers are reused and changed headers reauthenticated.

Planning uses logical IDs; budgets use the full opaque snapshot, format header,
opaque references, encrypted sizes and reserved header fetch cost. Prepare exact
encrypted mutations for sizing, reuse them at capture and persist before sending.
Format metadata never participates in business deletion, pairing, merge or Base.

Pending envelope v2 contains the exact wire request, public `vault_id` and local
logical replica state/scope, without plaintext business payloads, recovery code or
VRK. Before retry, verify current format/Vault, then reuse the same request ID,
nonce, ciphertext and digest. Accepted receipts decrypt/validate captured uploads
before confirming logical Base; derive DELETE IDs from the safe logical scope.
No historical payloads are fetched and later local edits remain untouched.

Empty-Store initialization securely delivers/saves recovery material and persists
the public-header CAS request before sending. Only one initializer wins; definite
losing CAS clears its request without replacing the winner's header or deleting
possibly referenced keys. Lost responses reuse the request without redisplaying
recovery codes. Failed output cannot publish a format marker. Initialization never
converts nonempty plaintext Stores; ordinary and background sync share this path.

### Historical object cleanup

Only exclusive offline administrator ownership permits removal of unreferenced
server objects; retain current objects, state and receipts. Plaintext backups and
filesystem snapshots remain outside E2EE's scope. Encrypted-only HTTP refuses
legacy Stores.

### Scoped resource material

`scoped_export` uses native HPKE from `cryptography` and accepts protocol v2 only.
Explicit read exports receive the content key; write exports additionally
receive resource-ID/fingerprint keys. VRK, Collection master, tag and private
Collection keys never leave the client.

Scanning, discovery and sync use the current Collection layout. Existing Vaults
need one-time local conversion before resuming; maintenance scripts are untracked.
See [collection exports](collection-export.md) for policy, CLI and vectors. The
content-blind Remote protocol and encrypted resource Envelope remain unchanged.
