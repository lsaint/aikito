"""Resource synchronization primitives shared by global and project sync flows.

These helpers implement link/copy mechanics, cleanup of managed runtime
entries, and safe project instruction linking. The CLI entry only wires them
into command handlers; keeping them here keeps the entry thin and importable.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

from aikito_platform import require_symlink_support, safe_symlink


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def sync_resource(
    source: Path, target: Path, mode: str = "link", dry_run: bool = False
) -> bool:
    """Syncs source path to target path using specified mode ('link' or 'copy')."""
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


def sync_project_instruction(source: Path, target: Path, dry_run: bool) -> bool:
    """Create a project instruction link without replacing existing content."""
    expected = source.resolve(strict=False)
    if target.is_symlink():
        if target.resolve(strict=False) == expected:
            print(f"[OK] Project instructions: {target} -> {source}")
            return True
        print(
            f"[CONFLICT] Project instructions already exist: {target}",
            file=sys.stderr,
        )
        return False
    if target.exists():
        print(
            f"[CONFLICT] Project instructions already exist: {target}",
            file=sys.stderr,
        )
        return False
    if dry_run:
        print(f"[DRY RUN LINK] {source} -> {target}")
        return True
    require_symlink_support()
    target.parent.mkdir(parents=True, exist_ok=True)
    if safe_symlink(source, target):
        print(f"[LINK] {target} -> {source}")
        return True
    return False


def sync_global_entry(
    source: Path,
    target: Path,
    agent_name: str,
    resource_name: str,
    dry_run: bool = False,
    installed: bool | None = None,
) -> bool:
    """Ensures an agent runtime entry points to its canonical global resource.

    Existing regular files and directories are never overwritten because they
    may contain unmanaged user resources.
    """
    if not target.parent.exists():
        if installed is True:
            target.parent.mkdir(parents=True)
        else:
            print(f"[SKIP] {agent_name} not detected: {target.parent}")
            return True

    if target.resolve(strict=False) == source.resolve(strict=False):
        print(f"[OK] {agent_name} {resource_name}: shared path {source}")
        return True

    expected_source = source.resolve()

    if target.is_symlink():
        if target.resolve(strict=False) == expected_source:
            print(f"[OK] {agent_name} {resource_name}: {target} -> {source}")
            return True

        print(f"[RELINK] {agent_name} {resource_name}: {target} -> {source}")
        if dry_run:
            return True
        target.unlink()
        require_symlink_support()
        return safe_symlink(expected_source, target)

    if target.exists():
        print(
            f"[CONFLICT] {agent_name} {resource_name}: {target} is not a symlink; "
            "move or merge it manually, then run 'aikito sync global' again.",
            file=sys.stderr,
        )
        return False

    if dry_run:
        print(f"[DRY RUN LINK] {agent_name} {resource_name}: {target} -> {source}")
        return True
    require_symlink_support()
    if safe_symlink(expected_source, target):
        print(f"[LINK] {agent_name} {resource_name}: {target} -> {source}")
        return True
    return False
