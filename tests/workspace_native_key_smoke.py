"""Independent CLI processes with a temporary keychain or current-user DPAPI."""

import base64
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

from http_remote_server import HTTPRemoteServer
from workspace_scoped_export_smoke import Reader, KEY, VAULT, SYNC

from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import encode_recovery_code
from aikito.workspace.native_keys import macos_command, _linux_collection
from aikito.workspace.remote_binding import (
    RemoteBinding,
    RemoteAuth,
    save_remote_binding,
)
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID


def main(base):
    base.mkdir(parents=True)
    home, config, workspace = (base / name for name in ("home", "config", "workspace"))
    home.mkdir()
    runtime = base / "runtime"
    runtime.mkdir(mode=0o700)
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "XDG_DATA_HOME": str(base / "data"),
        "XDG_RUNTIME_DIR": str(runtime),
        "AIKITO_DIR": str(workspace),
        "AIKITO_REMOTE_TOKEN": "native-key-smoke-only-token",
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    # Never reuse a desktop daemon's control socket in the private test session.
    env.pop("GNOME_KEYRING_CONTROL", None)
    keychain = base / "native-test.keychain-db"
    driver = Path(__file__).with_name("native_key_cli.py")
    password = "temporary-native-smoke-password"
    search = None

    def command(args, *, input=None):
        entry = (
            [sys.executable, str(driver)]
            if sys.platform == "darwin"
            else [sys.executable, "-m", "aikito"]
        )
        return subprocess.run(
            [*entry, *args],
            input=input,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    if sys.platform == "darwin":
        search = subprocess.run(
            ["/usr/bin/security", "list-keychains", "-d", "user"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        env["AIKITO_NATIVE_TEST_KEYCHAIN"] = str(keychain)
    try:
        if sys.platform == "darwin":
            macos_command(["create-keychain", "-p", password, str(keychain)])
            # Keep the user's search list unchanged; every test access is explicit.
            macos_command(["list-keychains", "-d", "user", "-s", *shlex.split(search)])
            macos_command(["unlock-keychain", "-p", password, str(keychain)])
        elif sys.platform.startswith("linux"):
            if "DBUS_SESSION_BUS_ADDRESS" not in env:
                raise AssertionError(
                    "Use an isolated Secret Service session for this smoke"
                )
            # This test runs inside a private D-Bus session and temporary HOME.
            initialized = subprocess.run(
                ["gnome-keyring-daemon", "--unlock", "--components=secrets"],
                input=password + "\n",
                env=env,
                capture_output=True,
                text=True,
                timeout=15,
            )
            assert initialized.returncode == 0, (
                "Test Secret Service initialization failed"
            )
        result = command(["init", "workspace", str(workspace)])
        assert result.returncode == 0, result.stderr
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
            store = DeviceKeyStore(home=home, workspace=workspace)
            keyfile = store.save(VAULT, KEY)
            original = backend.read()
            baseline = {
                p.relative_to(workspace): p.read_bytes()
                for p in workspace.rglob("*")
                if p.is_file()
            }
            args = [
                "remote",
                "key",
                "storage",
                "--workspace",
                str(workspace),
                "--backend",
            ]
            secured = command([*args, "native"])
            assert secured.returncode == 0, secured.stderr
            record = keyfile.read_bytes()
            assert record.startswith(b"{") and base64.b64encode(KEY) not in record
            assert KEY.hex() not in secured.stdout + secured.stderr
            # A separate unmodified CLI consumer loads the active backend.
            inspected = command(
                ["remote", "key", "inspect", "--workspace", str(workspace)]
            )
            assert inspected.returncode == 0, inspected.stderr
            # Inspection checkpoints are expected; storage itself never changes Base.
            changed = command([*args, "file"])
            assert changed.returncode == 0, changed.stderr
            assert keyfile.read_bytes() == base64.b64encode(KEY) + b"\n"
            assert command([*args, "native"]).returncode == 0
            protected = keyfile.read_bytes()
            if sys.platform == "darwin":
                macos_command(["lock-keychain", str(keychain)])
            elif sys.platform.startswith("linux"):
                collection = _linux_collection()
                subprocess.run(
                    [
                        "gdbus",
                        "call",
                        "--session",
                        "--dest",
                        "org.freedesktop.secrets",
                        "--object-path",
                        "/org/freedesktop/secrets",
                        "--method",
                        "org.freedesktop.Secret.Service.Lock",
                        "['" + collection + "']",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                )
            else:
                # DPAPI has no keychain lock; corrupt the blob, retaining its commitment.
                body = json.loads(protected)
                body["protected_key"] = base64.b64encode(b"invalid DPAPI blob").decode()
                keyfile.write_text(
                    json.dumps(body, sort_keys=True, separators=(",", ":"))
                )
                protected = keyfile.read_bytes()
            unavailable = command([*args, "file"])
            assert (
                unavailable.returncode == 1
                and "key_storage_unavailable" in unavailable.stderr
            )
            assert keyfile.read_bytes() == protected
            header = base / "header.json"
            header.write_bytes(backend.payloads[FORMAT_RESOURCE_ID].data)
            recovered = command(
                [
                    "remote",
                    "key",
                    "recover",
                    "--workspace",
                    str(workspace),
                    "--header",
                    str(header),
                    "--sync-id",
                    SYNC,
                    "--recovery-fd",
                    "0",
                    "--file-storage",
                ],
                input=encode_recovery_code(bytes([7]) * 32) + "\n",
            )
            assert recovered.returncode == 0, recovered.stderr
            assert keyfile.read_bytes() == base64.b64encode(KEY) + b"\n"
            assert backend.read() == original
            after = {
                p.relative_to(workspace): p.read_bytes()
                for p in workspace.rglob("*")
                if p.is_file()
                and p.relative_to(workspace).as_posix()
                != ".local/state/aikito/encrypted-discovery.json"
            }
            assert after == baseline
            assert not list(keyfile.parent.glob(".key-*"))
        (base / "checked.txt").write_text(
            "Native storage, independent readers and explicit file recovery verified\n"
        )
        print("[SUCCESS] Native storage and explicit recovery passed")
    finally:
        if sys.platform == "darwin" and keychain.exists():
            subprocess.run(
                ["/usr/bin/security", "delete-keychain", str(keychain)],
                capture_output=True,
                timeout=10,
                check=False,
            )
            macos_command(["list-keychains", "-d", "user", "-s", *shlex.split(search)])


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
