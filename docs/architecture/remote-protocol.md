# Remote Protocol

The Remote Protocol is Aikito's internal, transport-neutral serialized contract
between a client adapter and a resource-center backend. It carries the
`RemoteStore` domain operations across a byte boundary without exposing any
transport, storage backend or local path.

This document records stable constraints. It deliberately excludes product
roadmaps and hosted-service design.

## Layer position

```text
Reconciliation
    -> RemoteStore            (domain contract)
    -> Remote Protocol        (serialized operation contract)
    -> Transport              (byte delivery)
    -> RemoteProtocolHandler
    -> FilesystemRemote / InMemoryRemote
```

The protocol knows only bytes and portable domain objects. It must not import
transport, HTTP, socket, path or filesystem concerns. Endpoint routing and
authentication are outside Remote Protocol v1; see [remote binding](remote-binding.md).

## Versioning

The envelope carries its own version in `protocol`:

```python
REMOTE_PROTOCOL_VERSION = 1
```

An operation body may carry an independent `version`. Commit bodies carry
`COMMIT_ENCODING_VERSION`, which governs commit identity, digests and durable
pending/receipt state. The decoder dispatches on `operation` first, then
interprets the body version. The two version domains are independent: a future
protocol revision may still carry commit encoding v1, so changing authentication,
compression, HTTP envelope or encryption never rewrites a persisted request
identity.

## Envelope

All messages are canonical JSON bytes with a strict field set.

Request:

```json
{"protocol": 1, "operation": "read", "body": {}}
```

Success response:

```json
{"protocol": 1, "operation": "read", "ok": true, "body": {}}
```

Error response:

```json
{"protocol": 1, "operation": "read", "ok": false, "error": {"code": "...", "message": "..."}}
```

`ok` must be a strict boolean. A successful response never carries `error`; an
error response never carries `body`. The error `operation` is the recognized
operation or `null` when it cannot be identified; `null` is only valid for error
responses.

## Operations

| Operation | Request body | Success body |
| --- | --- | --- |
| `read` | `{}` | Snapshot: `sync_id`, `revision`, `resources` (no payloads) |
| `fetch` | `expected` snapshot and deduplicated `ids` | `payloads`: ID to encoded payload |
| `commit` | Full `CommitRequest` under its own `version` | Commit receipt |
| `resolve_commit` | `sync_id`, `client_id`, `request_id`, `mutation_digest` | `result`: receipt or `null` |
| `recover` | `{}` | `recovered`: strict boolean |

Every resource payload, in a `fetch` result or a commit mutation, uses one wire
shape: `{"content_hash": ..., "data": ...}`, where `data` is canonical Base64 of
the portable payload encoding. A mutation payload's `content_hash` must equal its
`after` descriptor's hash, and the portable byte length must match its `size`.

`fetch` is all-or-nothing. The response ID set must equal the requested set, and
each payload is validated against the matching expected descriptor's
`content_hash` and `size`. A decoding client also checks that the response operation matches
the request, that a commit result validates against the full original
`CommitRequest`, and that a resolve receipt matches the requested identity; the
full resolve digest check stays with the pending request owner.

`recover` is a backend lifecycle operation. It keeps the internal reconciliation
lifecycle complete; it does not promise a same-named public endpoint, and a
future backend may perform recovery server-side and collapse it.

## Error contract

Stable wire codes:

```text
snapshot_expired
invalid_content
recovery_required
store_unavailable
commit_outcome_unknown
request_identity_mismatch
store_identity_mismatch
replica_history_mismatch
protocol_error
```

Only exact `StoreError` subclasses identify themselves on the wire. A base
`StoreError` or an unclassified backend exception maps to a safe generic code and
never masquerades as a definite CAS rejection. Unknown codes, unknown protocol
versions, unknown operations and malformed bodies fail closed and never reach
backend dispatch.

The handler validates every backend result against its operation's shape before
encoding. A result it cannot encode is a server fault, never `protocol_error`:
`commit_outcome_unknown` for commit, because the commit may already be durable,
and `store_unavailable` otherwise.

Each code has a fixed safe message. Error bodies never carry local paths,
tracebacks, exception class names or `str(exc)`.

## Canonical encoding

- JSON with sorted keys, no insignificant whitespace, UTF-8, no nonfinite numbers.
- Duplicate JSON fields, invalid UTF-8, excessive nesting and noncanonical Base64
  are rejected. The nesting check covers both the envelope and payload JSON
  hidden inside Base64 fields.
- Payloads reuse the portable payload codec; descriptors, snapshots and receipts
  reuse the stable domain shapes.
- A decoded message must re-encode to the exact received bytes, so noncanonical
  input is rejected rather than silently normalized.

## Transport failure semantics

`remote_transport.py` owns `Exchange` and `TransportNotDelivered` without
depending on the protocol or store. `serialized_remote.py` retains these imports
for compatibility.

`exchange(bytes) -> bytes` describes byte delivery only. It must not encode
resource or commit meaning in exceptions.

- A transport that proves non-delivery raises `TransportNotDelivered`; the client
  maps it to `StoreUnavailable` for every operation.
- Any other exchange failure is `StoreUnavailable` for read/fetch/resolve/recover
  and `CommitOutcomeUnknown` for commit.
- After a commit enters the exchange, a lost, malformed or mismatched response,
  an unknown version/code, or an invalid success receipt is
  `CommitOutcomeUnknown`.
- The adapter never retries automatically. Recovery retains the original pending
  request and resolves it first. A missing matching receipt permits a bounded
  retry of the same complete request, even when the original delivery is unknown;
  it never permits creating a new request identity. Receipt lookup before CAS
  makes a matching replay return the original result without publishing twice.

## Commit capacity preflight

New reconciliation rounds fit the 67,108,864-byte (64 MiB) request and response
limits using complete wire sizes. Descriptor `size` is the portable encoded
payload byte length, validated alongside `content_hash` and included in request
and result digests. Version 1 is unpublished and changes in place; regenerated
golden vectors define the current encoding, without legacy migration.

Selection precedes payload fetch and marks excess work `DEFERRED`; groups that
cannot fit alone are `BLOCKED`. Upload encoding is checked again before pending
persistence, so an oversized exact-retry request is never persisted. Full
manifest capacity is a separate, known resource-count limit. See
[bounded reconciliation capacity](http-transport.md#capacity-and-framing).

Transport rejection after persistence cannot substitute for this preflight.

## Security boundary

- The protocol never carries a local, workspace or home path. The client-side
  attachment check is not serialized; a backend with no local constraint
  implements `validate_replica` as a no-op.
- Same-machine filesystem loopback assembly retains the backend's non-overlap
  check locally. A replica and center cannot be the same directory or contain
  each other, and this check makes no transport request.
- Resource IDs, `sync_id` and fingerprints stay opaque. The protocol checks
  transport integrity but does not require the storage side to parse plaintext or
  recompute semantic fingerprints.
- No account, credential, key or cipher concept exists in v1. Encryption, when
  added later, wraps the protocol payload at the transport layer and needs its own
  key and readable-scope design.

## Non-goals

Remote Protocol v1 does not define an HTTP server or client, credential issuance,
OAuth, databases, object storage, background daemons, network discovery or E2EE.
It adds no public reconciliation API or CLI.
[HTTP transport](http-transport.md) is a separate internal layer beneath the
adapter. Authentication and local identity pinning belong to
[remote binding](remote-binding.md); encryption remains a separate future layer.
A protocol handler and the test HTTP bridge are not production servers.
