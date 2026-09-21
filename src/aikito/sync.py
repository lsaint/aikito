"""Resource synchronization primitives shared by global and project sync flows.

These helpers implement link/copy mechanics, cleanup of managed runtime
entries, and safe project instruction linking. The CLI entry only wires them
into command handlers; keeping them here keeps the entry thin and importable.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from .compat import require_symlink_support, safe_symlink


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sync_resource(
    source: Path, target: Path, mode: str = "link", dry_run: bool = False
) -> bool:
    """Syncs source path to target path using specified mode ('link' or 'copy').

    NOTE(Phase 6): Remaining caller is project memory in project_sync.py.
    Will be retired in Phase 6 when memory migrates to the unified model.
    Instructions and skills no longer call this function.
    (remaining caller = project memory; removal = Phase 6)
    """
    if not source.exists():
        print(f"[WARN] Source path does not exist: {source}", file=sys.stderr)
        return False

    mode = mode.lower()
    if mode not in ("link", "copy"):
        print(
            f"[WARN] Invalid sync_mode '{mode}', defaulting to 'link'", file=sys.stderr
        )
        mode = "link"

    if dry_run:
        action = "LINK" if mode == "link" else "COPY"
        print(f"[DRY RUN {action}] {source} -> {target}")
        return True

    # Gate symlink capability before any destructive operation
    if mode == "link":
        require_symlink_support()

    # Remove existing target if needed (symlink, file, or directory)
    if target.is_symlink() or target.exists():
        try:
            if target.is_symlink() or target.is_file():
                target.unlink()
            elif target.is_dir():
                shutil.rmtree(target)
        except Exception as e:
            print(
                f"[ERROR] Failed to remove existing target {target}: {e}",
                file=sys.stderr,
            )
            return False

    if mode == "link":
        if safe_symlink(source, target):
            print(f"[LINK] {target} -> {source}")
            return True
        return False

    if mode == "copy":
        try:
            if source.is_dir():
                shutil.copytree(source, target)
                print(f"[COPY DIR] {source} -> {target}")
            else:
                shutil.copy2(source, target)
                print(f"[COPY FILE] {source} -> {target}")
            return True
        except Exception as e:
            print(f"[ERROR] Failed to copy {source} to {target}: {e}", file=sys.stderr)
            return False
    return False


def apply_runtime_cleanup(paths: tuple[Path, ...], dry_run: bool) -> None:
    """Remove entries already proven to be Aikito-managed by a cleanup plan."""
    for path in paths:
        if dry_run:
            print(f"[DRY RUN CLEANUP] Would remove stale managed item: {path}")
        elif path.is_symlink() or path.is_file():
            path.unlink()
            print(f"[CLEANUP] Removed stale managed item: {path}")
        elif path.is_dir():
            shutil.rmtree(path)
            print(f"[CLEANUP] Removed stale managed item: {path}")
