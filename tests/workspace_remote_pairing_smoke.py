"""Real CLI pairing over HTTPS; a test service simulates browser approval."""

import hashlib
import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from aikito.workspace.remote_protocol import RemoteProtocolHandler
from aikito.workspace.resource_state import REMOTE_BINDING_STATE
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace


def main(base):
    base.mkdir(parents=True)
    home = base / "home"
    home.mkdir()
    workspace = _workspace(base / "workspace")
    fixture = Path(__file__).parent / "fixtures/https_claim"
    backend = RemoteProtocolHandler(InMemoryRemote("11223344556677889900aabbccddeeff"))
    state = {}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            if self.path == "/v1/sync/pair":
                state["challenge"] = json.loads(raw)["challenge"]
                result = {
                    "request": "a" * 64,
                    "verification_uri": origin + "/sync/connect?request=" + "a" * 64,
                    "code": "aaaaaaaa",
                    "expires_in": 300,
                }
                status = 201
            elif self.path == "/v1/sync/poll":
                secret = json.loads(raw)["secret"]
                assert hashlib.sha256(secret.encode()).hexdigest() == state["challenge"]
                state["secret"] = secret
                result = {
                    "endpoint": origin + "/v1/stores/auto/remote",
                    "account_id": "alice",
                }
                status = 200
            else:
                assert self.path == "/v1/stores/auto/remote"
                assert self.headers["Authorization"] == "Bearer " + state["secret"]
                response = backend.handle(raw)
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(response)))
                self.end_headers()
                self.wfile.write(response)
                return
            response = json.dumps(result).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(response)))
            self.end_headers()
            self.wfile.write(response)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixture / "certificate.pem", fixture / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
    origin = f"https://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(base / "config"),
        "APPDATA": str(base / "config"),
        "AIKITO_DIR": str(workspace),
        "SSL_CERT_FILE": str(fixture / "certificate.pem"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }
    # Only opening an external browser is stubbed; CLI, HTTPS and persistence are real.
    runner = "from unittest.mock import patch; from aikito.cli import main; p=patch('webbrowser.open',return_value=True); p.start(); main()"
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-c",
                runner,
                "remote",
                "bind",
                origin,
                "--no-initialize",
            ],
            env=env,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=30,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        assert "Synchronization connected" in result.stdout
        assert state["secret"] not in result.stdout + result.stderr
        binding = json.loads((workspace / REMOTE_BINDING_STATE).read_text())
        assert binding["endpoint"] == origin + "/v1/stores/auto/remote"
        assert state["secret"] not in json.dumps(binding)
        (base / "bind.out").write_text(result.stdout)
        repeated = subprocess.run(
            [sys.executable, "-m", "aikito", "remote", "bind"],
            env=env,
            cwd=workspace,
            text=True,
            capture_output=True,
            timeout=15,
        )
        assert repeated.returncode == 0, repeated.stderr
        assert "Synchronization connected" in repeated.stdout
        (base / "checked.txt").write_text(
            "HTTPS pairing, private credentials, binding and repeat connection verified\n"
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
