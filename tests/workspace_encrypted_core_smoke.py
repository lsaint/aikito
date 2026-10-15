"""A clean core interpreter needs no encrypted dependency for existing CLI."""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path


def main(base: Path):
    assert importlib.util.find_spec("cryptography") is None
    base.mkdir(parents=True)
    home, config, workspace = base / "home", base / "config", base / "workspace"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "USERPROFILE": str(home),
        "XDG_CONFIG_HOME": str(config),
        "APPDATA": str(config),
        "AIKITO_DIR": str(workspace),
        "PYTHONPATH": str(Path(__file__).resolve().parents[1] / "src"),
    }

    def run(args):
        return subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    assert run(["--help"]).returncode == 0
    assert run(["init", "workspace", str(workspace)]).returncode == 0
    assert run(["remote", "key", "path"]).returncode == 0
    result = run(
        [
            "remote",
            "key",
            "recover",
            "--header",
            "missing.json",
            "--sync-id",
            "store",
            "--workspace",
            str(workspace),
            "--recovery-fd",
            "0",
            "--file-storage",
        ]
    )
    assert result.returncode != 0
    assert (
        "encrypted_dependency_missing" in result.stderr
        and "aikito[encrypted]" in result.stderr
    )
    stored = run(
        [
            "remote",
            "key",
            "storage",
            "--workspace",
            str(workspace),
            "--backend",
            "native",
        ]
    )
    assert stored.returncode != 0 and "encrypted_dependency_missing" in stored.stderr
    inspect = run(["remote", "key", "inspect", "--workspace", str(workspace)])
    assert inspect.returncode != 0 and "encrypted_dependency_missing" in inspect.stderr
    exported = run(
        [
            "remote",
            "key",
            "export",
            "--workspace",
            str(workspace),
            "--collection",
            "project/demo/memory",
            "--recipient",
            "missing-recipient.json",
            "--authorization",
            "missing-request.json",
            "--output",
            str(base / "never-written.json"),
        ]
    )
    assert (
        exported.returncode != 0 and "encrypted_dependency_missing" in exported.stderr
    )
    assert not (base / "never-written.json").exists()
    assert not (workspace / ".local/state/aikito/encrypted-discovery.json").exists()
    assert not (config / "aikito/encrypted-remote/keys").exists()
    (base / "checked.txt").write_text("Core CLI works without encrypted extra\n")
    print("[SUCCESS] Core CLI works without encrypted extra")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
