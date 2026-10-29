"""Host-local checkout approval, separate from resource copy ownership.

Old skill state proves a path was managed, but cannot prove the directory at that
path is still the same. Only successful manual sync records physical identity.
Callers hold WorkspaceWriterLock while checking or recording approval.
"""

from __future__ import annotations

import os
from pathlib import Path

from .approval_io import read_approval, write_approval
from .compat import is_reparse_point
from .directory_identity import directory_identity
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
    identity = directory_identity(checkout)
    return {
        "checkout": identity["path"],
        "device": identity["device"],
        "inode": identity["inode"],
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
        data = read_approval(path)
        if data is None:
            return f"Run 'aikito sync project {project}' once to approve this checkout"
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
        payload = {
            "version": 1,
            "workspace": str(workspace.resolve()),
            "project": project,
            **identity,
        }
        try:
            if read_approval(path) == payload:
                continue
        except ValueError:
            # Explicit successful manual sync may replace malformed local approval.
            pass
        write_approval(path, payload)
