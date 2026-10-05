# Internal HTTP transport

HTTP transports the existing Remote Protocol; it does not define a public REST
API. `HTTPTransport.exchange(bytes) -> bytes` has no
knowledge of operations, resources, receipts or reconciliation. It imports
neither the protocol nor the store. `RemoteProtocolHandler` remains independent
of HTTP. The client uses the standard library and adds no runtime dependency.

## Endpoint contract

- Send one `POST` to the complete configured endpoint, with
  `Content-Type: application/octet-stream`. Preserve the URL path and query.
  There are no operation-specific HTTP routes.
- Return every protocol response, including an error envelope, with HTTP 200.
  Only a complete 200 body is passed to the protocol decoder. Contracted 401/403
  are pre-dispatch access rejections; other non-200 statuses, including redirects,
  remain transport failures.
- Use a fresh connection for every exchange and send `Connection: close`.
  Neither side may require connection reuse. Do not automatically retry.
- Send only fixed transport headers and the caller's explicitly provided
  `authorization` value. Reject header controls and non-ASCII values before
  connecting. Never include authorization in exception messages or object repr.
  Do not follow redirects, use environment proxies, read environment credentials,
  send cookies or interpret request/response bodies in the transport.
- Accept HTTP and HTTPS. HTTPS verifies certificates and hostnames with the
  default trust store; there is no insecure mode.
- Invalid endpoints, embedded user information, fragments and invalid timeouts
  are rejected during construction. Non-ASCII paths must be URL encoded.

The test server listens on an ephemeral `127.0.0.1` port and uses `/v1/remote`.
That path is a test convention, not a public compatibility promise.

## Delivery and timeouts

`TransportNotDelivered` reports only proven failures before TCP establishment:
DNS lookup, connection refusal and local socket allocation failures. The TCP
connection step is separate from HTTPS negotiation. TLS failures, ambiguous
connection failures/timeouts, send failures, resets, response timeouts, malformed
framing and non-200 responses other than contracted 401/403 remain ordinary
exceptions.

`TransportRejected` reports 401/403 only under the endpoint contract that access
checks happen before reading or dispatching protocol operations. Response bodies
are not read and the connection is closed. `SerializedRemoteStore` maps these to
`RemoteAccessDenied`, a local `StoreError` rather than a protocol error code.
Even rejected commits retain pending. Binding and credential construction belong
to the [remote factory](remote-binding.md), never HTTP or reconciliation.

`SerializedRemoteStore` maps non-delivery to `StoreUnavailable`; remaining
exchange failures are `CommitOutcomeUnknown` for commit and `StoreUnavailable`
for other operations. A timeout never proves that a commit was rejected.
Retain the exact pending request and resolve its original identity on restart.
Only the existing bounded pending recovery path can resend it.

The default timeout is 30 seconds **per idle socket operation**, not an overall
deadline. A peer that continually sends bytes may keep an exchange active longer
than that timeout. This internal transport does not implement a total deadline.

## Capacity and framing

Both limits in `remote_limits.py` are 67,108,864 bytes (64 MiB), measured as
encoded body bytes. They are internal capacity policy, independent of protocol
and commit-encoding versions. Servers must enforce the same request limit.

New reconciliation commits are checked using their complete protocol encoding
**before pending persistence**. An oversized commit fails as
`WorkspaceReconcileError`, with no pending state and no commit exchange. Rejecting
only inside the HTTP transport would leave an exact-retry request unable to
progress. Existing pending requests retain their recovery identity and semantics.

A single resource is limited to 16,777,216 bytes (16 MiB) of portable payload
encoding (`MAX_RESOURCE_PAYLOAD_BYTES`), so one resource with Base64 and
envelope overhead always fits a request or response. Planning marks an
oversized local upload `BLOCKED` with a size reason; other resources still
advance, matching credential blocking. Planning captures and encodes each upload
once to measure its exact size, then releases its payload bytes.

Reconciliation selects a bounded round before any payload fetch. Download budgets
include exact Base64 lengths, escaped IDs, JSON wrappers and envelopes; fetch
requests include the complete expected descriptor snapshot. Upload selection sums
the exact encoded mutation sizes and commas with the fixed
commit envelope. Each upload is captured individually to retain only its
size, transport hash and descriptor; its bytes are released before capturing
the next resource. Application captures only the selected uploads and checks
the complete protocol encoding before pending persistence.
Shared TOML validation fields share the download budget. Verified payloads are
cached by `(id, content_hash)` for revalidation and application, without duplicate
fetches. A descriptor change invalidates the relevant cached content; semantic
validation still runs against the current descriptor before writing.

Dependency ordering permits a project to span multiple rounds: providers precede
new references and reference removal precedes provider deletion. Only dependency
cycles require an atomic group. Unselected work is `DEFERRED`; an individually
unfit group is `BLOCKED`, while unrelated resources can advance. Each round keeps
its own atomic remote commit and local transaction. The whole invocation is not
atomic. There is no pagination, streaming or server-side staging.

**Known limitation.** Center payload totals can exceed a single response limit,
but resource counts remain bounded by the full descriptor manifest. Read responses,
fetch requests and commit requests still carry that manifest. Manifest capacity
is checked before payload fetch or partial writes, including the prospective
accepted snapshot. Removing this restriction needs a separate protocol design.

Reject an excessive declared `Content-Length` before reading the body. Reject
invalid or repeated lengths, a length combined with transfer encoding, and
unsupported transfer encodings. Chunked and close-delimited responses are read
in bounded chunks; stop after at most one byte beyond the limit. Early EOF with
a declared length or an incomplete chunk is a failure.

`http.client` stops at the declared HTTP body boundary. This transport does not
claim to detect bytes a peer sends after that boundary; the connection is closed
and never reused. It does not reimplement HTTP framing to inspect trailing data.

## Compatibility artifacts

`tests/fixtures/remote_protocol_vectors/manifest.json` describes raw request and
response `.bin` files, error envelopes, malformed inputs and SHA-256 digest
preimages. Operation pairs run in manifest order against the stated initial
store identity. Error and malformed vectors are independent negative examples.

Canonical encoding must match byte for byte: UTF-8, sorted fields, compact JSON,
no ASCII/HTML escaping, strict Base64 and the existing numeric encodings.
Non-ASCII, `<`, `>`, `&`, U+2028 and U+2029 examples guard independent encoders.
Digest preimages distinguish the domain commit encoding from its protocol
envelope. Generate and verify with:

```sh
PYTHONPATH=src python tests/generate_remote_protocol_vectors.py
PYTHONPATH=src python tests/generate_remote_protocol_vectors.py --check
```

To run black-box store and receipt contracts against an external implementation:

```sh
export PYTHONPATH=src
export AIKITO_REMOTE_TEST_SERVER_CMD='python tests/http_remote_server.py --root {root}'
python -m pytest tests/test_workspace_remote_store_contract.py tests/test_workspace_remote_receipts.py
```

The command is split into argv without a shell; quote arguments containing
spaces. `{root}` is substituted after splitting. Every test receives a new empty
root, starts its own process and reads the listening URL from the first stdout
line. Startup is bounded and teardown terminates/reaps the process. No reset or
control endpoint is required. Backend-specific corruption and persistence tests
are skipped in this mode; socket faults are tested only against the in-process
test bridge. The bundled server proves the external harness in CI.

## Scope

`tests/http_remote_server.py` is test infrastructure, not part of the production
wheel and not a hardened hosted server. HTTP acceptance runs existing portable
reconciliation behavior plus real lost-response recovery in three-platform smoke
jobs. Unit tests focus on framing, failure classification, pending identity and
capacity boundaries.

No public package export, reconciliation CLI, pairing UI, encryption, database,
production server, proxy support, automatic discovery, daemon or logging is
introduced. HTTP authentication and local binding remain internal layers beneath
reconciliation, without changing protocol bytes.

## Multi-store and restart test assembly

An independent implementation can opt into the multi-store contract with an argv
placeholder `{config}` in `AIKITO_REMOTE_TEST_SERVER_CMD`. It points to a temporary
UTF-8 JSON file with this test-only shape:

```json
{"listen":"127.0.0.1:0","stores":[{"store_id":"alpha","token":"test-token","initialize":true}]}
```

The harness supplies `{root}` and `{config}` after argv splitting. Tokens are
synthetic test credentials. `initialize` defaults to true; false declares an
authorized but absent store. One token may have several store grants. The process
announces the first configured store's complete endpoint as its first stdout line.
Store routes use `/v1/stores/{store_id}/remote` on that listening address, with
strict lowercase ASCII IDs. These routes are a test assembly convention rather
than a Remote Protocol field. Protocol `sync_id` remains independently opaque.

Restart preserves the root, grants and listening address. The harness writes the
announced address back into `listen`, terminates/reaps the process, then starts it
again with the same configuration. Already initialized stores must reopen their
persisted state. A request never creates an undeclared store. Contract checks
cover independent resource/revision/receipt state, concurrent CAS, stable identity,
durable receipt resolution and exact-request replay after restart.

Missing or invalid credentials return 401. A valid token without the requested
store grant returns 403 even when that store does not exist; authorization
precedes existence and body consumption. An authorized absent store returns 404.
These statuses describe service assembly, not protocol error codes. The existing
single-store `{token}` mode also supplies one synthetic credential to ordinary
store and receipt fixtures, so those contracts can use authenticated endpoints.

`tests/workspace_external_server_smoke.py` runs portable reconciliation acceptance
and BoundRemote recovery through a relay that forwards protocol bytes and drops
one accepted commit response. The server is then restarted while pending remains;
resolution confirms the durable receipt without advancing revision twice. Public
CI runs the same assembly against the bundled test server. External implementations
run it locally with their `{root}` / `{config}` command; no fault or control endpoint
is required.

Run the multi-store contract against the bundled server with:

```sh
PYTHONPATH=src AIKITO_REMOTE_TEST_SERVER_CMD='python tests/http_remote_server.py --root {root} --test-config {config}' \
  python -m pytest tests/test_workspace_http_multistore_contract.py
PYTHONPATH=src python tests/workspace_external_server_smoke.py /tmp/external-server-check
```

Use a fresh smoke directory. The smoke defaults to the bundled server unless a
`{config}`-aware external command is already set in the environment.
