"""
Memory resource resolution and file finding helpers for aikito.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")


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
        rel_no_ext = self.rel_path.with_suffix("").as_posix()
        return f"{self.scope}/{rel_no_ext}"



class MemoryTargetConflictError(Exception):
    def __init__(self, target: str, candidates: list[MemoryFileItem]):
        super().__init__(f"Multiple memory notes match '{target}'")
        self.target = target
        self.candidates = candidates


class RenameResult(NamedTuple):
    old_path: Path
    new_path: Path
    scope: str
    old_stem: str
    new_stem: str
    index_file: Optional[Path]
    index_updated: bool
    refactored_notes: List[Path]


class InboundReference(NamedTuple):
    note_path: Path
    line_number: int
    line_content: str


class RemoveResult(NamedTuple):
    deleted_path: Path
    scope: str
    stem: str
    index_file: Optional[Path]
    index_updated: bool
    inbound_references: List[InboundReference]


def validate_memory_name(name: str) -> Optional[str]:
    """Validate memory note name conforms to kebab-case alphanumeric naming and length <= 50."""
    if not name or not isinstance(name, str) or not name.strip():
        return "Memory note name cannot be empty."

    name_clean = name.strip()
    if (
        "/" in name_clean
        or "\\" in name_clean
        or "\0" in name_clean
        or ".." in name_clean
    ):
        return f"Invalid memory note name '{name}'. Path separators and traversals are not allowed."

    if len(name_clean) > 50:
        return f"Invalid memory note name '{name}'. Length must be at most 50 characters (got {len(name_clean)})."

    if not NAME_PATTERN.fullmatch(name_clean):
        return (
            f"Invalid memory note name '{name}'. "
            f"Must be kebab-case (lowercase alphanumeric characters separated by hyphens, e.g. 'payment-idempotency')."
        )
    return None


def extract_note_title(note_path: Path) -> str:
    """Extract display title from note's first heading (# Title), or derive from stem."""
    stem = note_path.stem
    if note_path.is_file():
        try:
            content = note_path.read_text(encoding="utf-8", errors="ignore")
            for line in content.splitlines():
                line_stripped = line.strip()
                if line_stripped.startswith("# ") and not line_stripped.startswith(
                    "## "
                ):
                    title = line_stripped[2:].strip()
                    if title:
                        return title
        except OSError:
            pass
    return " ".join(
        word.capitalize() for word in stem.replace("-", " ").replace("_", " ").split()
    )


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


def _determine_scope_and_dir(aikito_dir: Path, note_path: Path) -> tuple[str, Path]:
    """Determine (scope_name, scope_dir) for a note in notes/."""
    scope_dir = note_path.parent.parent
    try:
        rel = scope_dir.resolve().relative_to(aikito_dir.resolve())
        parts = rel.parts
        if len(parts) == 1 and parts[0] == "memory":
            return ("global", scope_dir)
        if len(parts) == 3 and parts[0] == "projects" and parts[2] == "memory":
            return (parts[1], scope_dir)
    except (ValueError, AttributeError):
        pass

    if scope_dir.name == "memory":
        if scope_dir.parent.parent.name == "projects":
            return (scope_dir.parent.name, scope_dir)
        return ("global", scope_dir)

    return (scope_dir.name, scope_dir)


def rename_memory_note(
    aikito_dir: Path, target: str | Path, new_name: str
) -> RenameResult:
    """Atomically rename a memory note, update its index entry, and refactor inbound wikilinks."""
    err = validate_memory_name(new_name)
    if err:
        raise ValueError(err)

    if isinstance(target, Path):
        target_path = target
    else:
        target_path = resolve_memory_target(aikito_dir, target)

    if target_path.parent.name != "notes" or target_path.name == "index.md":
        raise ValueError(
            f"Cannot rename '{target_path.name}'. Only atomic memory notes in 'notes/' can be renamed."
        )

    old_stem = target_path.stem
    if old_stem == new_name:
        raise ValueError(f"Note '{old_stem}' is already named '{new_name}'.")

    parent_dir = target_path.parent
    new_path = parent_dir / f"{new_name}.md"
    if new_path.exists() and new_path.resolve() != target_path.resolve():
        raise FileExistsError(f"Target memory note '{new_path.name}' already exists.")

    scope, scope_dir = _determine_scope_and_dir(aikito_dir, target_path)
    index_file = scope_dir / "index.md"

    # 1. Rename physical note file
    target_path.rename(new_path)

    # 2. Update index.md in the note's scope
    index_updated = False
    pattern = re.compile(r"\[\[" + re.escape(old_stem) + r"(?=[|#\]])")
    if index_file.is_file():
        index_content = index_file.read_text(encoding="utf-8")
        new_index_content, n_subs = pattern.subn(f"[[{new_name}", index_content)
        if n_subs > 0:
            index_file.write_text(new_index_content, encoding="utf-8")
            index_updated = True

    # 3. Refactor inbound wikilinks in notes within the same scope
    scope_notes_dir = scope_dir / "notes"
    refactored_notes: List[Path] = []
    if scope_notes_dir.is_dir():
        for note_file in sorted(scope_notes_dir.glob("*.md")):
            try:
                content = note_file.read_text(encoding="utf-8", errors="ignore")
                new_content, n_subs = pattern.subn(f"[[{new_name}", content)
                if n_subs > 0:
                    note_file.write_text(new_content, encoding="utf-8")
                    refactored_notes.append(note_file)
            except OSError:
                pass

    return RenameResult(
        old_path=target_path,
        new_path=new_path,
        scope=scope,
        old_stem=old_stem,
        new_stem=new_name,
        index_file=index_file if index_file.exists() else None,
        index_updated=index_updated,
        refactored_notes=refactored_notes,
    )


def remove_memory_note(aikito_dir: Path, target: str | Path) -> RemoveResult:
    """Remove a memory note, prune its index entry, and scan for inbound wikilinks."""
    if isinstance(target, Path):
        target_path = target
    else:
        target_path = resolve_memory_target(aikito_dir, target)

    if target_path.parent.name != "notes" or target_path.name == "index.md":
        raise ValueError(
            f"Cannot remove '{target_path.name}'. Only atomic memory notes in 'notes/' can be removed."
        )

    stem = target_path.stem
    scope, scope_dir = _determine_scope_and_dir(aikito_dir, target_path)
    index_file = scope_dir / "index.md"
    pattern = re.compile(r"\[\[" + re.escape(stem) + r"(?=[|#\]])")

    # 1. Scan inbound references within the same scope before deletion
    scope_notes_dir = scope_dir / "notes"
    inbound_references: List[InboundReference] = []
    if scope_notes_dir.is_dir():
        for note_file in sorted(scope_notes_dir.glob("*.md")):
            if note_file.resolve() == target_path.resolve():
                continue
            try:
                content = note_file.read_text(encoding="utf-8", errors="ignore")
                for idx, line in enumerate(content.splitlines(), start=1):
                    if pattern.search(line):
                        inbound_references.append(
                            InboundReference(
                                note_path=note_file,
                                line_number=idx,
                                line_content=line.strip(),
                            )
                        )
            except OSError:
                pass

    # 2. Delete physical file
    target_path.unlink()

    # 3. Prune index.md
    index_updated = False
    if index_file.is_file():
        lines = index_file.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for line in lines:
            if pattern.search(line):
                index_updated = True
                continue
            new_lines.append(line)

        if index_updated:
            output_text = "\n".join(new_lines)
            if output_text and not output_text.endswith("\n"):
                output_text += "\n"
            index_file.write_text(output_text, encoding="utf-8")

    return RemoveResult(
        deleted_path=target_path,
        scope=scope,
        stem=stem,
        index_file=index_file if index_file.exists() else None,
        index_updated=index_updated,
        inbound_references=inbound_references,
    )
