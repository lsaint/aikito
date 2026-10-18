# Explicit resource collections and one-way migration

The client uses collection layout 2 and scoped-export protocol 2. The product
has not been publicly released; there is no parallel old-client runtime or
export downgrade path. The installed client has no retired-layout decoder,
layout selector or migration state. One-time local maintenance tooling retains
its own untracked dependencies.

## Current collection policy

| Resource kind | Collection |
| --- | --- |
| global-instructions | global/instructions |
| global-memory | global/memory |
| skill | skills |
| subagent | subagents |
| inbox | inbox |
| project-instructions | project/<name>/instructions |
| project-memory | project/<name>/memory |

MCP definitions, Agent definitions, workspace configuration, global skill
selection and project registration/configuration remain private. Invalid or
unknown identities cannot be delegated. A collection permission covers all its
current and future resources.

Global Memory has identity `global-memory:<member>` and physical path
`memory/<member>`. Ordinary scanning, encrypted discovery and reconciliation
use that identity. Instructions, complete Skill trees, Subagents and Inbox have
their own independent collection keys. Authenticated discovery checkpoints use
version 3. The existing encrypted-v1 header, resource Envelope and key derivation
algorithm remain unchanged.

## Existing Vault data

Existing data must be converted before the new client resumes synchronization.
One-time maintenance tooling is local and untracked; it is not part of this
repository or the installed client. Back up existing data and pause all writers
before converting it. Preserve encrypted pending requests, resource references
and Replica/Base history; do not reset state to hide conflicts or data loss.
Development and tests do not migrate the user's real Vault.

## Scoped export

The independently trusted authorization, sealed packet and HPKE material must
all use version 2. Recipient public-key documents and resource Envelopes retain
their existing version 1. Unsupported export versions fail before Store access.

```sh
aikito remote key export --workspace /path/to/workspace \
  --collection skills --recipient recipient.json \
  --authorization request-v2.json --output skills-export.json --permission write
```

Context binds every authorization field, including collection_id, plus the
algorithm, key version, collection tag, recipient public-key hash, parent and
cross-collection reference bindings. Non-project collections have a null parent.
Project collections bind an authenticated private project identity. Bindings
contain exactly logical_id and resource_id, sorted uniquely by logical ID.

HPKE domains are `aikito.scoped-read-export-v2` and
`aikito.scoped-write-export-v2`. Read material contains only the content key;
explicit write material adds resource-ID and fingerprint keys. Neither exports
VRK, collection master, tag key or another collection's keys.

The exporter authenticates every selected resource and each referenced target
locally, without exporting private dependency contents or keys. All fetches
share existing budgets and the snapshot is rechecked before sealing. Empty
collections return collection_empty; a valid zero-byte file can prove its key.
Skill payloads retain reference files, scripts, binary bytes and executable
flags, without executing them. Oversized payloads or packets fail explicitly.

Shared public test-only vectors remain at
`tests/fixtures/scoped_export/context-v2.json`, covering read/write material and
complete portable payloads for all seven collections. They are consumed by an
independent cryptographic receiver. Do not regenerate them during routine tests.
