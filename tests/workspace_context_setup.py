"""Public, test-only fixtures for the next collection layout and scoped export."""

from dataclasses import replace
from types import SimpleNamespace

from aikito.workspace.encrypted_remote import (
    EncryptedVault,
    _b64,
    create_recovery_metadata,
    metadata_resource,
)
from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    TreeEntry,
    TreePayload,
    inspect_payload,
    tree_mode_fingerprint,
)
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_store import RemoteSnapshot
from aikito.workspace.remote_wire import _canonical
from aikito.workspace.resource_state import local_resource_for_id
from aikito.workspace.scoped_export import EXPORT_SUITE, _hpke

VAULT = "00112233445566778899aabbccddeeff"
KEY = bytes(range(32))
SYNC = "11223344556677889900aabbccddee11"
NOW = 2000000000
PRIVATE = bytes([31]) * 32
KEYS = SimpleNamespace(load=lambda vault_id: KEY if vault_id == VAULT else None)
COLLECTIONS = (
    "global/instructions",
    "global/memory",
    "skills",
    "subagents",
    "inbox",
    "project/demo/instructions",
    "project/demo/memory",
)


def contents():
    return [
        ("project:demo", MemberPayload()),
        ("global-instructions:AGENTS.md", FilePayload(b"Global rules\r\n")),
        ("global-memory:notes/decision.md", FilePayload("数据库决策\r\n".encode())),
        ("global-memory:notes/empty.md", FilePayload(b"")),
        (
            "skill:demo",
            TreePayload(
                (
                    TreeEntry("SKILL.md", b"# Demo\nSee references/guide.md\n"),
                    TreeEntry("references/guide.md", b"Reference content\n"),
                    TreeEntry("scripts/run.py", b"print('demo')\n", True),
                    TreeEntry("assets/blob.bin", bytes(range(256))),
                )
            ),
        ),
        ("agent:codex", FilePayload(b"[agents.codex]\n")),
        ("agent:claude", FilePayload(b"[agents.claude]\n")),
        (
            "subagent:review",
            FilePayload(
                b'---\ndescription: "Review changes"\nagents: ["codex"]\n---\nReview changes.\n'
            ),
        ),
        (
            "subagent:verify",
            FilePayload(
                b'---\ndescription: "Verify changes"\nagents: ["claude"]\n---\nVerify changes.\n'
            ),
        ),
        ("inbox:context.md", FilePayload(b"Inbox note\n")),
        ("project-instructions:demo", FilePayload(b"Project rules\n")),
        ("project-memory:demo/notes/decision.md", FilePayload(b"Project decision\n")),
    ]


class Reader:
    def __init__(self, resources=None):
        self.calls = []
        self.descriptors, self.payloads, self.original = {}, {}, {}
        self.revision = 1
        self.vault = EncryptedVault(VAULT, KEY)
        for identity, payload in contents() if resources is None else resources:
            resource = local_resource_for_id(
                identity, "" if isinstance(payload, MemberPayload) else "0" * 64
            )
            fingerprint, references = inspect_payload(resource, payload)
            resource = replace(
                resource,
                fingerprint=fingerprint,
                references=references,
                mode_fingerprint=tree_mode_fingerprint(payload)
                if isinstance(payload, TreePayload)
                else None,
            )
            opaque, descriptor, outer = self.vault.encrypt(resource, payload, SYNC)
            self.descriptors[opaque], self.payloads[opaque] = descriptor, outer
            self.original[opaque] = (resource, payload)
        header = create_recovery_metadata(KEY, VAULT, SYNC, bytes([42]) * 32)
        self.descriptors[FORMAT_RESOURCE_ID], self.payloads[FORMAT_RESOURCE_ID] = (
            metadata_resource(header)
        )

    def read(self):
        return RemoteSnapshot(SYNC, self.revision, self.descriptors)

    def fetch(self, expected, ids):
        assert expected == self.read()
        self.calls.append(tuple(ids))
        return {identity: self.payloads[identity] for identity in ids}


def inputs(collection="skills", permission="read", now=NOW):
    _, _, PrivateKey, _ = _hpke()
    public = PrivateKey.from_private_bytes(PRIVATE).public_key().public_bytes_raw()
    recipient = {
        "version": 1,
        "algorithm": EXPORT_SUITE,
        "key_id": "context-reader",
        "public_key": _b64(public),
    }
    auth = {
        "version": 2,
        "account_id": "context-account",
        "store_id": "alpha",
        "sync_id": SYNC,
        "vault_id": VAULT,
        "collection_id": collection,
        "recipient_key_id": recipient["key_id"],
        "permissions": ["read"] if permission == "read" else ["read", "write"],
        "grant_id": "context-grant",
        "nonce": _b64(bytes([37]) * 32),
        "expires_at": now + 3600,
    }
    return _canonical(recipient), _canonical(auth)
