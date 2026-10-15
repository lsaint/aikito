"""Real CLI HTTPS recipient discovery, TOFU changes and trust failures."""

import base64
import json
import os
import ssl
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def canonical(document):
    return json.dumps(document, sort_keys=True, separators=(",", ":")).encode()


def main(base):
    base.mkdir(parents=True)
    home, config, workspace = (base / name for name in ("home", "config", "workspace"))
    home.mkdir()
    fixtures = Path(__file__).parent / "fixtures/https_claim"
    document = {
        "version": 1,
        "algorithm": "hpke-x25519-hkdf-sha256-a256gcm",
        "key_id": "fixture-1",
        "public_key": base64.b64encode(bytes(range(32))).decode(),
    }
    mode = ["success"]
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            assert self.path == "/v1/keys/recipient"
            assert not self.headers.get("Authorization") and not self.headers.get(
                "Cookie"
            )
            requests.append(self.path)
            if mode[0] == "redirect":
                self.send_response(307)
                self.send_header("Location", "https://other.invalid/v1/keys/recipient")
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            raw = canonical(document)
            if mode[0] == "duplicate":
                raw = b'{"version":1,' + raw[1:]
            if mode[0] == "oversize":
                raw = b"x" * 4097
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.load_cert_chain(fixtures / "certificate.pem", fixtures / "key.pem")
    server.socket = context.wrap_socket(server.socket, server_side=True)
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
        "SSL_CERT_FILE": str(fixtures / "certificate.pem"),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }

    def run(success, *args, environment=None):
        command = subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=environment or env,
            capture_output=True,
            timeout=15,
        )
        assert (command.returncode == 0) == success, command.stdout + command.stderr
        return command

    def fetch(name, *extra, success=True, environment=None):
        return run(
            success,
            "remote",
            "key",
            "fetch",
            "--workspace",
            str(workspace),
            "--origin",
            origin,
            "--output",
            str(base / name),
            *extra,
            environment=environment,
        )

    try:
        run(True, "init", "workspace", str(workspace))
        fetch("recipient.json")
        assert (base / "recipient.json").read_bytes() == canonical(document)
        pin = next((config / "aikito/remote-recipients").glob("*.json"))
        first = pin.read_bytes()
        fetch("same.json")
        assert pin.read_bytes() == first
        document["key_id"] = "fixture-2"
        rejected = fetch("changed.json", success=False)
        assert (
            b"recipient_key_changed" in rejected.stderr
            and not (base / "changed.json").exists()
        )
        assert pin.read_bytes() == first
        fetch("accepted.json", "--accept-key-change")
        assert pin.read_bytes() != first
        accepted = pin.read_bytes()
        for failure in ("redirect", "duplicate", "oversize"):
            mode[0] = failure
            before = len(requests)
            fetch(failure + ".json", success=False)
            assert len(requests) == before + 1
            assert not (base / (failure + ".json")).exists()
            assert pin.read_bytes() == accepted
        mode[0] = "success"
        untrusted = dict(env)
        untrusted.pop("SSL_CERT_FILE", None)
        untrusted.pop("SSL_CERT_DIR", None)
        fetch("untrusted.json", success=False, environment=untrusted)
        assert not (base / "untrusted.json").exists() and pin.read_bytes() == accepted
        fetch("recipient.json", success=False)
        assert not list(workspace.rglob("*recipient*"))
        assert not list((config / "aikito/remote-recipients").glob(".recipient-pin-*"))
        (base / "checked.txt").write_text("HTTPS recipient trust lifecycle verified\n")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    print("[SUCCESS] HTTPS recipient discovery and trust pinning passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
