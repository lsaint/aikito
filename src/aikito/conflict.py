"""Git conflict marker detection and validation for Aikito.

Provides conflict marker scanning for Markdown and TOML files,
distinguishing between blocking conflict groups and isolated markers.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from .compat import safe_relative_path

# Regex matching any conflict marker line at column 0.
_START_RE = re.compile(r"^<{7}([ \t].*)?$")
_SEP_RE = re.compile(r"^(={7}|\|{7})([ \t].*)?$")
_END_RE = re.compile(r"^>{7}([ \t].*)?$")
_ANY_CONFLICT_RE = re.compile(r"^(<{7}|={7}|>{7}|\|{7})([ \t].*)?$")

CONFLICT_FIX_HINT = "Resolve the Git conflict manually, then run 'git add' and commit"


def has_any_conflict_markers(text: str) -> bool:
    """Return True if text contains any Git conflict marker line."""
    return any(_ANY_CONFLICT_RE.match(line) for line in text.splitlines())


def find_conflict_marker_lines(path: Path) -> tuple[list[int], list[int]]:
    """Scan a file for conflict marker lines.

    Returns:
        (blocking_lines, isolated_lines)

    Rules:
        - TOML files: any conflict marker line is syntax-breaking and blocking.
        - Markdown/text files: only grouped conflict blocks
          (<<<<<<< ... ======= ... >>>>>>>) are blocking.
          Isolated markers (such as an isolated '=======' heading underline)
          are non-blocking.
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ([], [])

    if path.suffix == ".toml":
        blocking: list[int] = []
        for lineno, line in enumerate(lines, start=1):
            if _ANY_CONFLICT_RE.match(line):
                blocking.append(lineno)
        return (blocking, [])

    # For Markdown and other text files: identify conflict groups
    blocking_set: set[int] = set()
    all_markers: set[int] = set()

    pending_start: int | None = None
    pending_seps: list[int] = []

    for lineno, line in enumerate(lines, start=1):
        if _START_RE.match(line):
            all_markers.add(lineno)
            pending_start = lineno
            pending_seps = []
        elif _SEP_RE.match(line):
            all_markers.add(lineno)
            if pending_start is not None:
                pending_seps.append(lineno)
        elif _END_RE.match(line):
            all_markers.add(lineno)
            if pending_start is not None and pending_seps:
                blocking_set.add(pending_start)
                blocking_set.update(pending_seps)
                blocking_set.add(lineno)
            pending_start = None
            pending_seps = []

    isolated_set = all_markers - blocking_set
    return (sorted(blocking_set), sorted(isolated_set))


def collect_resource_conflicts(
    paths: Sequence[Path],
    home: Path,
) -> list[str]:
    """Scan files and directories for blocking conflict markers.

    Returns:
        List of formatted error messages:
        '{rel_path}:{lineno}: Git conflict marker detected'
    """
    errors: list[str] = []
    seen_files: set[Path] = set()

    for path in paths:
        if not path.exists():
            continue
        if path.is_file():
            candidates = [path]
        elif path.is_dir():
            candidates = [
                p for p in sorted(path.rglob("*")) if p.is_file() and not p.is_symlink()
            ]
        else:
            continue

        for file_path in candidates:
            resolved = file_path.resolve(strict=False)
            if resolved in seen_files:
                continue
            seen_files.add(resolved)

            blocking_lines, _ = find_conflict_marker_lines(file_path)
            if blocking_lines:
                rel = safe_relative_path(file_path, home)
                for lineno in blocking_lines:
                    errors.append(f"{rel}:{lineno}: Git conflict marker detected")

    return errors
