"""Host-local checkout approval, separate from resource copy ownership.

Old skill state proves a path was managed, but cannot prove the directory at that
path is still the same. Only successful manual sync records physical identity.
Callers hold WorkspaceWriterLock while checking or recording approval.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from .compat import _atomic_write_text, is_reparse_point, secure_file_permissions
from .project_config import get_project_candidate_paths
from .skill_state import get_binding_hash, validate_state_store_root


def candidate_checkout(raw: str, home: Path) -> Path:
    """Expand a configured path without discarding its symlink components."""
    normalized = raw.replace("\\", "/")
    if normalized == "~":
        return home.absolute()
    if normalized.startswith("~/"):
        return (home / normalized[2:]).absolute()
    return Path(os.path.abspath(os.path.expanduser(raw)))


def _identity(checkout: Path) -> dict:
    current = checkout
    # System aliases above home (e.g. /var on macOS) are resolved in identity.
    while current.parent != current.parent.parent:
        if is_reparse_point(current):
            raise ValueError(
                f"Checkout path contains a symbolic link or reparse point: {current}"
            )
        current = current.parent
    info = checkout.stat()
    if not checkout.is_dir() or not info.st_ino:
        raise ValueError(f"Cannot establish checkout directory identity: {checkout}")
    return {
        "checkout": str(checkout.resolve()),
        "device": info.st_dev,
        "inode": info.st_ino,
    }


def _approval_path(
    workspace: Path, home: Path, project: str, checkout: Path, *, create: bool = False
) -> Path:
    root, error = validate_state_store_root(home, create_if_missing=create)
    if error:
        raise ValueError(error)
    return root / f"{get_binding_hash(workspace, project, checkout)}.approval"


def checkout_approval_error(
    workspace: Path, home: Path, project: str, checkout: Path
) -> str | None:
    """Return a reason when automatic writes are not approved; never creates state."""
    try:
        identity = _identity(checkout)
        path = _approval_path(workspace, home, project, checkout)
        if is_reparse_point(path):
            return f"Unsafe checkout approval file: {path}"
        if not path.exists():
            return f"Run 'aikito sync project {project}' once to approve this checkout"
        data = json.loads(path.read_text(encoding="utf-8"))
        expected = {
            "version": 1,
            "workspace": str(workspace.resolve()),
            "project": project,
            **identity,
        }
        if data != expected:
            return (
                "Checkout identity changed; run manual project sync to approve it again"
            )
    except (OSError, ValueError) as exc:
        return str(exc)
    return None


def approve_checkouts(
    workspace: Path, home: Path, project: str, checkouts: tuple[Path, ...], data: dict
) -> None:
    """Record successful manual synchronization, including instruction-only projects."""
    candidates = [
        candidate_checkout(raw, home) for _, raw in get_project_candidate_paths(data)
    ]
    for checkout in checkouts:
        aliases = [
            path for path in candidates if path.resolve() == checkout.resolve()
        ] or [checkout]
        # Approval requires stable, unaliased paths even though manual sync permits aliases.
        if any(is_reparse_point(path) for path in aliases):
            continue
        try:
            identity = _identity(checkout)
        except ValueError:
            continue
        path = _approval_path(workspace, home, project, checkout, create=True)
        if is_reparse_point(path):
            raise ValueError(f"Unsafe checkout approval file: {path}")
        payload = {
            "version": 1,
            "workspace": str(workspace.resolve()),
            "project": project,
            **identity,
        }
        try:
            if (
                path.exists()
                and json.loads(path.read_text(encoding="utf-8")) == payload
            ):
                continue
        except ValueError:
            # Explicit successful manual sync may replace malformed local approval.
            pass
        _atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")
        secure_file_permissions(path)
