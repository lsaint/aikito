"""Run the real console CLI in isolation and check its confirmation boundary."""

import http.client
import json
import os
import subprocess
import sys
import time
from pathlib import Path


def main(base):
    base.mkdir(parents=True)
    workspace = base / "workspace"
    home, config = base / "home", base / "config"
    home.mkdir()
    config.mkdir()
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "XDG_CONFIG_HOME": str(config), "APPDATA": str(config), "AIKITO_DIR": str(workspace), "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src")}
    initialized = subprocess.run([sys.executable, "-m", "aikito", "init", "workspace", str(workspace)], env=env, capture_output=True, timeout=60)
    assert initialized.returncode == 0, initialized.stderr.decode()
    private = workspace / ".local/state/aikito"
    binding = private / "workspace-reconcile/binding.json"
    binding.parent.mkdir(parents=True, exist_ok=True)
    binding.write_text(json.dumps({"version": 1, "endpoint": "https://offline.example/v1/stores/main/remote", "sync_id": "a" * 32, "auth": {"type": "bearer_env", "env": "TEST_TOKEN"}}))
    checked = int(time.time())
    history = {"version": 1, "records": [{"origin": "https://recipient.example", "account_id": "account-1", "collection_id": "project/demo/memory", "endpoint": "https://offline.example/v1/stores/main/remote", "sync_id": "a" * 32, "checked_at": checked, "grants": [{"grant_id": "c" * 64, "permissions": ["read"], "expires_at": checked + 86400, "status": "active"}]}]}
    (private / "console-access.json").write_text(json.dumps(history))
    process = subprocess.Popen([sys.executable, "-u", "-m", "aikito", "web", "--no-open", "--port", "0"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "Aikito Console"
        origin = process.stdout.readline().strip()
        port = int(origin.rsplit(":", 1)[1])
        def request(path, method="GET", headers=None, body=None):
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
            try:
                connection.request(method, path, body=body, headers=headers or {})
                response = connection.getresponse()
                return response.status, response.read()
            finally:
                connection.close()
        status, index = request("/")
        assert status == 200 and b"/memory-access.js" in index
        status, script = request("/memory-access.js")
        assert status == 200 and b"Confirm authorization" in script
        assert b'value="https://aikito.notsonan.com"' in script
        assert b"Step 2 of 2: Signed in. Access is not granted yet." in script
        assert b'event.data.type === "signing-in"' in script
        status, raw = request("/api/console-session")
        assert status == 200
        token = json.loads(raw)["token"]
        for headers in [{"Origin": "https://attacker.example", "X-Aikito-Console-Token": token}, {"Origin": origin, "X-Aikito-Console-Token": "wrong"}]:
            status, _ = request("/api/projects/demo/access", "POST", {**headers, "Content-Type": "application/json"}, b"{}")
            assert status == 403
        status, _ = request("/api/console-session", headers={"Host": "attacker.example"})
        assert status == 403
        status, raw = request("/api/projects/demo/access")
        assert status == 200 and json.loads(raw)["records"][0]["checked_at"] == checked
        process.terminate()
        process.communicate(timeout=10)
        process = subprocess.Popen([sys.executable, "-u", "-m", "aikito", "web", "--no-open", "--port", "0"], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        assert process.stdout.readline().strip() == "Aikito Console"
        origin = process.stdout.readline().strip()
        port = int(origin.rsplit(":", 1)[1])
        status, raw = request("/api/projects/demo/access")
        assert status == 200 and json.loads(raw)["records"][0]["grants"][0]["grant_id"] == "c" * 64
        (base / "access-history-checked.txt").write_text("Console CLI retained offline access history across process restarts\n")
        (base / "console.html").write_bytes(index)
        (base / "memory-access.js").write_bytes(script)
        (base / "login-progress-checked.txt").write_text("Console CLI served the explicit sign-in and authorization steps\n")
        (base / "default-origin-checked.txt").write_text("Default recipient origin verified through the console CLI\n")
        (base / "checked.txt").write_text("Real console CLI served Memory access and rejected foreign confirmation requests\n")
    finally:
        process.terminate()
        process.communicate(timeout=10)


if __name__ == "__main__":
    main(Path(sys.argv[1]))
