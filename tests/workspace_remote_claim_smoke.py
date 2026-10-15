"""Real CLI ownership proof with an isolated HTTPS service and public test CA."""

import json
import os
import ssl
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
)


def main(base):
    base.mkdir(parents=True)
    home, config, workspace = (base / name for name in ("home", "config", "workspace"))
    home.mkdir()
    fixture = Path(__file__).parent / "fixtures/https_claim"
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "SSL_CERT_FILE": str(fixture / "certificate.pem"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
        "AIKITO_CLAIM_TEST_TOKEN": "public-claim-test-token",
    }
    body = {
        "version": 1,
        "challenge_id": "a" * 64,
        "account_id": "account-1",
        "store_id": "alpha",
        "sync_id": "00112233445566778899aabbccddeeff",
        "expires_at": int(time.time()) + 240,
    }
    calls = []
    mode = ["accept"]

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            received = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert self.path == "/v1/stores/alpha/claim"
            assert self.headers["Authorization"] == "Bearer public-claim-test-token"
            assert received == {
                "challenge_id": body["challenge_id"],
                "sync_id": body["sync_id"],
                "expected_account_id": "account-1",
            }
            calls.append(mode[0])
            if mode[0] == "redirect":
                self.send_response(307)
                self.send_header("Location", "https://attacker.invalid/collect")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            record = {
                key: body[key] for key in ("account_id", "store_id", "sync_id")
            } | {"created_at": int(time.time())}
            raw = json.dumps(record).encode()
            self.send_response(201)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixture / "certificate.pem", fixture / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def run(args):
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=env,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert env["AIKITO_CLAIM_TEST_TOKEN"] not in result.stdout + result.stderr
        return result

    try:
        assert run(["init", "workspace", str(workspace)]).returncode == 0
        with patch.dict(os.environ, env):
            save_remote_binding(
                workspace,
                RemoteBinding(
                    1,
                    f"https://127.0.0.1:{server.server_port}/v1/stores/alpha/remote",
                    body["sync_id"],
                    RemoteAuth("bearer_env", "AIKITO_CLAIM_TEST_TOKEN"),
                ),
                home=home,
            )
        before = {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        challenge = base / "challenge.json"
        challenge.write_text(json.dumps(body))
        args = [
            "remote",
            "claim",
            "--workspace",
            str(workspace),
            "--challenge",
            str(challenge),
            "--account",
            "account-1",
        ]
        success = run(args)
        assert success.returncode == 0, success.stderr
        (base / "claim.out").write_text(success.stdout)
        assert calls == ["accept"]
        body["account_id"] = "attacker"
        challenge.write_text(json.dumps(body))
        rejected = run(args)
        assert rejected.returncode == 1 and calls == ["accept"]
        body["account_id"] = "account-1"
        challenge.write_text(json.dumps(body))
        mode[0] = "redirect"
        redirected = run(args)
        assert redirected.returncode == 1 and calls == ["accept", "redirect"]
        after = {
            p.relative_to(workspace): p.read_bytes()
            for p in workspace.rglob("*")
            if p.is_file()
        }
        assert before == after
        (base / "checked.txt").write_text(
            "HTTPS claim, explicit account, no redirects or workspace writes verified\n"
        )
        print("[SUCCESS] HTTPS ownership proof CLI passed")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
