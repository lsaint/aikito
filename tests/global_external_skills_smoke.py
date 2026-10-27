"""Verify external global skills coexist with the real CLI in both install orders."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def exercise(root: Path, external_first: bool) -> None:
    home, workspace = root / "home", root / "workspace"
    home.mkdir(parents=True)
    runtime = home / ".agents" / "skills"
    external = home / ".config" / "vendor" / "skill"
    external.mkdir(parents=True)
    (external / "SKILL.md").write_text("external\n", encoding="utf-8")
    environment = dict(
        os.environ,
        HOME=str(home),
        USERPROFILE=str(home),
        AIKITO_DIR=str(workspace),
        AIKITO_NO_UPDATE_NOTIFIER="1",
        XDG_CONFIG_HOME=str(home / ".config"),
        XDG_STATE_HOME=str(home / ".local" / "state"),
        XDG_CACHE_HOME=str(home / ".cache"),
        APPDATA=str(home / "AppData" / "Roaming"),
        LOCALAPPDATA=str(home / "AppData" / "Local"),
    )

    def cli(*args: str) -> None:
        result = subprocess.run(
            [sys.executable, "-m", "aikito", *args],
            env=environment,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, (args, result.stdout, result.stderr)

    def install_external() -> None:
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "vendor-skill").symlink_to(external, target_is_directory=True)

    if external_first:
        install_external()
    cli("init", "workspace", str(workspace))
    cli("adopt")
    if not external_first:
        cli("sync", "global")
        install_external()
    assert not (workspace / "skills" / "vendor-skill").exists()
    cli("sync", "global", "--dry-run")
    cli("sync", "global")
    assert (runtime / "vendor-skill").is_symlink()
    assert (runtime / "vendor-skill" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == "external\n"
    assert (runtime / "aikito").is_symlink()
    assert not (workspace / "skills" / "vendor-skill").exists()


if __name__ == "__main__":
    base = Path(sys.argv[1])
    exercise(base / "external-first", True)
    exercise(base / "aikito-first", False)
