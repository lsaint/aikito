"""Real CLI sealed exports over authenticated HTTP without Store or workspace writes."""

import base64
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from http_remote_server import HTTPRemoteServer

from workspace_encrypted_setup import unsupported_header, unchecked_header_resource
from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import (
    EncryptedVault,
    create_recovery_metadata,
    _b64,
    EncryptedRemoteError,
)
from aikito.workspace.payload import FilePayload, MemberPayload, inspect_payload
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_store import RemoteSnapshot
from aikito.workspace.remote_wire import _canonical
from aikito.workspace.resource_state import local_resource_for_id
from aikito.workspace.scoped_export import EXPORT_SUITE, _hpke, _info, open_scoped_read

KEY = bytes(range(32))
VAULT = "00112233445566778899aabbccddeeff"
SYNC = "11223344556677889900aabbccddee11"
COLLECTION = "project/demo/memory"


class Reader:
    def __init__(self):
        self.descriptors, self.payloads = {}, {}
        vault = EncryptedVault(VAULT, KEY)
        contents = [
            ("project:demo", MemberPayload()),
            ("project:other", MemberPayload()),
            ("memory:notes/private.md", FilePayload(b"global private payload\n")),
            (
                "project-memory:demo/notes/shared.md",
                FilePayload(b"shared demo payload\n"),
            ),
            (
                "project-memory:other/notes/shared.md",
                FilePayload(b"other project payload\n"),
            ),
        ]
        for logical, payload in contents:
            resource = local_resource_for_id(
                logical, "" if isinstance(payload, MemberPayload) else "0" * 64
            )
            fingerprint, references = inspect_payload(resource, payload)
            resource = replace(resource, fingerprint=fingerprint, references=references)
            identity, descriptor, outer = vault.encrypt(resource, payload, SYNC)
            self.descriptors[identity], self.payloads[identity] = descriptor, outer
        self.header()

    def header(self, migrating=False):
        encoded = (
            unsupported_header(KEY, VAULT, SYNC, bytes([7]) * 32)
            if migrating
            else create_recovery_metadata(KEY, VAULT, SYNC, bytes([7]) * 32)
        )
        self.descriptors[FORMAT_RESOURCE_ID], self.payloads[FORMAT_RESOURCE_ID] = (
            unchecked_header_resource(encoded)
        )

    def read(self):
        return RemoteSnapshot(SYNC, 1, self.descriptors)

    def fetch(self, expected, ids):
        assert expected == self.read()
        return {identity: self.payloads[identity] for identity in ids}


def main(base):
    base.mkdir(parents=True)
    home, workspace, config = (base / name for name in ("home", "workspace", "config"))
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "AIKITO_REMOTE_TOKEN": "scoped-export-smoke-only-token",
    }

    def command(args):
        return subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=60,
        )

    initialized = command(["init", "workspace", str(workspace)])
    assert initialized.returncode == 0, initialized.stderr
    _, _, PrivateKey, _ = _hpke()
    private = PrivateKey.generate().private_bytes_raw()
    public = PrivateKey.from_private_bytes(private).public_key().public_bytes_raw()
    recipient = {
        "version": 1,
        "key_id": "reader-1",
        "algorithm": EXPORT_SUITE,
        "public_key": _b64(public),
    }
    recipient_path, auth_path = base / "recipient.json", base / "authorization.json"
    recipient_path.write_bytes(_canonical(recipient))
    auth = {
        "version": 1,
        "account_id": "account-1",
        "store_id": "store-1",
        "sync_id": SYNC,
        "vault_id": VAULT,
        "collection_id": COLLECTION,
        "recipient_key_id": "reader-1",
        "permissions": ["read"],
        "grant_id": "grant-1",
        "nonce": _b64(bytes([37]) * 32),
        "expires_at": int(time.time()) + 3600,
    }
    auth_path.write_bytes(_canonical(auth))
    backend = Reader()
    with (
        patch.dict(os.environ, env),
        HTTPRemoteServer(backend, token=env["AIKITO_REMOTE_TOKEN"]) as server,
    ):
        save_remote_binding(
            workspace,
            RemoteBinding(
                1, server.url, SYNC, RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN")
            ),
            home=home,
        )
        keys = DeviceKeyStore(home=home, workspace=workspace)
        keys.save(VAULT, KEY)
        baseline = {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        before = backend.read()

        def export(collection=COLLECTION, name="scoped-read.json", permission="read"):
            output = base / name
            result = command(
                [
                    "remote",
                    "key",
                    "export",
                    "--workspace",
                    str(workspace),
                    "--collection",
                    collection,
                    "--recipient",
                    str(recipient_path),
                    "--authorization",
                    str(auth_path),
                    "--output",
                    str(output),
                    "--permission",
                    permission,
                ]
            )
            return result, output

        result, output = export()
        assert result.returncode == 0, result.stderr
        encoded = output.read_bytes()
        assert KEY.hex() not in result.stdout + result.stderr
        assert _b64(private) not in result.stdout + result.stderr
        opened = open_scoped_read(encoded, private, authorization=_canonical(auth))
        vault = EncryptedVault(VAULT, KEY)
        identity = vault.opaque_id("project-memory:demo/notes/shared.md")
        _, payload = opened.decrypt(
            identity, backend.descriptors[identity], backend.payloads[identity]
        )
        assert payload.data == b"shared demo payload\n"
        for logical in (
            "memory:notes/private.md",
            "project:demo",
            "project-memory:other/notes/shared.md",
        ):
            identity = vault.opaque_id(logical)
            try:
                opened.decrypt(
                    identity, backend.descriptors[identity], backend.payloads[identity]
                )
            except EncryptedRemoteError:
                pass
            else:
                raise AssertionError("Unauthorized content decrypted")
        denied, _ = export()
        assert denied.returncode == 1 and output.read_bytes() == encoded
        for collection in ("private", "project/other/memory"):
            requests = server.request_count
            denied, target = export(collection, "denied.json")
            assert denied.returncode == 1 and not target.exists()
            assert server.request_count == requests
        auth["permissions"] = ["read", "memory.write"]
        auth_path.write_bytes(_canonical(auth))
        requests = server.request_count
        denied, target = export(name="write-denied.json")
        assert (
            denied.returncode == 1
            and not target.exists()
            and server.request_count == requests
        )
        auth["permissions"] = ["read"]
        auth_path.write_bytes(_canonical(auth))
        auth["permissions"] = ["read", "write"]
        auth_path.write_bytes(_canonical(auth))
        requests = server.request_count
        denied, target = export(name="implicit-write.json")
        assert (
            denied.returncode == 1
            and not target.exists()
            and server.request_count == requests
        )
        written, write_path = export(name="scoped-write.json", permission="write")
        assert written.returncode == 0, written.stderr
        packet = json.loads(write_path.read_bytes())
        suite, _, PrivateKey, _ = _hpke()
        material = json.loads(
            suite.decrypt(
                base64.b64decode(packet["ciphertext"]),
                PrivateKey.from_private_bytes(private),
                info=_info(packet["context"], "write"),
            )
        )
        assert set(material) == {
            "version",
            "collection_id",
            "content_key",
            "resource_id_key",
            "fingerprint_key",
        }
        scoped = vault.collection(COLLECTION)
        assert material["content_key"] == _b64(scoped.content)
        assert material["resource_id_key"] == _b64(scoped.resource_id)
        assert material["fingerprint_key"] == _b64(scoped.fingerprint)
        (base / "write-checked.txt").write_text("Explicit write export verified\n")
        auth["permissions"] = ["read"]
        auth_path.write_bytes(_canonical(auth))
        assert backend.read() == before
        backend.header(migrating=True)
        denied, target = export(name="migration-denied.json")
        assert (
            denied.returncode == 1
            and "encrypted_content_invalid" in denied.stderr
            and not target.exists()
        )
        assert {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        } == baseline
        assert not (workspace / ".local/state/aikito/encrypted-discovery.json").exists()
        assert not list(base.glob(".scoped-export-*"))
    (base / "checked.txt").write_text(
        "Scoped read export, recipient isolation and rejection paths verified\n"
    )
    print("[SUCCESS] Scoped read export and isolation passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
