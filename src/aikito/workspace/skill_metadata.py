"""Persist logical executable flags when the host cannot represent them."""

from __future__ import annotations

import hashlib
import json
import stat
import subprocess
from pathlib import Path

from ..compat import is_reparse_point
from ..skill_artifacts import SKILL_EXECUTABLE_METADATA


def executable_fingerprint(paths) -> str:
    """Hash only executable relative paths; all other files are implicit false."""
    return hashlib.sha256(
        json.dumps(sorted(paths), ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate skill executable metadata field")
        result[key] = value
    return result


def read_executable_metadata(path: Path) -> frozenset[str]:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return frozenset()
    if not stat.S_ISREG(mode) or is_reparse_point(path):
        raise ValueError("Unsafe skill executable metadata")
    raw = json.loads(path.read_bytes(), object_pairs_hook=_unique_fields)
    if (
        not isinstance(raw, dict)
        or set(raw) != {"version", "executable"}
        or type(raw["version"]) is not int
        or raw["version"] != 1
        or type(raw["executable"]) is not list
    ):
        raise ValueError("Invalid skill executable metadata")
    for name in raw["executable"]:
        if (
            type(name) is not str
            or not name
            or any(
                part in {"", ".", "..", SKILL_EXECUTABLE_METADATA}
                for part in name.split("/")
            )
            or any(ord(char) < 32 or char in '\\<>:"|?*' for char in name)
        ):
            raise ValueError("Unsafe skill executable metadata path")
    if len(raw["executable"]) != len(set(raw["executable"])):
        raise ValueError("Duplicate skill executable metadata path")
    return frozenset(raw["executable"])


def write_executable_metadata(path: Path, executable) -> None:
    path.write_text(
        json.dumps({"version": 1, "executable": sorted(executable)}, sort_keys=True)
        + "\n",
        encoding="utf-8",
        newline="\n",
    )


def git_executable_metadata(directory: Path) -> frozenset[str]:
    """Query git index for files with executable mode (100755) under directory."""
    try:
        res = subprocess.run(
            ["git", "ls-files", "-s", "."],
            capture_output=True,
            text=True,
            cwd=str(directory),
            check=False,
            timeout=3.0,
        )
        if res.returncode != 0:
            return frozenset()
        executables: list[str] = []
        for line in res.stdout.splitlines():
            parts = line.split("\t", 1)
            if len(parts) == 2 and parts[0].startswith("100755"):
                git_path = parts[1].strip().replace("\\", "/")
                executables.append(git_path)
        return frozenset(executables)
    except (subprocess.SubprocessError, OSError):
        return frozenset()
