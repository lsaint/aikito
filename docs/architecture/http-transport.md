# Internal HTTP transport

HTTP transports the existing Remote Protocol; it does not define a cloud
service or public REST API. `HTTPTransport.exchange(bytes) -> bytes` has no
knowledge of operations, resources, receipts or reconciliation. It imports
neither the protocol nor the store. `RemoteProtocolHandler` remains independent
of HTTP. The client uses the standard library and adds no runtime dependency.

## Endpoint contract

- Send one `POST` to the complete configured endpoint, with
  `Content-Type: application/octet-stream`. Preserve the URL path and query.
  There are no operation-specific HTTP routes.
- Return every protocol response, including an error envelope, with HTTP 200.
  Only a complete 200 body is passed to the protocol decoder. Any other status,
  including redirects, is a transport failure.
- Use a fresh connection for every exchange and send `Connection: close`.
  Neither side may require connection reuse. Do not automatically retry.
- Do not follow redirects, use environment proxies, send credentials or cookies,
  or interpret request/response bodies in the transport.
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
framing and non-200 responses remain ordinary exceptions.

`SerializedRemoteStore` maps non-delivery to `StoreUnavailable`; all other
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
advance, matching credential blocking. Planning first uses a cheap file-size
upper bound and captures a payload exactly only when that bound is exceeded.

Fetch remains an all-or-nothing single batch. The response limit therefore bounds
one download batch, including Base64/envelope overhead; it also bounds read
snapshots and every other response. There is no pagination or automatic splitting.
A capacity failure does not write partial downloaded resources or advance Base.

**Known limitation.** Uploads are bounded per round, but the center accumulates
across rounds. A new replica's first round, or a long-offline replica, may need
more than one response limit of downloads; every such round then fails until the
pending download set shrinks. Effective center capacity is therefore about one
download batch. Bounded multi-round reconciliation removes this limit and must
land before a hosted backend.

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

No public package export, cloud CLI, account, pairing, authentication, encryption,
database, hosted server, proxy support, automatic discovery, daemon or logging
is introduced. HTTP transport is an internal layer beneath the existing adapter.
