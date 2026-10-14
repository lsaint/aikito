"""Real local key recovery commands and isolated encrypted round trips."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import (
    EncryptedVault,
    create_recovery_metadata,
    encode_recovery_code,
)
from aikito.workspace.payload import FilePayload
from aikito.workspace.resource_state import local_resource_for_id


def main(base: Path):
    base.mkdir(parents=True, exist_ok=True)
    workspace, home, config = base / "workspace", base / "home", base / "config"
    home.mkdir()
    fixture = json.loads(
        (
            Path(__file__).parent / "fixtures/remote_protocol_vectors/encrypted-v1.json"
        ).read_text()
    )
    assert fixture["test_only"] is True
    inputs = fixture["inputs"]
    vrk = bytes.fromhex(inputs["vrk_hex"])
    code = bytes.fromhex(inputs["recovery_code_hex"])
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
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
    header = base / "header.json"
    header.write_bytes(
        create_recovery_metadata(vrk, inputs["vault_id"], inputs["sync_id"], code)
    )
    args = [
        "remote",
        "key",
        "recover",
        "--header",
        str(header),
        "--sync-id",
        inputs["sync_id"],
        "--workspace",
        str(workspace),
        "--recovery-fd",
        "0",
    ]
    result = run(args, encode_recovery_code(code))
    assert result.returncode == 0, result.stderr
    assert (
        code.hex() not in result.stdout + result.stderr
        and vrk.hex() not in result.stdout + result.stderr
    )
    store = DeviceKeyStore(config / "aikito/encrypted-remote/keys", workspace=workspace)
    assert store.load(inputs["vault_id"]) == vrk
    keyfile = store.path(inputs["vault_id"])
    before = keyfile.read_bytes()
    result = run(args, encode_recovery_code(bytes(32)))
    assert result.returncode != 0 and "recovery_key_invalid" in result.stderr
    assert keyfile.read_bytes() == before
    path = run(
        [
            "remote",
            "key",
            "path",
            "--vault-id",
            inputs["vault_id"],
            "--workspace",
            str(workspace),
        ]
    )
    assert path.returncode == 0 and Path(path.stdout.strip()) == keyfile
    payload = FilePayload(bytes.fromhex(inputs["file_data_hex"]))
    resource = local_resource_for_id(
        fixture["inner"]["logical_id"], fixture["inner"]["fingerprint"]
    )
    vault = EncryptedVault(inputs["vault_id"], store.load(inputs["vault_id"]))
    encrypted = vault.encrypt(resource, payload, inputs["sync_id"])
    assert vault.decrypt(*encrypted, inputs["sync_id"], "project/demo/memory") == (
        resource,
        payload,
    )
    assert not list(workspace.rglob("*.key"))
    assert not (
        workspace / ".local/state/aikito/workspace-reconcile/pending.json"
    ).exists()
    (base / "checked.txt").write_text(
        "Local recovery and encrypted round trips passed\n"
    )
    print("[SUCCESS] Local recovery and encrypted round trips passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
