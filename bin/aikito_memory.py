"""
Memory resource resolution and file finding helpers for aikito.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List


class MemoryFileItem:
    def __init__(self, scope: str, rel_path: Path, full_path: Path):
        self.scope = scope
        self.rel_path = rel_path
        self.full_path = full_path

    @property
    def stem(self) -> str:
        return self.full_path.stem

    @property
    def short_identifier(self) -> str:
        return f"{self.scope}/{self.stem}"

    @property
    def full_identifier(self) -> str:
        rel_no_ext = str(self.rel_path.with_suffix(""))
        return f"{self.scope}/{rel_no_ext}"


class MemoryTargetConflictError(Exception):
    def __init__(self, target: str, candidates: list[MemoryFileItem]):
        super().__init__(f"Multiple memory notes match '{target}'")
        self.target = target
        self.candidates = candidates


def find_memory_files(aikito_dir: Path) -> List[MemoryFileItem]:
    items = []

    # 1. Global memory
    global_mem = aikito_dir / "memory"
    if global_mem.is_dir():
        for file_path in global_mem.rglob("*.md"):
            if file_path.is_file():
                rel_path = file_path.relative_to(global_mem)
                items.append(
                    MemoryFileItem(
                        scope="global", rel_path=rel_path, full_path=file_path
                    )
                )

    # 2. Project memory
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if proj_folder.is_dir():
                proj_mem = proj_folder / "memory"
                if proj_mem.is_dir():
                    for file_path in proj_mem.rglob("*.md"):
                        if file_path.is_file():
                            rel_path = file_path.relative_to(proj_mem)
                            items.append(
                                MemoryFileItem(
                                    scope=proj_folder.name,
                                    rel_path=rel_path,
                                    full_path=file_path,
                                )
                            )

    return items


def ensure_safe_path(
    target_path: Path, allowed_roots: List[Path], resource_label: str
) -> Path:
    resolved_path = target_path.resolve()
    resolved_roots = [r.resolve() for r in allowed_roots]

    is_safe = False
    for root in resolved_roots:
        try:
            if resolved_path.is_relative_to(root):
                is_safe = True
                break
        except AttributeError:
            if str(resolved_path).startswith(str(root)):
                is_safe = True
                break

    if not is_safe:
        print(
            f"[ERROR] Path escape detected for {resource_label}: {target_path}",
            file=sys.stderr,
        )
        sys.exit(1)

    return resolved_path


def resolve_memory_target(aikito_dir: Path, target: str) -> Path:
    target_raw = target.strip()
    if target_raw.endswith("…"):
        target_raw = target_raw[:-1]
    target_norm = target_raw[:-3] if target_raw.endswith(".md") else target_raw

    items = find_memory_files(aikito_dir)

    matched = []
    seen_paths = set()

    def add_match(item: MemoryFileItem) -> None:
        global_mem = aikito_dir / "memory"
        projects_dir = aikito_dir / "projects"
        resolved_path = ensure_safe_path(
            item.full_path, [global_mem, projects_dir], "memory note"
        )

        if resolved_path not in seen_paths:
            seen_paths.add(resolved_path)
            matched.append(item)

    def match_keys(item: MemoryFileItem) -> tuple[str, ...]:
        if "/" in target_norm:
            return (item.short_identifier, item.full_identifier)
        return (item.stem,)

    exact_matches = [item for item in items if target_norm in match_keys(item)]
    candidates = exact_matches or [
        item
        for item in items
        if any(key.startswith(target_norm) for key in match_keys(item))
    ]
    for item in candidates:
        add_match(item)

    if len(matched) == 1:
        return matched[0].full_path

    if len(matched) > 1:
        raise MemoryTargetConflictError(target, matched)

    print(f"[ERROR] Memory note '{target}' not found.", file=sys.stderr)
    print("Run 'aikito show memory' to view available memory files.", file=sys.stderr)
    sys.exit(1)


def resolve_memory_target_for_command(
    aikito_dir: Path, target: str, operation: str
) -> Path:
    try:
        return resolve_memory_target(aikito_dir, target)
    except MemoryTargetConflictError as exc:
        print(
            f"[CONFLICT] Multiple memory notes match '{exc.target}':\n",
            file=sys.stderr,
        )
        for item in exc.candidates:
            print(f"  - {item.full_identifier}", file=sys.stderr)
        print("\nPlease specify the full identifier, e.g.:", file=sys.stderr)
        for item in exc.candidates:
            print(
                f"  aikito {operation} memory {item.full_identifier}",
                file=sys.stderr,
            )
        sys.exit(1)
