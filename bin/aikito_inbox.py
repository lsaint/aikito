"""
Inbox resource resolution and file finding helpers for aikito.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List

from aikito_memory import ensure_safe_path


@dataclass
class InboxNoteRow:
    name: str
    modified: str
    file_path: Path
    mtime: float


class InboxTargetConflictError(Exception):
    def __init__(self, target: str, candidates: List[Path]):
        super().__init__(f"Multiple inbox notes match '{target}'")
        self.target = target
        self.candidates = candidates


def find_inbox_files(inbox_dir: Path) -> List[Path]:
    """Find all valid markdown notes in the inbox directory."""
    if not inbox_dir.is_dir():
        return []

    files: List[Path] = []
    for p in inbox_dir.rglob("*.md"):
        if p.is_file() and not p.name.startswith("."):
            try:
                rel = p.relative_to(inbox_dir)
                if any(part.startswith(".") for part in rel.parts):
                    continue
            except ValueError:
                pass
            files.append(p)
    return sorted(files)


def collect_inbox_rows(inbox_dir: Path) -> List[InboxNoteRow]:
    """Collect inbox note rows sorted by modification time descending."""
    files = find_inbox_files(inbox_dir)
    rows: List[InboxNoteRow] = []

    for f in files:
        try:
            st = f.stat()
            mtime = st.st_mtime
            dt = datetime.fromtimestamp(mtime)
            mod_str = dt.strftime("%Y-%m-%d %H:%M")
        except OSError:
            mtime = 0.0
            mod_str = "-"

        try:
            rel = f.relative_to(inbox_dir)
            rel_stem = rel.with_suffix("").as_posix()
        except ValueError:
            rel_stem = f.stem

        rows.append(
            InboxNoteRow(
                name=rel_stem,
                modified=mod_str,
                file_path=f,
                mtime=mtime,
            )
        )

    rows.sort(key=lambda r: (-r.mtime, r.name))
    return rows


def resolve_inbox_target(inbox_dir: Path, target: str) -> Path:
    """Resolve inbox note target by exact match or unique prefix."""
    target_raw = target.strip().replace("\\", "/")
    if target_raw.endswith("…"):
        target_raw = target_raw[:-1]
    target_norm = target_raw[:-3] if target_raw.endswith(".md") else target_raw

    files = find_inbox_files(inbox_dir)
    if not files:
        print(f"[ERROR] Inbox note '{target}' not found.", file=sys.stderr)
        print("Run 'aikito show inbox' to view available inbox files.", file=sys.stderr)
        sys.exit(1)

    matched: List[Path] = []
    seen_paths = set()

    def add_match(file_path: Path) -> None:
        resolved = ensure_safe_path(file_path, [inbox_dir], "inbox note")
        if resolved not in seen_paths:
            seen_paths.add(resolved)
            matched.append(resolved)

    file_keys_map = {}
    for f in files:
        try:
            rel = f.relative_to(inbox_dir)
            rel_no_ext = rel.with_suffix("").as_posix()
            rel_str = rel.as_posix()
        except ValueError:
            rel_no_ext = f.stem
            rel_str = f.name
        keys = (f.stem, rel_no_ext, f.name, rel_str)
        file_keys_map[f] = keys

    exact_matches = [
        f
        for f, keys in file_keys_map.items()
        if target_norm in (keys[0], keys[1]) or target_raw in (keys[2], keys[3])
    ]
    candidates = exact_matches or [
        f
        for f, keys in file_keys_map.items()
        if any(k.startswith(target_norm) or k.startswith(target_raw) for k in keys)
    ]

    for f in candidates:
        add_match(f)

    if len(matched) == 1:
        return matched[0]

    if len(matched) > 1:
        raise InboxTargetConflictError(target, matched)

    print(f"[ERROR] Inbox note '{target}' not found.", file=sys.stderr)
    print("Run 'aikito show inbox' to view available inbox files.", file=sys.stderr)
    sys.exit(1)


def resolve_inbox_target_for_command(
    inbox_dir: Path, target: str, operation: str = "show"
) -> Path:
    """Resolve inbox note target with formatted CLI error output on conflicts."""
    try:
        return resolve_inbox_target(inbox_dir, target)
    except InboxTargetConflictError as exc:
        print(
            f"[CONFLICT] Multiple inbox notes match '{exc.target}':\n",
            file=sys.stderr,
        )
        for item in exc.candidates:
            try:
                rel = item.relative_to(inbox_dir)
                ident = rel.with_suffix("").as_posix()
            except ValueError:
                ident = item.stem
            print(f"  - {ident}", file=sys.stderr)
        print("\nPlease specify the exact name, e.g.:", file=sys.stderr)
        for item in exc.candidates:
            try:
                rel = item.relative_to(inbox_dir)
                ident = rel.with_suffix("").as_posix()
            except ValueError:
                ident = item.stem
            print(
                f"  aikito {operation} inbox {ident}",
                file=sys.stderr,
            )
        sys.exit(1)



def remove_inbox_note(inbox_dir: Path, target: str | Path) -> Path:
    """Remove an inbox note file."""
    if isinstance(target, Path):
        target_path = target
    else:
        target_path = resolve_inbox_target(inbox_dir, target)

    safe_path = ensure_safe_path(target_path, [inbox_dir], "inbox note")
    if not safe_path.is_file():
        raise FileNotFoundError(f"Inbox note not found: {safe_path}")

    safe_path.unlink()
    return safe_path
