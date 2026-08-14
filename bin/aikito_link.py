"""
Shared symlink classification utilities for aikito.

Both aikito_status and aikito_doctor depend on this module so that they
produce consistent verdicts about the same filesystem state. Neither
command module imports the other.
"""

from enum import Enum
from pathlib import Path


class SymlinkVerdict(Enum):
    OK = "OK"  # is_symlink, target exists, resolves to expected
    DANGLING = "DANGLING"  # is_symlink, target does not exist
    WRONG_TARGET = "WRONG_TARGET"  # is_symlink, target exists but resolves elsewhere
    NOT_SYMLINK = "NOT_SYMLINK"  # path exists but is a regular file or directory
    MISSING = "MISSING"  # path does not exist at all and is not a symlink


def classify_symlink(path: Path, expected_source: Path) -> SymlinkVerdict:
    """Classify a symlink target against an expected canonical source.

    Args:
        path: The filesystem path to inspect.
        expected_source: The canonical source that ``path`` should point to.

    Returns:
        A :class:`SymlinkVerdict` value describing the relationship.
    """
    if path.is_symlink():
        # Use strict=True so dangling symlinks raise OSError rather than
        # silently resolving to a non-existent path (the old strict=False bug).
        try:
            resolved = path.resolve(strict=True)
        except OSError:
            return SymlinkVerdict.DANGLING
        if resolved == expected_source.resolve():
            return SymlinkVerdict.OK
        return SymlinkVerdict.WRONG_TARGET
    if path.exists():
        return SymlinkVerdict.NOT_SYMLINK
    return SymlinkVerdict.MISSING


# Coarse-grained mappings used by status (backwards-compatible labels)
_STATUS_MAP: dict[SymlinkVerdict, str] = {
    SymlinkVerdict.OK: "OK",
    SymlinkVerdict.DANGLING: "CONFLICT",
    SymlinkVerdict.WRONG_TARGET: "CONFLICT",
    SymlinkVerdict.NOT_SYMLINK: "CONFLICT",
    SymlinkVerdict.MISSING: "MISSING",
}


def symlink_verdict_to_status(verdict: SymlinkVerdict) -> str:
    """Convert a :class:`SymlinkVerdict` to a coarse status string.

    This mapping is intentionally conservative: any broken symlink state
    becomes ``"CONFLICT"`` so that ``aikito status`` surfaces it without
    exposing fine-grained detail.  ``aikito doctor`` uses the verdict
    directly for precise diagnostics.
    """
    return _STATUS_MAP[verdict]
