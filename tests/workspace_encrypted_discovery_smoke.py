"""Independent CLI processes resume authenticated discovery through real HTTP."""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

from http_remote_server import HTTPRemoteServer
from workspace_memory_remote import InMemoryRemote

from aikito.workspace.encrypted_remote import (
    EncryptedVault,
    create_recovery_metadata,
    metadata_resource,
    encode_recovery_code,
)
from aikito.workspace.encrypted_discovery import DISCOVERY_STATE
from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    ResourceMutation,
    inspect_payload,
)
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.resource_state import local_resource_for_id


def encrypted_mutation(vault, snapshot, logical_id, payload):
    resource = local_resource_for_id(
        logical_id, "" if isinstance(payload, MemberPayload) else "0" * 64
    )
    fingerprint, references = inspect_payload(resource, payload)
    resource = replace(resource, fingerprint=fingerprint, references=references)
    identity, descriptor, outer = vault.encrypt(resource, payload, snapshot.sync_id)
    return ResourceMutation(
        identity, snapshot.resources.get(identity), descriptor, outer
    )


def main(base: Path):
    base.mkdir(parents=True)
    home, workspace, config = base / "home", base / "workspace", base / "config"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "AIKITO_REMOTE_TOKEN": "discovery-smoke-token",
    }

    def run(args, input=None):
        return subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=env,
            input=input,
            capture_output=True,
            text=True,
            check=False,
        )

    result = run(["init", "workspace", str(workspace)])
    assert result.returncode == 0, result.stderr
    backend = InMemoryRemote("11223344556677889900aabbccddeeff")
    with HTTPRemoteServer(backend, token=env["AIKITO_REMOTE_TOKEN"]) as server:
        result = run(["remote", "bind", server.url, "--workspace", str(workspace)])
        assert result.returncode == 0, result.stderr
        # Test-only publication is external to the production sync engine.
        key, code = bytes(range(32)), bytes([42]) * 32
        vault = EncryptedVault("00112233445566778899aabbccddeeff", key)
        snapshot = backend.read()
        metadata = create_recovery_metadata(key, vault.vault_id, snapshot.sync_id, code)
        descriptor, payload = metadata_resource(metadata)
        mutations = [ResourceMutation(FORMAT_RESOURCE_ID, None, descriptor, payload)]
        for n in range(35):
            mutations.append(
                encrypted_mutation(
                    vault,
                    snapshot,
                    f"memory:notes/{n:02d}.md",
                    FilePayload(f"Private payload {n}\n".encode()),
                )
            )
        for project in ("demo", "other"):
            mutations.append(
                encrypted_mutation(
                    vault, snapshot, f"project:{project}", MemberPayload()
                )
            )
            for n in range(2):
                mutations.append(
                    encrypted_mutation(
                        vault,
                        snapshot,
                        f"project-memory:{project}/notes/{n}.md",
                        FilePayload("共享笔记\n".encode()),
                    )
                )
        backend.commit(
            build_commit_request(
                "setup-client", "setup-request", snapshot, tuple(mutations)
            )
        )
        before = backend.read()
        checkpoint = workspace / DISCOVERY_STATE
        args = ["remote", "key", "inspect", "--workspace", str(workspace)]
        locked = run(args)
        assert locked.returncode != 0 and "encrypted_store_locked" in locked.stderr
        assert not checkpoint.exists()
        header = base / "header.json"
        header.write_bytes(metadata)
        recovered = run(
            [
                "remote",
                "key",
                "recover",
                "--header",
                str(header),
                "--sync-id",
                snapshot.sync_id,
                "--workspace",
                str(workspace),
                "--recovery-fd",
                "0",
            ],
            encode_recovery_code(code),
        )
        assert recovered.returncode == 0, recovered.stderr
        results = [run(args) for _ in range(3)]
        assert all(r.returncode == 0 for r in results), [r.stderr for r in results]
        assert "verified=32, remaining=9" in results[0].stdout
        assert "verified=37, remaining=4" in results[1].stdout
        assert "verified=41, remaining=0" in results[2].stdout
        assert "complete:" in results[2].stdout
        same = run(args)
        assert same.returncode == 0 and "fetched=0" in same.stdout
        original = checkpoint.read_bytes()
        assert b"Private payload" not in original and key.hex().encode() not in original
        checkpoint.write_bytes(original + b" ")
        invalid = run(args)
        assert invalid.returncode != 0 and checkpoint.read_bytes() == original + b" "
        checkpoint.write_bytes(original)
        mutation = encrypted_mutation(
            vault,
            before,
            "memory:notes/00.md",
            FilePayload(b"Changed private payload\n"),
        )
        backend.commit(
            build_commit_request("update-client", "update-request", before, (mutation,))
        )
        updated = run(args)
        assert (
            updated.returncode == 0
            and "fetched=1" in updated.stdout
            and "remaining=0" in updated.stdout
        )
        after = backend.read()
        guarded = run(["remote", "sync", "--dry-run", "--workspace", str(workspace)])
        assert (
            guarded.returncode != 0 and "encrypted_store_unsupported" in guarded.stderr
        )
        assert backend.read() == after
        assert not (
            workspace / ".local/state/aikito/workspace-reconcile/pending.json"
        ).exists()
        assert not (workspace / "memory/notes/00.md").exists()
        output = "".join(
            r.stdout + r.stderr
            for r in [locked, *results, same, invalid, updated, guarded]
        )
        assert (
            key.hex() not in output
            and code.hex() not in output
            and "Private payload" not in output
        )
    (base / "checked.txt").write_text(
        "Bounded encrypted discovery and process restart passed\n"
    )
    print("[SUCCESS] Bounded encrypted discovery and process restart passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
