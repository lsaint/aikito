"""Persist logical executable flags when the host cannot represent them."""

from __future__ import annotations

import hashlib
import json
import os
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


def _git_executable_paths(directory: Path) -> frozenset[str]:
    """Read executable files under directory from the enclosing Git index."""
    env = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    try:
        result = subprocess.run(
            ["git", "ls-files", "-s", "-z", "--", "."],
            cwd=directory,
            env=env,
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return frozenset()
    if result.returncode != 0:
        return frozenset()
    executable = set()
    for record in result.stdout.split(b"\0"):
        info, _, name = record.partition(b"\t")
        if info.startswith(b"100755 ") and name:
            executable.add(os.fsdecode(name))
    return frozenset(executable)


def logical_executable_metadata(directory: Path) -> frozenset[str]:
    """Return logical executable paths on hosts without native execute bits.

    The reserved metadata file is authoritative. A fresh Git checkout lacks it,
    so fall back to the Git index modes instead of treating every file as
    nonexecutable.
    """
    path = directory / SKILL_EXECUTABLE_METADATA
    if not os.path.lexists(path):
        return _git_executable_paths(directory)
    return read_executable_metadata(path)
