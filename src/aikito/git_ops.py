"""Git operations, tracking detection, and info/exclude manipulation."""

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path


def is_git_repository(repo_path: Path) -> bool:
    """Return True if repo_path is inside a git working tree."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_path), "rev-parse", "--is-inside-work-tree"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=5,
        )
        return result.returncode == 0 and result.stdout.strip() == "true"
    except (OSError, subprocess.SubprocessError):
        return False


def get_git_repo_root(repo_dir: Path) -> Path | None:
    """Return the git repository top-level directory for repo_dir, or None if not git."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        res = subprocess.run(
            ["git", "-C", str(repo_dir), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=5,
        )
        if res.returncode == 0 and res.stdout.strip():
            return Path(res.stdout.strip()).resolve()
        return None
    except (OSError, subprocess.SubprocessError):
        return None


DEFAULT_TRACKED_PATHSPECS = (
    "AGENTS.md",
    "CLAUDE.md",
    ".claude/CLAUDE.md",
    ".agents/skills",
)


class GitRepoTracker:
    """Caches tracked files and directories for a git repository or checkout."""

    def __init__(
        self,
        repo_dir: Path,
        initial_pathspecs: Sequence[str] = DEFAULT_TRACKED_PATHSPECS,
    ):
        self.repo_dir = repo_dir.resolve()
        self.tracked_files: set[str] = set()
        self.tracked_dirs: set[str] = set()
        self.queried_pathspecs: set[str] = set()
        self.index_path: Path | None = self._get_index_path()
        self.index_mtime_ns: int | None = None
        self._load(initial_pathspecs)

    def _get_index_path(self) -> Path | None:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            res = subprocess.run(
                ["git", "-C", str(self.repo_dir), "rev-parse", "--git-path", "index"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
                timeout=5,
            )
            if res.returncode == 0 and res.stdout.strip():
                p = Path(res.stdout.strip())
                if not p.is_absolute():
                    p = (self.repo_dir / p).resolve()
                return p
            return None
        except (OSError, subprocess.SubprocessError):
            return None

    def _load(self, pathspecs: Sequence[str]) -> None:
        clean_specs = [s.strip() for s in pathspecs if s.strip()]
        if not clean_specs:
            return

        if self.index_path and self.index_path.is_file():
            try:
                self.index_mtime_ns = self.index_path.stat().st_mtime_ns
            except OSError:
                self.index_mtime_ns = None
        else:
            self.index_mtime_ns = None

        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        try:
            res = subprocess.run(
                ["git", "-C", str(self.repo_dir), "ls-files", "--", *clean_specs],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=env,
                check=False,
                timeout=5,
            )
            if res.returncode == 0:
                self.queried_pathspecs.update(clean_specs)
                for line in res.stdout.splitlines():
                    f = line.strip()
                    if f:
                        self.tracked_files.add(f)
                        parts = f.split("/")
                        for i in range(1, len(parts)):
                            self.tracked_dirs.add("/".join(parts[:i]))
        except (OSError, subprocess.SubprocessError):
            pass

    def _check_freshness(self) -> None:
        if self.index_path is None or not self.index_path.is_file():
            new_index = self._get_index_path()
            if new_index and new_index.is_file():
                self.index_path = new_index
                specs = list(self.queried_pathspecs) or list(DEFAULT_TRACKED_PATHSPECS)
                self.tracked_files.clear()
                self.tracked_dirs.clear()
                self.queried_pathspecs.clear()
                self._load(specs)
                return

        needs_reload = False
        if self.index_path and self.index_path.is_file():
            try:
                curr_mtime = self.index_path.stat().st_mtime_ns
                if curr_mtime != self.index_mtime_ns:
                    needs_reload = True
            except OSError:
                needs_reload = True
        elif self.index_mtime_ns is not None:
            needs_reload = True

        if needs_reload:
            specs = list(self.queried_pathspecs) or list(DEFAULT_TRACKED_PATHSPECS)
            self.tracked_files.clear()
            self.tracked_dirs.clear()
            self.queried_pathspecs.clear()
            self._load(specs)

    def is_tracked(self, path: Path) -> bool:
        self._check_freshness()
        if not path.is_absolute():
            path = self.repo_dir / path

        try:
            target_path = path.parent.resolve() / path.name
            rel = target_path.relative_to(self.repo_dir).as_posix()
        except (ValueError, OSError):
            try:
                rel = path.relative_to(self.repo_dir).as_posix()
            except ValueError:
                return False

        if rel == "." or not rel:
            return bool(self.tracked_files)

        covered = any(
            rel == spec
            or rel.startswith(spec.rstrip("/") + "/")
            or spec.startswith(rel.rstrip("/") + "/")
            for spec in self.queried_pathspecs
        )
        if not covered:
            self._load([rel])

        if path.is_dir() and not path.is_symlink():
            return rel in self.tracked_dirs
        return rel in self.tracked_files


_GIT_TRACKER_CACHE: dict[Path, GitRepoTracker] = {}


def get_git_repo_tracker(repo_dir: Path, *, fresh: bool = False) -> GitRepoTracker:
    root = repo_dir.resolve()
    if fresh or root not in _GIT_TRACKER_CACHE:
        _GIT_TRACKER_CACHE[root] = GitRepoTracker(root)
    return _GIT_TRACKER_CACHE[root]


def clear_git_tracker_cache() -> None:
    _GIT_TRACKER_CACHE.clear()


def is_git_tracked(
    path: Path,
    repo_root: Path | None = None,
    *,
    tracker: GitRepoTracker | None = None,
) -> bool:
    """Return True if path is tracked by git in the enclosing or provided repository."""
    if tracker is not None:
        return tracker.is_tracked(path)

    if repo_root is None:
        repo_root = get_git_repo_root(path if path.is_dir() else path.parent)
        if repo_root is None:
            return False
    else:
        repo_root = repo_root.resolve()

    tr = get_git_repo_tracker(repo_root)
    return tr.is_tracked(path)


def is_git_typechange(path: Path, repo_root: Path | None = None) -> bool:
    """Return True if path has typechange status ('T') in git working tree."""
    if repo_root is None:
        repo_root = get_git_repo_root(path if path.is_dir() else path.parent)
        if repo_root is None:
            return False
    else:
        repo_root = repo_root.resolve()

    if not path.is_absolute():
        path = repo_root / path

    try:
        target_path = path.parent.resolve() / path.name
        rel = target_path.relative_to(repo_root).as_posix()
    except (ValueError, OSError):
        try:
            rel = path.relative_to(repo_root).as_posix()
        except ValueError:
            return False

    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "status", "--porcelain", "--", rel],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=5,
        )
        if result.returncode != 0 or not result.stdout:
            return False
        for line in result.stdout.splitlines():
            if len(line) >= 2 and "T" in line[:2]:
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return False


def get_git_exclude_pattern(repo_dir: Path, entry: str) -> str:
    """Return the normalized git info/exclude pattern with leading slash relative to repo root."""
    clean = entry.strip().rstrip("/")
    if not clean:
        return ""
    clean_rel = clean.lstrip("/")
    toplevel = get_git_repo_root(repo_dir)
    if toplevel is None:
        return f"/{clean_rel}"
    try:
        rel_dir = repo_dir.resolve().relative_to(toplevel).as_posix()
    except ValueError:
        rel_dir = ""
    if not rel_dir or rel_dir == ".":
        return f"/{clean_rel}"
    return f"/{rel_dir}/{clean_rel}"


def get_git_exclude_path(repo_dir: Path) -> Path | None:
    """Return the absolute path to info/exclude for repo_dir, or None if not git."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
            check=False,
            timeout=5,
        )
        if res.returncode != 0:
            return None
        out = res.stdout.strip()
        if not out:
            return None
        p = Path(out)
        if not p.is_absolute():
            p = (repo_dir / p).resolve()
        return p
    except (OSError, subprocess.SubprocessError):
        return None


def add_git_exclude_entries(repo_dir: Path, entries: Sequence[str]) -> bool:
    """Add entries to info/exclude idempotently.

    Entries should be relative to repo_dir. They are normalized with leading
    slash relative to the git repository top-level directory.
    """
    if not is_git_repository(repo_dir):
        return True

    exclude_path = get_git_exclude_path(repo_dir)
    if exclude_path is None:
        return False

    patterns = [get_git_exclude_pattern(repo_dir, e) for e in entries]
    clean_patterns = [p for p in patterns if p]
    if not clean_patterns:
        return True

    lines: list[str] = []
    if exclude_path.is_file():
        try:
            lines = exclude_path.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
        except OSError:
            return False

    existing = set(
        line.strip()
        for line in lines
        if line.strip() and not line.strip().startswith("#")
    )
    to_add = [
        p for p in clean_patterns if p not in existing and p.lstrip("/") not in existing
    ]
    if not to_add:
        return True

    lines.extend(to_add)
    exclude_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        exclude_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return True
    except OSError:
        return False


def remove_git_exclude_entries(repo_dir: Path, entries: Sequence[str]) -> bool:
    """Remove matching entries from info/exclude."""
    if not is_git_repository(repo_dir):
        return True

    exclude_path = get_git_exclude_path(repo_dir)
    if exclude_path is None:
        return False
    if not exclude_path.is_file():
        return True

    to_remove = set()
    for e in entries:
        pat = get_git_exclude_pattern(repo_dir, e)
        if pat:
            to_remove.add(pat)
            to_remove.add(pat.lstrip("/"))
            to_remove.add(e.strip().rstrip("/"))

    if not to_remove:
        return True

    try:
        lines = exclude_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False

    new_lines = [line for line in lines if line.strip() not in to_remove]
    if len(new_lines) == len(lines):
        return True

    try:
        content = "\n".join(new_lines) + ("\n" if new_lines else "")
        exclude_path.write_text(content, encoding="utf-8")
        return True
    except OSError:
        return False


def check_git_exclude_entries(
    repo_dir: Path, entries: Sequence[str]
) -> dict[str, bool]:
    """Return a mapping of entry -> bool indicating if entry is in info/exclude."""
    exclude_path = get_git_exclude_path(repo_dir)
    res = {e: False for e in entries}
    if exclude_path is None or not exclude_path.is_file():
        return res
    try:
        lines = exclude_path.read_text(encoding="utf-8", errors="replace").splitlines()
        existing = set(
            line.strip()
            for line in lines
            if line.strip() and not line.strip().startswith("#")
        )
        for e in entries:
            pat = get_git_exclude_pattern(repo_dir, e)
            res[e] = bool(pat and (pat in existing or pat.lstrip("/") in existing))
        return res
    except OSError:
        return res
