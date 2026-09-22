"""Keep Aikito-owned workspace skills aligned with the installed CLI package."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TextIO

from .templating import BUNDLED_SKILL_NAMES, bundled_skill_path


class BundledSkillRefreshError(RuntimeError):
    """Raised when a bundled skill cannot be backed up or refreshed safely."""


def _directory_digest(root: Path) -> str | None:
    """Return a deterministic SHA-256 digest for one complete directory tree."""
    if not root.is_dir() or root.is_symlink():
        return None

    digest = hashlib.sha256()
    for path in sorted(
        root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_symlink():
            kind = b"link"
            content = os.readlink(path).encode("utf-8")
        elif path.is_dir():
            kind = b"dir"
            content = b""
        elif path.is_file():
            kind = b"file"
            content = path.read_bytes()
        else:
            kind = b"other"
            content = b""
        for part in (kind, relative, content):
            digest.update(len(part).to_bytes(8, "big"))
            digest.update(part)
    return digest.hexdigest()


directory_digest = _directory_digest


def outdated_bundled_skills(workspace: Path) -> tuple[str, ...]:
    """Return bundled system skills that differ from the installed package."""
    skills_root = workspace / "skills"
    if not skills_root.is_dir():
        return ()

    outdated: list[str] = []
    for name in BUNDLED_SKILL_NAMES:
        source = bundled_skill_path(name)
        target = skills_root / name
        if _directory_digest(target) != _directory_digest(source):
            outdated.append(name)
    return tuple(outdated)


def print_bundled_skill_notice(
    workspace: Path,
    *,
    names: tuple[str, ...] | None = None,
    output: TextIO | None = None,
) -> tuple[str, ...]:
    """Warn when installed bundled skills and workspace snapshots differ."""
    output = output or sys.stderr
    outdated = outdated_bundled_skills(workspace)
    if names is not None:
        selected = set(names)
        outdated = tuple(name for name in outdated if name in selected)
    if outdated:
        rendered = ", ".join(outdated)
        print(
            f"\n[NOTICE] Bundled skill snapshot differs from the installed Aikito "
            f"package: {rendered}. Run 'aikito sync global' to refresh it.",
            file=output,
        )
    return outdated


def _backup_target(target: Path, backup: Path) -> None:
    backup.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        backup.symlink_to(os.readlink(target), target_is_directory=target.is_dir())
    elif target.is_dir():
        shutil.copytree(target, backup, symlinks=True)
    else:
        shutil.copy2(target, backup, follow_symlinks=False)


def _replace_directory(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
    staged = staging_root / target.name
    try:
        shutil.copytree(source, staged)
        if target.is_symlink() or target.is_file():
            target.unlink()
        elif target.is_dir():
            shutil.rmtree(target)
        os.replace(staged, target)
    finally:
        shutil.rmtree(staging_root, ignore_errors=True)


def refresh_bundled_skills(
    workspace: Path,
    home: Path,
    *,
    dry_run: bool = False,
) -> tuple[str, ...]:
    """Refresh divergent bundled skills, preserving previous contents in backups."""
    outdated = outdated_bundled_skills(workspace)
    if not outdated:
        return ()

    if dry_run:
        for name in outdated:
            print(f"[DRY-RUN] Would refresh bundled skill: {name}")
        return outdated

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    backup_root = home / ".aikito" / "backups" / f"bundled-skills_{timestamp}"

    try:
        for name in outdated:
            target = workspace / "skills" / name
            source = bundled_skill_path(name)
            if target.exists() or target.is_symlink():
                backup = backup_root / name
                _backup_target(target, backup)
                print(f"[BACKUP] Bundled skill '{name}': {backup}")
            _replace_directory(source, target)
            print(f"[REFRESH] Bundled skill '{name}' updated from installed Aikito")
    except OSError as exc:
        raise BundledSkillRefreshError(
            f"Failed to refresh bundled skill '{name}': {exc}"
        ) from exc

    return outdated
