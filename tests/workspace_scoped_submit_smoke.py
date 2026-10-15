"""Core CLI transport checks with a public TLS fixture; no real grants are activated."""

import base64
import hashlib
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)
from aikito.workspace.remote_wire import _canonical


def main(base):
    base.mkdir(parents=True)
    home, workspace, config = (base / part for part in ("home", "workspace", "config"))
    home.mkdir()
    public = bytes(range(32))
    recipient = {
        "version": 1,
        "algorithm": "hpke-x25519-hkdf-sha256-a256gcm",
        "key_id": "fixture-recipient",
        "public_key": base64.b64encode(public).decode(),
    }
    auth = {
        "version": 1,
        "account_id": "oidc:" + "a" * 64,
        "store_id": "alpha",
        "sync_id": "b" * 32,
        "vault_id": "00112233445566778899aabbccddeeff",
        "collection_id": "project/demo/memory",
        "recipient_key_id": recipient["key_id"],
        "permissions": ["read"],
        "grant_id": "c" * 64,
        "nonce": base64.b64encode(b"n" * 32).decode(),
        "expires_at": int(time.time()) + 240,
    }
    context = {k: v for k, v in auth.items() if k != "collection_id"}
    context.update(
        algorithm=recipient["algorithm"],
        key_version=1,
        collection_tag="1" * 32,
        parent_project_id="e1:" + "2" * 32 + ":" + "3" * 64,
        recipient_public_key_sha256=hashlib.sha256(public).hexdigest(),
    )
    # Transport-only opaque bytes: the production receiver's HPKE/sample checks
    # are exercised by the independent native-client -> Go integration test.
    packet = {
        "version": 1,
        "context": context,
        "ciphertext": base64.b64encode(b"x" * 48).decode(),
    }
    mode, posts = ["verified"], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/v1/keys/recipient" and not self.headers.get(
                "Authorization"
            )
            raw = _canonical(recipient)
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_POST(self):
            assert self.path == "/v1/stores/alpha/grants"
            assert self.headers["Authorization"] == "Bearer public-submit-test-token"
            assert not self.headers.get("Cookie") and not self.headers.get("Origin")
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert body == _canonical(packet)
            posts.append(body)
            if mode[0] == "redirect":
                self.send_response(307)
                self.send_header("Location", "https://other.invalid/grants")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            receipt = {
                k: auth[k]
                for k in (
                    "version",
                    "account_id",
                    "store_id",
                    "sync_id",
                    "collection_id",
                    "permissions",
                    "grant_id",
                    "expires_at",
                )
            }
            receipt["status"] = "active" if mode[0] == "active" else "verified"
            if mode[0] == "wrong-receipt":
                receipt["account_id"] = "attacker"
            raw = _canonical(receipt)
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    fixtures = Path(__file__).parent / "fixtures/https_claim"
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(fixtures / "certificate.pem", fixtures / "key.pem")
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"https://127.0.0.1:{server.server_address[1]}"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "SSL_CERT_FILE": str(fixtures / "certificate.pem"),
        "AIKITO_SUBMIT_TEST_TOKEN": "public-submit-test-token",
    }

    def run(success, *arguments):
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *arguments],
            env=env,
            capture_output=True,
            timeout=15,
        )
        assert (result.returncode == 0) == success, result.stdout + result.stderr
        assert b"public-submit-test-token" not in result.stdout + result.stderr
        return result

    try:
        run(True, "init", "workspace", str(workspace))
        # save_remote_binding uses the explicit home/config boundary.
        previous = {k: os.environ.get(k) for k in ("XDG_CONFIG_HOME", "APPDATA")}
        try:
            os.environ.update({"XDG_CONFIG_HOME": str(config), "APPDATA": str(config)})
            save_remote_binding(
                workspace,
                RemoteBinding(
                    1,
                    origin + "/v1/stores/alpha/remote",
                    auth["sync_id"],
                    RemoteAuth("bearer_env", "AIKITO_SUBMIT_TEST_TOKEN"),
                ),
                home=home,
            )
        finally:
            for key, value in previous.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
        run(
            True,
            "remote",
            "key",
            "fetch",
            "--workspace",
            str(workspace),
            "--origin",
            origin,
            "--output",
            str(base / "recipient.json"),
        )
        authorization, sealed = base / "request.json", base / "sealed.json"
        authorization.write_bytes(_canonical(auth))
        sealed.write_bytes(_canonical(packet))
        arguments = [
            "remote",
            "key",
            "submit",
            "--workspace",
            str(workspace),
            "--origin",
            origin,
            "--authorization",
            str(authorization),
            "--package",
            str(sealed),
            "--account",
            auth["account_id"],
            "--collection",
            auth["collection_id"],
        ]
        state = {
            str(path.relative_to(workspace)): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }
        run(True, *arguments)
        assert len(posts) == 1
        mode[0] = "active"
        run(True, *arguments)
        assert len(posts) == 2
        for failure in ("redirect", "wrong-receipt"):
            mode[0] = failure
            before = len(posts)
            run(False, *arguments)
            assert len(posts) == before + 1
        mode[0] = "verified"
        bad = list(arguments)
        bad[bad.index(auth["account_id"])] = "attacker"
        before = len(posts)
        run(False, *bad)
        assert len(posts) == before
        recipient["key_id"] = "changed-recipient"
        run(False, *arguments)
        assert len(posts) == before
        recipient["key_id"] = "fixture-recipient"
        mode[0] = "active"
        auth["permissions"] = ["read", "write"]
        context["permissions"] = ["read", "write"]
        authorization.write_bytes(_canonical(auth))
        sealed.write_bytes(_canonical(packet))
        before_write = len(posts)
        run(False, *arguments)
        assert len(posts) == before_write
        write_result = run(True, *arguments, "--permission", "write")
        assert b"Scoped write grant activated" in write_result.stdout
        assert len(posts) == before_write + 1
        (base / "write-request.json").write_bytes(_canonical(auth))
        (base / "write-sealed.json").write_bytes(_canonical(packet))
        (base / "write-checked.txt").write_text("Explicit write submission verified\n")
        assert state == {
            str(path.relative_to(workspace)): path.read_bytes()
            for path in workspace.rglob("*")
            if path.is_file()
        }
        (base / "checked.txt").write_text(
            "Pinned scoped submission transport verified\n"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("[SUCCESS] Scoped submission transport and independent intent passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
