"""Project configuration parsing, path resolution, and pure configuration transforms."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .compat import _atomic_write_text, safe_relative_path

DEFAULT_PROJECT_SYNC_MODE = "link"


@dataclass(frozen=True)
class ProjectPathEntry:
    label: str
    raw_path: str
    resolved_path: Path
    exists: bool


@dataclass(frozen=True)
class ProjectBinding:
    entries: tuple[ProjectPathEntry, ...]

    @property
    def active_entries(self) -> tuple[ProjectPathEntry, ...]:
        return tuple(e for e in self.entries if e.exists)

    @property
    def offline_entries(self) -> tuple[ProjectPathEntry, ...]:
        return tuple(e for e in self.entries if not e.exists)


def resolve_project_path(raw_path: object, home: Path) -> Path | None:
    """Resolve a raw project path string relative to home."""
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = raw_path.strip()
    if raw == "~":
        return home.resolve()
    normalized = raw.replace("\\", "/")
    if normalized.startswith("~/"):
        return (home / normalized[2:]).resolve()
    return Path(raw).expanduser().resolve()


def get_project_candidate_paths(
    config: Mapping[str, Any],
) -> tuple[tuple[str, str], ...]:
    """Extract candidate path definitions from a project's parsed agent.toml data."""
    candidates: list[tuple[str, str]] = []

    raw_paths_sec = config.get("paths")
    if isinstance(raw_paths_sec, dict):
        for key, val in raw_paths_sec.items():
            if isinstance(val, str) and val.strip():
                candidates.append((str(key), val.strip()))
    elif isinstance(raw_paths_sec, list):
        for idx, item in enumerate(raw_paths_sec, start=1):
            if isinstance(item, str) and item.strip():
                candidates.append((str(idx), item.strip()))

    if not candidates:
        raw_path = config.get("path")
        if isinstance(raw_path, list):
            for idx, item in enumerate(raw_path, start=1):
                if isinstance(item, str) and item.strip():
                    candidates.append((str(idx), item.strip()))
        elif isinstance(raw_path, str) and raw_path.strip():
            candidates.append(("default", raw_path.strip()))

    return tuple(candidates)


def resolve_project_binding(config: Mapping[str, Any], home: Path) -> ProjectBinding:
    """Resolve all configured project candidate paths and categorize them."""
    candidates = get_project_candidate_paths(config)
    entries: list[ProjectPathEntry] = []
    seen_paths: set[Path] = set()
    for label, raw in candidates:
        resolved = resolve_project_path(raw, home)
        if resolved is not None:
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            entries.append(
                ProjectPathEntry(
                    label=label,
                    raw_path=raw,
                    resolved_path=resolved,
                    exists=resolved.is_dir(),
                )
            )
    return ProjectBinding(tuple(entries))


def add_candidate_path_to_content(
    content: str, new_raw_path: str, home: Path, *, match_resolved: bool = True
) -> str | None:
    """Pure transformation of TOML content to add a new candidate path.

    Returns the updated TOML string, or None if the candidate path is already present.
    Raises ValueError if TOML is invalid or rewritten TOML fails verification.
    ``match_resolved=False`` keeps distinct raw collection members during import.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML content: {exc}") from exc

    candidates = get_project_candidate_paths(data)
    new_resolved = resolve_project_path(new_raw_path, home)
    for _, raw in candidates:
        if raw == new_raw_path or (
            match_resolved
            and new_resolved
            and resolve_project_path(raw, home) == new_resolved
        ):
            return None

    lines = content.splitlines()

    first_section_idx = len(lines)
    paths_section_header_idx: int | None = None
    paths_section_end_idx = len(lines)

    for i, line in enumerate(lines):
        section_match = re.match(r"^\s*\[([a-zA-Z0-9_.\-]+)\]\s*(?:#.*)?$", line)
        if section_match:
            sec_name = section_match.group(1)
            if first_section_idx == len(lines):
                first_section_idx = i
            if sec_name == "paths":
                paths_section_header_idx = i
            elif paths_section_header_idx is not None and paths_section_end_idx == len(
                lines
            ):
                paths_section_end_idx = i

    new_path_repr = json.dumps(new_raw_path)

    if paths_section_header_idx is not None:
        existing_keys = (
            set(data["paths"].keys()) if isinstance(data.get("paths"), dict) else set()
        )
        idx = 1
        while f"path_{idx}" in existing_keys:
            idx += 1
        new_key = f"path_{idx}"
        lines.insert(paths_section_end_idx, f"{new_key} = {new_path_repr}")

    elif isinstance(data.get("paths"), dict):
        existing_keys = set(data["paths"].keys())
        idx = 1
        while f"path_{idx}" in existing_keys:
            idx += 1
        new_key = f"path_{idx}"

        replaced = False
        for i in range(first_section_idx):
            if re.match(r"^\s*paths\s*=\s*\{", lines[i]):
                if "}" in lines[i]:
                    r_idx = lines[i].rindex("}")
                    before = lines[i][:r_idx].rstrip()
                    after = lines[i][r_idx:]
                    sep = ", " if before and not before.endswith("{") else " "
                    lines[i] = (
                        f"{before}{sep}{new_key} = {new_path_repr} {after.lstrip()}"
                    )
                    replaced = True
                    break
                else:
                    for j in range(i + 1, first_section_idx):
                        if "}" in lines[j]:
                            lines.insert(j, f"    {new_key} = {new_path_repr},")
                            replaced = True
                            break
                    if replaced:
                        break
        if not replaced:
            lines.insert(first_section_idx, f"[paths]\n{new_key} = {new_path_repr}\n")

    elif isinstance(data.get("paths"), list):
        replaced = False
        for i in range(first_section_idx):
            if re.match(r"^\s*paths\s*=\s*\[", lines[i]):
                if "]" in lines[i]:
                    r_idx = lines[i].rindex("]")
                    before = lines[i][:r_idx].rstrip()
                    after = lines[i][r_idx:]
                    sep = ", " if before and not before.endswith("[") else " "
                    lines[i] = f"{before}{sep}{new_path_repr}{after}"
                    replaced = True
                    break
                else:
                    for j in range(i + 1, len(lines)):
                        if "]" in lines[j]:
                            lines.insert(j, f"    {new_path_repr},")
                            replaced = True
                            break
                    if replaced:
                        break
        if not replaced:
            lines.insert(first_section_idx, f"paths = [{new_path_repr}]")

    elif "path" in data and any(
        re.match(r"^\s*path\s*=", lines[i]) for i in range(first_section_idx)
    ):
        for i in range(first_section_idx):
            if re.match(r"^\s*path\s*=", lines[i]):
                old_val = data["path"]
                if isinstance(old_val, list):
                    items = [json.dumps(p) for p in old_val] + [new_path_repr]
                else:
                    items = [json.dumps(old_val), new_path_repr]
                lines[i] = (
                    "paths = [\n" + "".join(f"    {item},\n" for item in items) + "]"
                )
                break

    else:
        entry = f"paths = [\n    {new_path_repr},\n]\n"
        if first_section_idx < len(lines):
            lines.insert(first_section_idx, entry)
        else:
            lines.append(entry)

    new_content = "\n".join(lines).strip() + "\n"

    try:
        verified_data = tomllib.loads(new_content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Failed to generate valid TOML: {exc}\nGenerated:\n{new_content}"
        ) from exc

    verified_candidates = [raw for _, raw in get_project_candidate_paths(verified_data)]
    if new_raw_path not in verified_candidates:
        raise ValueError(f"Failed to record new candidate path '{new_raw_path}'")

    return new_content


def add_candidate_path(pre_bytes: bytes, new_raw_path: str, home: Path) -> bytes | None:
    """Pure transformation of TOML bytes to add a new candidate path.

    Returns the updated UTF-8 bytes, or None if the candidate path is already present.
    """
    content = pre_bytes.decode("utf-8")
    post_content = add_candidate_path_to_content(content, new_raw_path, home)
    if post_content is None:
        return None
    return post_content.encode("utf-8")


def append_candidate_path_to_config(
    config_path: Path, new_raw_path: str, home: Path
) -> bool:
    """Append a new candidate path to an existing agent.toml if not already present.

    Returns True if appended, False if candidate already exists.
    Raises FileNotFoundError if config_path does not exist.
    Raises ValueError on invalid input or if rewritten TOML is invalid.
    """
    if not config_path.is_file():
        raise FileNotFoundError(f"Project config file not found: {config_path}")
    try:
        pre_bytes = config_path.read_bytes()
    except OSError as exc:
        raise OSError(f"Failed to read {config_path}: {exc}") from exc

    post_bytes = add_candidate_path(pre_bytes, new_raw_path, home)
    if post_bytes is None:
        return False

    _atomic_write_text(config_path, post_bytes.decode("utf-8"))
    return True


def display_project_path(path: Path | None, home: Path) -> str:
    """Display a Path object relative to home or as a clean string."""
    if path is None:
        return "-"
    resolved_home = home.resolve()
    for base in (home, resolved_home):
        displayed = safe_relative_path(path, base)
        if displayed.startswith("~/"):
            return displayed
    resolved_path = path.resolve()
    if resolved_path != path:
        displayed = safe_relative_path(resolved_path, resolved_home)
        if displayed.startswith("~/"):
            return displayed
    return path.as_posix()


def display_candidate_path(entry: ProjectPathEntry, home: Path) -> str:
    """Format a candidate path entry for user presentation."""
    if entry.exists:
        return display_project_path(entry.resolved_path, home)
    raw = entry.raw_path.strip().replace("\\", "/")
    if raw == "~" or raw.startswith("~/"):
        return raw
    unresolved = Path(raw)
    if unresolved.is_absolute():
        return display_project_path(unresolved, home)
    # Keep configured other-OS paths (e.g. D:/...) instead of cwd-resolving them.
    return raw


def candidate_path_views(
    binding: ProjectBinding, home: Path
) -> tuple[tuple[str, str, bool], ...]:
    """Produce presentation tuples (label, display_path, exists) for binding entries."""
    return tuple(
        (entry.label, display_candidate_path(entry, home), entry.exists)
        for entry in binding.entries
    )


def joined_candidate_paths(
    candidate_paths: tuple[tuple[str, str, bool], ...],
) -> str:
    """Format candidate paths into a comma-separated list."""
    if not candidate_paths:
        return "-"
    return ", ".join(path for _, path, _ in candidate_paths)
