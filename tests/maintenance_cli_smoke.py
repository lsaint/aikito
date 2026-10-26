"""Exercise maintenance runner approval arguments through the real CLI."""

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

from aikito.templating import load_template


def run_smoke(base: Path) -> None:
    base = base.resolve()
    base.mkdir(parents=True, exist_ok=True)
    workspace = base / "workspace with spaces"
    capture = base / "runner.json"
    recorder = base / "record_runner.py"
    recorder.write_text(
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "Path(os.environ['MAINTENANCE_CAPTURE']).write_text(json.dumps({\n"
        "    'options': sys._xoptions, 'prompt': sys.argv[1], 'cwd': os.getcwd(),\n"
        "}), encoding='utf-8')\n",
        encoding="utf-8",
    )
    env = os.environ.copy()
    env["AIKITO_DIR"] = str(workspace)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[1] / "src")
    env["MAINTENANCE_CAPTURE"] = str(capture)
    env["XDG_CONFIG_HOME"] = str(base / "config")
    env["APPDATA"] = str(base / "config")
    env["XDG_STATE_HOME"] = str(base / "state")

    def cli(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            cwd=base,
            env=env,
            text=True,
            capture_output=True,
        )
        assert result.returncode == 0, result.stdout + result.stderr

    cli("init", "workspace", str(workspace))
    pi_definition = workspace / "agents" / "pi.toml"
    pi_definition.write_text(load_template("agents/pi.toml"), encoding="utf-8")
    pi_runner = tomllib.loads(pi_definition.read_text(encoding="utf-8"))["agents"][
        "pi"
    ]["runner"]
    assert pi_runner["command"] == ["pi", "{prompt}"]
    definition = workspace / "agents" / "smoke.toml"
    command = json.dumps([sys.executable, str(recorder), "{prompt}"])
    for args in (
        ["-X", "aikito_auto_approve", "-X", "aikito_workspace={workspace}"],
        [],
    ):
        definition.write_text(
            '[agents.smoke]\ndisplay_name = "Smoke"\n'
            f"[agents.smoke.runner]\ncommand = {command}\n"
            f"auto_approve_args = {json.dumps(args)}\n",
            encoding="utf-8",
        )
        cli("maintain", "memory", "global", "--agent", "smoke")
        data = json.loads(capture.read_text(encoding="utf-8"))
        assert Path(data["cwd"]).resolve() == workspace.resolve()
        assert "until the user confirms the proposal" in data["prompt"]
        assert "Do not push" in data["prompt"]
        assert (
            "Tool auto-approval does not count as proposal confirmation"
            in data["prompt"]
        )
        assert "interactive follow-up is unavailable" in data["prompt"]
        if args:
            assert data["options"]["aikito_auto_approve"] is True
            assert data["options"]["aikito_workspace"] == str(workspace.resolve())
            (base / "auto-approved.json").write_text(json.dumps(data), encoding="utf-8")
        else:
            assert "aikito_auto_approve" not in data["options"]
    (base / "checked.txt").write_text("ok\n", encoding="utf-8")


if __name__ == "__main__":
    run_smoke(Path(sys.argv[1]))
