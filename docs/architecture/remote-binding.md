# Remote Binding

A remote binding pins one local workspace to a resource center. The endpoint
locates it; `sync_id` identifies it. Reusing a URL does not authorize accepting a
replacement center. Binding, authentication and endpoint routing remain outside
[Remote Protocol v1](remote-protocol.md) and reconciliation.

## Local state

`workspace.remote_binding` stores version-1 JSON at
`.local/state/aikito/workspace-reconcile/binding.json`:

```json
{
  "version": 1,
  "endpoint": "https://example.invalid/v1/remote",
  "sync_id": "0123456789abcdef0123456789abcdef",
  "auth": {"type": "bearer_env", "env": "AIKITO_REMOTE_TOKEN"}
}
```

Only the environment variable name is saved, never its secret value. Binding is
local-only: it is excluded from resource snapshots, remote payloads and workspace
Git context. It is separate from `remote.json`, which describes a filesystem
resource center. Both the outer object and auth object reject unknown or missing
fields, duplicate JSON keys, unsupported versions and malformed values. Identity
uses the existing 32-character lowercase hexadecimal rule; environment names
match `[A-Z_][A-Z0-9_]*`.

State readers validate path entries and never follow symlinks or reparse points.
`load_remote_binding` is strictly read-only: it creates neither directories nor
lock files, and does not recover journals. Atomic replacement lets readers see a
complete old or new binding. Lifecycle writes use `WorkspaceWriterLock`, recover
local resource journals before attachment checks and use the existing private
state atomic writers and permission policy. Malformed state blocks creation and
removal; it is never silently replaced.

Binding is deliberately absent from journal `PathPolicy.states`. Adding it would
invalidate interrupted journals from earlier versions. Binding writes are
independent of resource transactions and leave their compatibility policy intact.

## Construction and identity

The internal `workspace.remote_factory` entry points are:

- `bind_remote(workspace, endpoint, auth, *, home=None, environ=None)` reads the
  real remote identity and persists the pin under the writer lock. An existing
  binding is never overwritten. Existing replica or pending identities must
  match, including after an explicit unbind.
- `open_bound_remote(workspace, *, environ=None)` loads the binding, resolves its
  credential and assembles `HTTPTransport`, `SerializedRemoteStore` and
  `BoundRemote`. Construction performs no network exchange or journal recovery.
- `verify_remote_binding(binding, *, environ=None)` reads and validates the pin.
- `remove_remote_binding(workspace, *, home=None)` explicitly removes only the
  binding. Unresolved pending blocks removal. Replica state, resources and remote
  contents are preserved. Removal does not authorize pairing a different center.

Future callers use this factory rather than assembling a bound transport stack
themselves. These remain internal modules; no public CLI or package API is added.

Each bound instance verifies the pin before its first identity-bearing operation:
`fetch`, `commit` and `resolve_commit`. `recover` also verifies first. Verification is cached only
after a successful read. Every subsequent `read` still checks the returned
identity; failure invalidates the cache. Expected snapshot and recovery identities
must match the pin. A changed center raises `StoreIdentityMismatch` without
updating binding or sending the old recovery identity to the replacement center.
Backend snapshot and commit identity checks continue to apply after verification.
Local attachment validation performs no network exchange. If the verification
read reports `RecoveryRequired`, `recover` may run before the pin is verified:
it carries no center, client or request identity and only completes the
endpoint's own interrupted transaction. The pin is verified immediately after,
before `recover` returns or any identity-bearing operation is sent. Thus
`resolve_commit` reporting `RecoveryRequired` can still be recovered by pending
recovery without exposing its recovery identity to an unverified center.

## Credentials and access failures

The sole provider is `bearer_env`. The factory resolves a nonempty token consisting
of visible ASCII characters without whitespace and explicitly provides the
`Authorization: Bearer ...` value to HTTP. Transport never reads environment
credentials. Credentials are excluded from object representations and exception
messages. Authentication does not change protocol bytes or request digests.

Authenticated endpoints require HTTPS with normal certificate and hostname
verification. Only exact `127.0.0.1`, `::1` and `localhost` hosts may use HTTP for
local testing. Embedded URL credentials and fragments are rejected. Credentials
must never appear in query parameters; recognizable token, secret, password,
credential, auth and API-key names are rejected. This name check is a guard, not
permission to put secrets under another query name. Redirects are not followed.

The endpoint contract requires HTTP 401 and 403 to be returned **before reading
or dispatching a Remote Protocol operation**. HTTP closes these responses without
reading their bodies and raises `TransportRejected`. The adapter converts this
to `RemoteAccessDenied`, a local `StoreError`, including for commit. Neither error
is part of the protocol error-code mapping. 401 means authentication is required
or invalid; 403 means authorization is denied. Other statuses, redirects, response
loss and timeouts remain conservative transport failures: an uncertain commit
raises `CommitOutcomeUnknown`.

## Pending recovery and verification

Binding, credential and access failures retain the exact pending record. Even a
contracted 401/403 does not clear it: intermediary rejection must not erase
recovery evidence. Restarting with the same pin and valid credentials resolves
the original request before reconciliation plans another mutation. An identity
mismatch stops before lookup or backend recovery. Only existing local completion
or `SnapshotExpired` paths clear pending.

Unit tests cover strict state validation, safe paths, identity-first calls and
failure classification. The test HTTP bridge counts protocol dispatches and
checks credentials before reading request bodies. An external implementation's
black-box auth contract checks access rejection, absent receipt and unchanged
revision, without requiring internal counters or a control endpoint.

`AIKITO_REMOTE_TEST_SERVER_CMD` supports `{root}` and `{token}` argv placeholders.
The latter supplies an ephemeral **test** token; production credentials must not
be placed in command arguments. To verify an auth-aware external implementation:

```sh
PYTHONPATH=src AIKITO_REMOTE_TEST_SERVER_CMD='python tests/http_remote_server.py --root {root} --token {token}' \
  python -m pytest tests/test_workspace_http_external.py -k external_auth_contract
```

Three-platform smoke jobs run `tests/workspace_remote_binding_smoke.py` with real
HTTP sockets, assert binding and resource files, check that binding excludes the
token, and verify pending retention, no unauthorized revision advance and restart
recovery after lost responses. Existing recovery contracts and byte-exact protocol
vectors continue to apply unchanged.

## Independent server acceptance

The multi-store test assembly keeps URL `store_id` separate from the pinned
protocol `sync_id`. A service restart reuses its listening address and store
metadata, so a bound client keeps both pins. The external server smoke exercises
real reconciliation, a lost accepted response, pending retention, service restart,
and durable receipt resolution through `open_bound_remote`. It verifies that
binding contains only the credential source and that successful recovery clears
pending without a second publication. See the
[HTTP test assembly](http-transport.md#multi-store-and-restart-test-assembly).
