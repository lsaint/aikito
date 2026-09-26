"""Logical resource snapshots of one Aikito workspace.

A snapshot identifies canonical resources by kind and name rather than by
file. One resource may span several files (a subagent), live in one TOML table
(an Agent), or be one member of a set field (a selected skill, a project path).
Snapshots only read the given workspace, never follow symbolic links, and
collect every problem as a finding instead of stopping at the first one.
Fingerprints of TOML resources are semantic, so comments and formatting do not
count as changes. Snapshots make no security judgments; callers decide how to
treat :func:`scan_credentials` results.
"""

from __future__ import annotations

import hashlib
import json
import re
import stat
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .add import validate_resource_name
from .compat import is_reparse_point, is_windows
from .config import LEGACY_DEFAULT_INBOX_PATH, get_inbox_path, load_workspace_config
from .diagnostics import Finding
from .init import _validate_project_name, is_recognized_workspace
from .memory import validate_memory_name
from .project_config import get_project_candidate_paths
from .subagent import SUBAGENT_NAME_PATTERN
from .templating import BUNDLED_SKILL_NAMES
from .workspace_layout import (
    WorkspaceLayoutError,
    parse_subagent_file,
    require_current_layout,
)

# Operating-system and interpreter artifacts that never carry resource content.
IGNORED_NAMES = frozenset({".DS_Store", "Thumbs.db", "desktop.ini", "__pycache__"})
# Host-local workspace entries that are neither resources nor worth reporting.
LOCAL_ONLY_NAMES = frozenset({".git", ".local"})

_SECRET_PATTERN = re.compile(
    rb"(?i)(?:api[_-]?key|access[_-]?token|password|client[_-]?secret)"
    rb"\s*[:=]\s*['\"]?[A-Za-z0-9_./+\-=]{16,}"
)
_TOP_LEVEL_FILES = ("layout.toml", "skills.toml", "config.toml")
_TOP_LEVEL_DIRS = (
    "agents",
    "global",
    "memory",
    "projects",
    "skills",
    "subagents",
    "mcps",
)
_PROJECT_SET_FIELDS = ("path", "paths", "skills")


class WorkspaceResourceError(ValueError):
    """The path cannot be read as an Aikito workspace."""


@dataclass(frozen=True)
class ResourcePart:
    """One storage location of a resource; ``table`` selects a TOML table."""

    path: str
    table: str = ""


@dataclass(frozen=True)
class Resource:
    kind: str
    name: str
    fingerprint: str
    parts: tuple[ResourcePart, ...]
    references: tuple[str, ...] = ()

    @property
    def id(self) -> str:
        return resource_id(self.kind, self.name)


@dataclass(frozen=True)
class WorkspaceSnapshot:
    root: Path
    resources: dict[str, Resource]
    findings: tuple[Finding, ...]
    skipped: tuple[str, ...]


def resource_id(kind: str, name: str) -> str:
    return f"{kind}:{name}"


def resource_kind_for_path(path: str, *, inbox_prefix: str = "") -> str | None:
    """Classify a canonical resource path for scanners and transactions."""
    parts = Path(path).parts
    if len(parts) == 1 and parts[0] == "skills.toml":
        return "skills-config"
    if len(parts) == 2:
        area, filename = parts
        stem = Path(filename).stem
        if area == "memory" and filename.endswith(".md"):
            return "memory"
        if area == "skills" and not validate_resource_name(filename, "skill"):
            return "skill" if filename not in BUNDLED_SKILL_NAMES else None
        if (
            area == "agents"
            and filename.endswith(".toml")
            and not validate_resource_name(stem, "agent")
        ):
            return "agent"
        if (
            area == "subagents"
            and filename.endswith(".md")
            and SUBAGENT_NAME_PATTERN.fullmatch(stem)
        ):
            return "subagent"
        if (
            area == "mcps"
            and filename.endswith(".toml")
            and not validate_resource_name(stem, "mcp")
        ):
            return "mcp"
    if len(parts) == 3 and parts[:2] == ("memory", "notes"):
        return (
            "memory"
            if parts[2].endswith(".md")
            and not validate_memory_name(Path(parts[2]).stem)
            else None
        )
    if (
        len(parts) == 3
        and parts[0] == "projects"
        and not _validate_project_name(parts[1])
    ):
        if parts[2] == "agent.toml":
            return "project-config"
        if parts[2] == "AGENTS.md":
            return "project-instructions"
    if (
        len(parts) in (4, 5)
        and parts[0] == "projects"
        and not _validate_project_name(parts[1])
        and parts[2] == "memory"
    ):
        if len(parts) == 4 and parts[3].endswith(".md"):
            return "memory"
        if (
            len(parts) == 5
            and parts[3] == "notes"
            and parts[4].endswith(".md")
            and not validate_memory_name(Path(parts[4]).stem)
        ):
            return "memory"
    if inbox_prefix:
        prefix = Path(inbox_prefix).parts
        if (
            len(parts) > len(prefix)
            and parts[: len(prefix)] == prefix
            and parts[-1].endswith(".md")
        ):
            return "inbox"
    return None


def value_fingerprint(value: Any) -> str:
    """Fingerprint a parsed TOML value independently of its formatting."""
    encoded = json.dumps(value, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def fingerprint_resource(path: Path, kind: str) -> str:
    """Fingerprint a standalone file or skill with snapshot semantics."""
    scanner = _Scanner(path.parent)
    if kind in (
        "memory",
        "project-memory",
        "inbox",
        "project-instructions",
        "agent",
        "subagent",
        "mcp",
        "project-config",
        "skills-config",
        "legacy",
        "layout",
    ):
        result = scanner.file_digest(path)
    elif kind == "skill" and _entry_type(path) == "directory":
        result = scanner.tree_digest(path)
    else:
        result = None
    if result is None or scanner.findings:
        raise WorkspaceResourceError(f"Cannot fingerprint resource: {path}")
    return result


def is_ignored_name(name: str) -> bool:
    return name in IGNORED_NAMES or name.startswith("._") or name.endswith(".pyc")


def _entry_type(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unreadable"
    if stat.S_ISLNK(mode) or is_reparse_point(path):
        return "link"
    if stat.S_ISDIR(mode):
        return "directory"
    if stat.S_ISREG(mode):
        return "file"
    return "special"


class _Scanner:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.resources: dict[str, Resource] = {}
        self.findings: list[Finding] = []
        self.skipped: list[str] = []
        self.inbox_rel = Path()

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def error(self, code: str, message: str, path: Path) -> None:
        self.findings.append(
            Finding("error", message, code=code, resource=self.rel(path))
        )

    def add(
        self,
        kind: str,
        name: str,
        fingerprint: str,
        parts: tuple[ResourcePart, ...],
        references: tuple[str, ...] = (),
    ) -> None:
        physical = {
            "memory": "memory",
            "project-memory": "memory",
            "skill": "skill",
            "inbox": "inbox",
            "agent": "agent",
            "subagent": "subagent",
            "mcp": "mcp",
            "project": "project-config",
            "project-instructions": "project-instructions",
        }.get(kind)
        if (
            physical is not None
            and resource_kind_for_path(
                parts[0].path,
                inbox_prefix=self.inbox_rel.as_posix() if self.inbox_rel.parts else "",
            )
            != physical
        ):
            self.error(
                "unsupported-entry",
                "Unsupported resource path",
                self.root / parts[0].path,
            )
            return
        resource = Resource(kind, name, fingerprint, parts, references)
        self.resources[resource.id] = resource

    def directory(self, path: Path, *, optional: bool = False) -> bool:
        kind = _entry_type(path)
        if kind == "directory":
            return True
        if kind == "missing" and optional:
            return False
        self.error("unsafe-entry", f"Expected a real directory: {kind}", path)
        return False

    def children(self, directory: Path) -> list[Path]:
        try:
            entries = sorted(directory.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            self.error("unreadable", f"Cannot list directory: {exc}", directory)
            return []
        return [entry for entry in entries if not is_ignored_name(entry.name)]

    def unsupported(self, path: Path) -> None:
        self.error("unsupported-entry", "Unsupported entry in a managed area", path)

    def read(self, path: Path) -> bytes | None:
        kind = _entry_type(path)
        if kind != "file":
            self.error("unsafe-entry", f"Expected a regular file: {kind}", path)
            return None
        try:
            content = path.read_bytes()
        except OSError as exc:
            self.error("unreadable", f"Cannot read file: {exc}", path)
            return None
        return content

    def file_digest(self, path: Path) -> str | None:
        content = self.read(path)
        return None if content is None else hashlib.sha256(content).hexdigest()

    def toml(self, path: Path) -> dict[str, Any] | None:
        content = self.read(path)
        if content is None:
            return None
        try:
            return tomllib.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            self.error("invalid-toml", f"Invalid TOML: {exc}", path)
            return None

    def tree_digest(self, directory: Path) -> str | None:
        entries: list[str] = []
        before = len(self.findings)
        self._collect_tree(directory, directory, entries)
        if len(self.findings) != before:
            return None
        raw = "\n".join(sorted(entries)).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _collect_tree(self, base: Path, directory: Path, entries: list[str]) -> None:
        children = self.children(directory)
        if not children and directory != base:
            entries.append(f"d {directory.relative_to(base).as_posix()}")
        for child in children:
            kind = _entry_type(child)
            if kind == "directory":
                self._collect_tree(base, child, entries)
                continue
            digest = self.file_digest(child)
            if digest is None:
                continue
            executable = not is_windows() and bool(
                child.lstat().st_mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
            )
            rel = child.relative_to(base).as_posix()
            entries.append(f"f {rel} {digest}{' *' if executable else ''}")


def _scan_markdown_file(scanner: _Scanner, path: Path, kind: str, name: str) -> None:
    digest = scanner.file_digest(path)
    if digest is not None:
        scanner.add(kind, name, digest, (ResourcePart(scanner.rel(path)),))


def _scan_memory(scanner: _Scanner, memory: Path, kind: str, prefix: str) -> None:
    if not scanner.directory(memory, optional=True):
        return
    for entry in scanner.children(memory):
        if entry.name == "notes":
            if not scanner.directory(entry):
                continue
            for note in scanner.children(entry):
                if note.suffix != ".md" or validate_memory_name(note.stem):
                    scanner.unsupported(note)
                    continue
                _scan_markdown_file(scanner, note, kind, f"{prefix}notes/{note.name}")
        elif entry.suffix == ".md":
            _scan_markdown_file(scanner, entry, kind, f"{prefix}{entry.name}")
        else:
            scanner.unsupported(entry)


def _string_members(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = [raw]
    elif isinstance(raw, dict):
        raw = list(raw.values())
    if not isinstance(raw, list):
        return []
    return [item.strip() for item in raw if isinstance(item, str) and item.strip()]


def _project_members(config: dict[str, Any]) -> tuple[list[str], list[str]]:
    paths = [path for _, path in get_project_candidate_paths(config)]
    return sorted(set(paths)), sorted(set(_string_members(config.get("skills"))))


def _scan_project(scanner: _Scanner, project: Path) -> None:
    name = project.name
    if _validate_project_name(name):
        scanner.unsupported(project)
        return
    config_path = project / "agent.toml"
    config = scanner.toml(config_path)
    if config is not None:
        if (
            (
                "path" in config
                and not (
                    isinstance(config["path"], str)
                    or (
                        isinstance(config["path"], list)
                        and all(isinstance(value, str) for value in config["path"])
                    )
                )
            )
            or (
                "paths" in config
                and (
                    not isinstance(config["paths"], (list, dict))
                    or any(
                        not isinstance(value, str)
                        for value in (
                            config["paths"].values()
                            if isinstance(config["paths"], dict)
                            else config["paths"]
                        )
                    )
                )
            )
            or (
                "skills" in config
                and (
                    not isinstance(config["skills"], list)
                    or any(not isinstance(value, str) for value in config["skills"])
                )
            )
        ):
            scanner.error(
                "invalid-project-field",
                "Invalid project path or skills collection",
                config_path,
            )
        part = (ResourcePart(scanner.rel(config_path)),)
        fields = {
            key: val for key, val in config.items() if key not in _PROJECT_SET_FIELDS
        }
        scanner.add("project", name, value_fingerprint(fields), part)
        paths, skills = _project_members(config)
        for path in paths:
            scanner.add(
                "project-path", f"{name}/{path}", "", part, (f"project:{name}",)
            )
        for skill in skills:
            references = (f"project:{name}",)
            if skill not in BUNDLED_SKILL_NAMES:
                references += (resource_id("skill", skill),)
            scanner.add("project-skill", f"{name}/{skill}", "", part, references)
    for entry in scanner.children(project):
        if entry.name == "agent.toml":
            continue
        if entry.name == "AGENTS.md":
            _scan_markdown_file(scanner, entry, "project-instructions", name)
        elif entry.name == "memory":
            _scan_memory(scanner, entry, "project-memory", f"{name}/")
        else:
            scanner.unsupported(entry)


def _scan_skills(scanner: _Scanner, skills: Path) -> None:
    for skill in scanner.children(skills):
        if skill.name in BUNDLED_SKILL_NAMES:
            scanner.skipped.append(scanner.rel(skill))
            continue
        if validate_resource_name(skill.name, "skill") or not scanner.directory(skill):
            if _entry_type(skill) == "directory":
                scanner.unsupported(skill)
            continue
        if _entry_type(skill / "SKILL.md") != "file":
            scanner.error("invalid-skill", "Skill has no regular SKILL.md", skill)
            continue
        digest = scanner.tree_digest(skill)
        if digest is not None:
            scanner.add(
                "skill", skill.name, digest, (ResourcePart(scanner.rel(skill)),)
            )


def _agent_references(table: dict[str, Any]) -> tuple[str, ...]:
    agents = table.get("agents")
    if not isinstance(agents, list):
        return ()
    return tuple(resource_id("agent", name) for name in agents if isinstance(name, str))


def _scan_subagents(scanner: _Scanner, directory: Path) -> None:
    if not scanner.directory(directory):
        return
    for path in scanner.children(directory):
        if path.suffix != ".md" or not SUBAGENT_NAME_PATTERN.fullmatch(path.stem):
            scanner.unsupported(path)
            continue
        try:
            metadata, body = parse_subagent_file(path)
        except WorkspaceLayoutError as exc:
            scanner.error("invalid-subagent", str(exc), path)
            continue
        fingerprint = value_fingerprint(
            {
                "instructions": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                "table": metadata,
            }
        )
        scanner.add(
            "subagent",
            path.stem,
            fingerprint,
            (ResourcePart(scanner.rel(path)),),
            _agent_references(metadata),
        )


def _scan_mcps(scanner: _Scanner, directory: Path) -> None:
    if not scanner.directory(directory):
        return
    for entry in scanner.children(directory):
        if entry.suffix != ".toml" or validate_resource_name(entry.stem, "mcp"):
            scanner.unsupported(entry)
            continue
        document = scanner.toml(entry)
        if document is not None:
            scanner.add(
                "mcp",
                entry.stem,
                value_fingerprint(document),
                (ResourcePart(scanner.rel(entry)),),
                _agent_references(document),
            )


def _scan_tables(
    scanner: _Scanner, filename: str, document: dict | None, allowed: set[str]
) -> None:
    for key in sorted((document or {}).keys() - allowed):
        scanner.error(
            "unsupported-entry",
            f"Unsupported top-level key '{key}'",
            scanner.root / filename,
        )


def _flatten(prefix: str, value: Any) -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        items: list[tuple[str, Any]] = []
        for key in sorted(value):
            items.extend(_flatten(f"{prefix}.{key}" if prefix else key, value[key]))
        return items
    return [(prefix, value)]


def _scan_top_level_toml(scanner: _Scanner) -> None:
    root = scanner.root
    agent_dir = root / "agents"
    if scanner.directory(agent_dir):
        for path in scanner.children(agent_dir):
            if path.suffix != ".toml" or validate_resource_name(path.stem, "agent"):
                scanner.unsupported(path)
                continue
            document = scanner.toml(path)
            table = (document or {}).get("agents")
            if document is None:
                continue
            if (
                set(document) != {"agents"}
                or not isinstance(table, dict)
                or set(table) != {path.stem}
                or not isinstance(table[path.stem], dict)
            ):
                scanner.error(
                    "invalid-agent",
                    "Agent file must define its matching table only",
                    path,
                )
                continue
            scanner.add(
                "agent",
                path.stem,
                value_fingerprint(table[path.stem]),
                (ResourcePart(scanner.rel(path)),),
            )

    skills = scanner.toml(root / "skills.toml")
    _scan_tables(scanner, "skills.toml", skills, {"skills"})
    selected = (skills or {}).get("skills", [])
    if not isinstance(selected, list) or any(
        not isinstance(item, str) for item in selected
    ):
        scanner.error(
            "invalid-skill-selection",
            "Skills must be a list of names",
            root / "skills.toml",
        )
        selected = []
    for name in sorted({item for item in selected if isinstance(item, str)}):
        references = (
            () if name in BUNDLED_SKILL_NAMES else (resource_id("skill", name),)
        )
        scanner.add(
            "skill-selection", name, "", (ResourcePart("skills.toml"),), references
        )

    if _entry_type(root / "config.toml") != "missing":
        config = scanner.toml(root / "config.toml")
        for key, value in _flatten("", config or {}):
            part = (
                ResourcePart(
                    "config.toml", key.rsplit(".", 1)[0] if "." in key else ""
                ),
            )
            scanner.add("config", key, value_fingerprint(value), part)


def _inbox_directory(scanner: _Scanner) -> Path | None:
    root = scanner.root
    raw = load_workspace_config(root).inbox.path.strip() or "inbox"
    configured = Path(raw).expanduser()
    if configured == Path(LEGACY_DEFAULT_INBOX_PATH).expanduser():
        configured = root / "inbox"
    elif not configured.is_absolute():
        configured = root / configured
    try:
        configured_parts = configured.relative_to(root).parts
    except ValueError:
        return None
    if ".." in configured_parts:
        return None
    current = root
    for part in configured_parts:
        current /= part
        if _entry_type(current) == "link":
            scanner.error("unsafe-entry", "Inbox path crosses a symbolic link", current)
            return None
    inbox = get_inbox_path(root)
    try:
        inbox.relative_to(root.resolve())
    except ValueError:
        return None
    return root / inbox.relative_to(root.resolve())


def _scan_inbox(scanner: _Scanner, inbox: Path) -> None:
    if not scanner.directory(inbox, optional=True):
        return
    for entry in scanner.children(inbox):
        if entry.name.startswith("."):
            continue
        if _entry_type(entry) == "directory":
            _scan_inbox(scanner, entry)
        elif entry.suffix == ".md":
            name = entry.relative_to(scanner.root / scanner.inbox_rel).as_posix()
            _scan_markdown_file(scanner, entry, "inbox", name)
        elif _entry_type(entry) == "file":
            scanner.skipped.append(scanner.rel(entry))
        else:
            scanner.error("unsafe-entry", "Unsupported inbox entry", entry)


def snapshot_workspace(root: Path) -> WorkspaceSnapshot:
    """Read every canonical resource of one workspace without writing."""
    root = root.expanduser().resolve()
    if not is_recognized_workspace(root):
        raise WorkspaceResourceError(f"Not an Aikito workspace: {root}")
    try:
        require_current_layout(root)
    except WorkspaceLayoutError as exc:
        raise WorkspaceResourceError(str(exc)) from exc
    scanner = _Scanner(root)
    _scan_top_level_toml(scanner)

    managed = set(_TOP_LEVEL_FILES) | set(_TOP_LEVEL_DIRS)
    inbox = _inbox_directory(scanner)
    if inbox is None:
        scanner.skipped.append("inbox (outside the workspace)")
    elif inbox != root:
        scanner.inbox_rel = Path(scanner.rel(inbox))
        managed.add(scanner.inbox_rel.parts[0])
        _scan_inbox(scanner, inbox)

    for entry in scanner.children(root):
        if entry.name not in managed and entry.name not in LOCAL_ONLY_NAMES:
            scanner.skipped.append(entry.name)

    global_dir = root / "global"
    if scanner.directory(global_dir):
        for entry in scanner.children(global_dir):
            if entry.name == "AGENTS.md":
                _scan_markdown_file(scanner, entry, "global-instructions", "AGENTS.md")
            else:
                scanner.unsupported(entry)
    _scan_memory(scanner, root / "memory", "memory", "")
    if scanner.directory(root / "projects"):
        for project in scanner.children(root / "projects"):
            if scanner.directory(project):
                _scan_project(scanner, project)
    if scanner.directory(root / "skills"):
        _scan_skills(scanner, root / "skills")
    _scan_subagents(scanner, root / "subagents")
    _scan_mcps(scanner, root / "mcps")

    return WorkspaceSnapshot(
        root,
        dict(sorted(scanner.resources.items())),
        tuple(scanner.findings),
        tuple(sorted(scanner.skipped)),
    )


def _content_files(path: Path) -> list[Path]:
    kind = _entry_type(path)
    if kind == "file":
        return [path]
    if kind != "directory":
        return []
    files: list[Path] = []
    for child in sorted(path.iterdir(), key=lambda item: item.name):
        if not is_ignored_name(child.name):
            files.extend(_content_files(child))
    return files


def scan_credentials(snapshot: WorkspaceSnapshot) -> tuple[Finding, ...]:
    """Report files that match common plaintext credential patterns.

    Pattern matching cannot prove a file is free of secrets and may flag
    examples, so results are warnings reported by path only.
    """
    paths = sorted(
        {part.path for item in snapshot.resources.values() for part in item.parts}
    )
    findings: list[Finding] = []
    for rel in paths:
        for path in _content_files(snapshot.root / rel):
            try:
                content = path.read_bytes()
            except OSError:
                continue
            if _SECRET_PATTERN.search(content):
                findings.append(
                    Finding(
                        "warning",
                        "Possible plaintext credential",
                        code="possible-credential",
                        resource=path.relative_to(snapshot.root).as_posix(),
                    )
                )
    return tuple(findings)
