"""Real CLI context exports preserve all remote and local workspace contents."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from http_remote_server import HTTPRemoteServer
from workspace_context_setup import (
    COLLECTIONS,
    KEY,
    PRIVATE,
    SYNC,
    VAULT,
    Reader,
    inputs,
)

from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)
from aikito.workspace.scoped_export import _hpke, _info, _unb64, open_scoped_read


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
        "AIKITO_CONTEXT_TEST_TOKEN": "context-smoke-only-token",
    }

    def command(args):
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=env,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
        return result

    initialized = command(["init", "workspace", str(workspace)])
    assert initialized.returncode == 0, initialized.stderr
    reader = Reader()
    recipient_path, auth_path = base / "recipient.json", base / "authorization.json"
    with (
        patch.dict(os.environ, env),
        HTTPRemoteServer(reader, token=env["AIKITO_CONTEXT_TEST_TOKEN"]) as server,
    ):
        save_remote_binding(
            workspace,
            RemoteBinding(
                1,
                server.url,
                SYNC,
                RemoteAuth("bearer_env", "AIKITO_CONTEXT_TEST_TOKEN"),
            ),
            home=home,
        )
        DeviceKeyStore(home=home, workspace=workspace).save(VAULT, KEY)
        before = reader.read()
        local = {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        for collection in COLLECTIONS:
            name = collection.replace("/", "-")
            for permission in ("read", "write"):
                recipient, auth = inputs(collection, permission, int(time.time()))
                recipient_path.write_bytes(recipient)
                auth_path.write_bytes(auth)
                output = base / f"{name}-{permission}.json"
                args = [
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
                result = command(args)
                assert result.returncode == 0, (collection, permission, result.stderr)
                packet = json.loads(output.read_bytes())
                assert (
                    packet["version"] == 2
                    and packet["context"]["collection_id"] == collection
                )
                if permission == "read":
                    opened = open_scoped_read(
                        output.read_bytes(), PRIVATE, authorization=auth
                    )
                    tag = packet["context"]["collection_tag"]
                    for identity in reader.original:
                        if identity.startswith(f"e1:{tag}:"):
                            assert (
                                opened.decrypt(
                                    identity,
                                    reader.descriptors[identity],
                                    reader.payloads[identity],
                                )
                                == reader.original[identity]
                            )
                else:
                    suite, _, PrivateKey, _ = _hpke()
                    material = json.loads(
                        suite.decrypt(
                            _unb64(packet["ciphertext"]),
                            PrivateKey.from_private_bytes(PRIVATE),
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
                # Existing exports must never be overwritten, even by the same request.
                saved, requests = output.read_bytes(), server.request_count
                repeated = command(args)
                assert repeated.returncode == 1 and output.read_bytes() == saved
                assert server.request_count == requests
        for collection in (
            "private",
            "agents",
            "workspace/config",
            "workspace/skills",
            "project/demo/config",
        ):
            recipient, auth = inputs(collection, now=int(time.time()))
            recipient_path.write_bytes(recipient)
            auth_path.write_bytes(auth)
            output = base / "denied.json"
            requests = server.request_count
            denied = command(
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
                ]
            )
            assert denied.returncode == 1 and not output.exists()
            assert server.request_count == requests
        assert reader.read() == before
        assert local == {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        assert not (workspace / ".local/state/aikito/encrypted-discovery.json").exists()
    (base / "checked.txt").write_text(
        "All context collections exported without mutation.\n"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
