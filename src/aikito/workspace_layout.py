"""Required workspace layout and explicit migration from the original files."""

from __future__ import annotations

import hashlib
import json
import re
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .add import validate_resource_name
from .skill_state import WorkspaceWriterLock

LAYOUT_FILE = "layout.toml"
LAYOUT_CONTENT = "version = 2\n"
_LEGACY_FILES = ("agents.toml", "subagents.toml")
_AGENT_HEADER = re.compile(r"(?m)^\[agents\.([a-z0-9][a-z0-9-]*)\][ \t]*$")
_SUBAGENT_HEADER = re.compile(r"(?m)^\[subagents\.([a-z0-9][a-z0-9-]*)\][ \t]*$")


class WorkspaceLayoutError(ValueError):
    """The workspace needs migration or has an invalid resource layout."""


def migration_path_policy():
    """Register migration-only files with the shared transaction engine."""
    from .workspace_core import PathPolicy

    return PathPolicy(
        resources=(
            ("legacy", "agents.toml"),
            ("legacy", "subagents.toml"),
            ("layout", "layout.toml"),
        )
    )


@dataclass(frozen=True)
class MigrationPlan:
    root: Path
    creates: tuple[tuple[str, str], ...]
    updates: tuple[tuple[str, str], ...]
    removes: tuple[str, ...]
    findings: tuple[str, ...]
    notes: tuple[str, ...] = ()
    marker_content: str = LAYOUT_CONTENT

    @property
    def blocked(self) -> bool:
        return bool(self.findings)


def _marker_version(root: Path) -> int | None:
    path = root / LAYOUT_FILE
    if not path.exists() and not path.is_symlink():
        return None
    if not path.is_file() or path.is_symlink():
        raise WorkspaceLayoutError(f"Unsafe workspace layout marker: {path}")
    try:
        document = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise WorkspaceLayoutError(f"Invalid workspace layout marker: {path}") from exc
    if set(document) != {"version"} or type(document["version"]) is not int:
        raise WorkspaceLayoutError(f"Invalid workspace layout marker: {path}")
    return document["version"]


def require_current_layout(root: Path) -> None:
    """Reject legacy or incomplete workspaces before normal operations."""
    from .workspace_core import WorkspaceCoreError, pending_kinds

    try:
        if "layout" in pending_kinds((root,)):
            raise WorkspaceLayoutError(
                f"Workspace migration is incomplete: {root}\n"
                "Run: aikito migrate workspace-resources"
            )
    except WorkspaceCoreError as exc:
        raise WorkspaceLayoutError(
            f"Unsafe workspace transaction state: {exc}"
        ) from exc
    version = _marker_version(root)
    legacy = [
        name
        for name in _LEGACY_FILES
        if (root / name).exists() or (root / name).is_symlink()
    ]
    if version == 2 and not legacy:
        for name in ("agents", "subagents"):
            directory = root / name
            if not directory.is_dir() or directory.is_symlink():
                raise WorkspaceLayoutError(
                    f"Workspace resource directory missing or unsafe: {directory}"
                )
        return
    command = "aikito migrate workspace-resources"
    if version is None or legacy:
        raise WorkspaceLayoutError(
            f"Workspace needs migration: {root}\n"
            f"Run: {command} --dry-run\nThen: {command}"
        )
    raise WorkspaceLayoutError(
        f"Unsupported workspace layout version {version}: {root}"
    )


def load_agent_document(root: Path) -> dict[str, Any]:
    """Load only per-Agent TOML files in a migrated workspace."""
    require_current_layout(root)
    return _read_agent_files(root)


def _read_agent_files(root: Path) -> dict[str, Any]:
    directory = root / "agents"
    if not directory.is_dir() or directory.is_symlink():
        raise WorkspaceLayoutError(f"Agents directory missing or unsafe: {directory}")
    agents: dict[str, Any] = {}
    for path in sorted(directory.iterdir()):
        if path.name in (".DS_Store", "Thumbs.db", "desktop.ini"):
            continue
        if path.suffix != ".toml" or validate_resource_name(path.stem, "agent"):
            raise WorkspaceLayoutError(f"Unsupported Agent entry: {path}")
        if not path.is_file() or path.is_symlink():
            raise WorkspaceLayoutError(f"Unsafe Agent entry: {path}")
        try:
            document = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
            raise WorkspaceLayoutError(f"Invalid Agent file: {path}") from exc
        table = document.get("agents")
        if (
            set(document) != {"agents"}
            or not isinstance(table, dict)
            or set(table) != {path.stem}
            or not isinstance(table[path.stem], dict)
        ):
            raise WorkspaceLayoutError(
                f"Agent file must contain only [agents.{path.stem}]: {path}"
            )
        agents[path.stem] = table[path.stem]
    return {"agents": agents}


def _parse_subagent_text(path: Path, content: str) -> tuple[dict[str, Any], str]:
    def reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise WorkspaceLayoutError(f"Duplicate subagent object key: {path}")
            value[key] = item
        return value

    def reject_constant(value: str) -> None:
        raise WorkspaceLayoutError(f"Unsupported subagent value {value}: {path}")

    lines = content.splitlines(keepends=True)
    if not lines or lines[0].strip() != "---":
        raise WorkspaceLayoutError(f"Subagent frontmatter missing: {path}")
    closing = next(
        (index for index, line in enumerate(lines[1:], 1) if line.strip() == "---"),
        None,
    )
    if closing is None:
        raise WorkspaceLayoutError(f"Subagent frontmatter is incomplete: {path}")
    metadata: dict[str, Any] = {}
    for line in lines[1:closing]:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise WorkspaceLayoutError(f"Invalid subagent metadata: {path}")
        key, raw = line.split(":", 1)
        key = key.strip()
        if not re.fullmatch(r"[a-z][a-z0-9-]*", key) or key in metadata:
            raise WorkspaceLayoutError(f"Invalid or duplicate subagent key: {path}")
        try:
            metadata[key] = json.loads(
                raw.strip(),
                object_pairs_hook=reject_duplicate_pairs,
                parse_constant=reject_constant,
            )
        except json.JSONDecodeError as exc:
            raise WorkspaceLayoutError(
                f"Invalid subagent value for {key}: {path}"
            ) from exc
    if (
        not isinstance(metadata.get("description"), str)
        or not metadata["description"].strip()
    ):
        raise WorkspaceLayoutError(f"Subagent description missing: {path}")
    agents = metadata.get("agents")
    if (
        not isinstance(agents, list)
        or not agents
        or any(
            not isinstance(name, str) or validate_resource_name(name, "agent")
            for name in agents
        )
        or len(set(agents)) != len(agents)
    ):
        raise WorkspaceLayoutError(f"Subagent agents list invalid: {path}")
    from .subagent import KNOWN_PLATFORM_FIELDS

    if any(
        key not in ("description", "agents")
        and (key not in KNOWN_PLATFORM_FIELDS or not isinstance(value, dict))
        for key, value in metadata.items()
    ):
        raise WorkspaceLayoutError(f"Subagent platform config invalid: {path}")
    body = "".join(lines[closing + 1 :])
    if not body.strip():
        raise WorkspaceLayoutError(f"Subagent instructions missing: {path}")
    return metadata, body


def parse_subagent_file(path: Path) -> tuple[dict[str, Any], str]:
    """Parse the strict JSON-valued frontmatter used by canonical subagents."""
    if not path.is_file() or path.is_symlink():
        raise WorkspaceLayoutError(f"Unsafe subagent file: {path}")
    with path.open("r", encoding="utf-8", newline="") as stream:
        return _parse_subagent_text(path, stream.read())


def _resource_sections(
    content: str, header_pattern: re.Pattern[str]
) -> list[tuple[str, str, str]]:
    """Assign each contiguous pre-table comment block to the following resource."""
    headers = list(header_pattern.finditer(content))
    starts: list[int] = []
    for match in headers:
        before = content[: match.start()].splitlines(keepends=True)
        start = match.start()
        has_comment = False
        for line in reversed(before):
            if line.lstrip().startswith("#"):
                has_comment = True
            elif line.strip() or has_comment:
                break
            start -= len(line)
        starts.append(start if has_comment else match.start())
    return [
        (
            match.group(1),
            content[start : match.start()],
            content[
                match.start() : starts[index + 1]
                if index + 1 < len(starts)
                else len(content)
            ],
        )
        for index, (match, start) in enumerate(zip(headers, starts))
    ]


def _standalone_comments(content: str) -> str:
    return "".join(
        line
        for line in content.splitlines(keepends=True)
        if line.lstrip().startswith("#")
    )


def _split_agent_text(content: str, expected: dict[str, Any]) -> dict[str, str]:
    sections = _resource_sections(content, _AGENT_HEADER)
    fragments: dict[str, str] = {}
    if any(validate_resource_name(name, "agent") for name in expected):
        raise WorkspaceLayoutError("Unsupported Agent name in agents.toml")
    for name, comments, table_text in sections:
        if name in fragments:
            raise WorkspaceLayoutError(f"Duplicate Agent table: {name}")
        fragment = (comments + table_text).strip() + "\n"
        parsed = tomllib.loads(fragment).get("agents", {})
        if set(parsed) != {name} or parsed[name] != expected.get(name):
            raise WorkspaceLayoutError(f"Cannot preserve Agent table text: {name}")
        fragments[name] = fragment
    if set(fragments) != set(expected):
        raise WorkspaceLayoutError("Unsupported Agent table header in agents.toml")
    return fragments


def render_subagent_text(
    metadata: dict[str, Any], body: str, comments: str = ""
) -> str:
    lines = ["---\n", comments]
    for key in (
        "description",
        "agents",
        *sorted(set(metadata) - {"description", "agents"}),
    ):
        lines.append(
            f"{key}: {json.dumps(metadata[key], ensure_ascii=False, sort_keys=True)}\n"
        )
    lines.append("---\n")
    lines.append(body)
    return "".join(lines)


def build_migration_plan(root: Path) -> MigrationPlan:
    """Read the legacy layout and report all target collisions before writing."""
    root = root.expanduser().resolve()
    version = _marker_version(root)
    legacy_paths = tuple(
        name
        for name in _LEGACY_FILES
        if (root / name).exists() or (root / name).is_symlink()
    )
    if version == 2 and not legacy_paths:
        findings = tuple(
            f"Workspace resource directory missing or unsafe: {root / name}"
            for name in ("agents", "subagents")
            if not (root / name).is_dir() or (root / name).is_symlink()
        )
        return MigrationPlan(root, (), (), (), findings)
    if version is not None:
        return MigrationPlan(
            root, (), (), (), (f"Unsupported or incomplete layout marker: {version}",)
        )
    if len(legacy_paths) != 2:
        return MigrationPlan(
            root, (), (), (), ("Both legacy configuration files are required",)
        )
    findings: list[str] = []
    notes: list[str] = []
    marker_content = LAYOUT_CONTENT
    creates: list[tuple[str, str]] = []
    updates: list[tuple[str, str]] = []
    from .subagent import SubagentConfigError, validate_platform_opts

    for name in legacy_paths:
        path = root / name
        if not path.is_file() or path.is_symlink():
            findings.append(f"Unsafe legacy file: {name}")
    agents_dir = root / "agents"
    if agents_dir.exists() and (not agents_dir.is_dir() or agents_dir.is_symlink()):
        findings.append("Unsafe Agent directory: agents")
    elif agents_dir.is_dir():
        for path in agents_dir.iterdir():
            if path.name not in (".DS_Store", "Thumbs.db", "desktop.ini"):
                findings.append(f"Target already exists: agents/{path.name}")
    try:
        agents_text = (root / "agents.toml").read_text(encoding="utf-8")
        agents_document = tomllib.loads(agents_text)
        agents = agents_document.get("agents")
        if set(agents_document) != {"agents"} or not isinstance(agents, dict):
            raise WorkspaceLayoutError("Invalid legacy Agent registry")
        for name, fragment in _split_agent_text(agents_text, agents).items():
            relative = f"agents/{name}.toml"
            if not (root / relative).exists() and not (root / relative).is_symlink():
                creates.append((relative, fragment))
        if not agents and "#" in agents_text:
            notes.append("Comments in empty agents.toml cannot be attached to an Agent")
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        WorkspaceLayoutError,
    ) as exc:
        findings.append(f"agents.toml: {exc}")
    try:
        subagents_text = (root / "subagents.toml").read_text(encoding="utf-8")
        subagents_document = tomllib.loads(subagents_text)
        table = subagents_document.get("subagents")
        if set(subagents_document) != {"subagents"} or not isinstance(table, dict):
            raise WorkspaceLayoutError("Invalid legacy subagent registry")
        sections = _resource_sections(subagents_text, _SUBAGENT_HEADER)
        first_header = _SUBAGENT_HEADER.search(subagents_text)
        section_start = (
            first_header.start() - len(sections[0][1])
            if first_header and sections
            else len(subagents_text)
        )
        prefix_comments = _standalone_comments(subagents_text[:section_start])
        comments_by_name = {
            name: comments + _standalone_comments(table_text)
            for name, comments, table_text in sections
        }
        if len(comments_by_name) != len(sections) or set(comments_by_name) != set(
            table
        ):
            raise WorkspaceLayoutError("Unsupported subagent table header")
        if sections:
            first_name = sections[0][0]
            comments_by_name[first_name] = (
                prefix_comments + comments_by_name[first_name]
            )
        elif prefix_comments:
            marker_content += prefix_comments
            notes.append("Subagent registry comments preserved in layout.toml")
        for name, metadata in sorted(table.items()):
            if validate_resource_name(name, "subagent") or not isinstance(
                metadata, dict
            ):
                findings.append(f"Invalid legacy subagent: {name}")
                continue
            path = root / "subagents" / f"{name}.md"
            if not path.is_file() or path.is_symlink():
                findings.append(f"Missing or unsafe subagent instructions: {path}")
                continue
            with path.open("r", encoding="utf-8", newline="") as stream:
                body = stream.read()
            new_text = render_subagent_text(
                metadata, body, comments_by_name.get(name, "")
            )
            parsed, parsed_body = _parse_subagent_text(path, new_text)
            if parsed != metadata or parsed_body != body:
                findings.append(f"Subagent cannot round-trip: {name}")
            else:
                updates.append((f"subagents/{name}.md", new_text))
        for name, metadata in table.items():
            if isinstance(metadata, dict):
                for platform, options in metadata.items():
                    if platform not in ("description", "agents"):
                        validate_platform_opts(platform, name, options)
        for path in (root / "subagents").iterdir():
            if path.name not in (".DS_Store", "Thumbs.db", "desktop.ini") and (
                path.suffix != ".md" or path.stem not in table
            ):
                findings.append(f"Unregistered subagent entry: {path}")
    except (
        OSError,
        UnicodeDecodeError,
        tomllib.TOMLDecodeError,
        WorkspaceLayoutError,
        SubagentConfigError,
        ValueError,
        TypeError,
    ) as exc:
        findings.append(f"subagents.toml: {exc}")
    if (root / "layout.toml").exists() or (root / "layout.toml").is_symlink():
        findings.append("Target already exists: layout.toml")
    return MigrationPlan(
        root,
        tuple(creates),
        tuple(updates),
        legacy_paths,
        tuple(findings),
        tuple(notes),
        marker_content,
    )


def apply_migration(plan: MigrationPlan, home: Path) -> None:
    """Apply a fresh migration plan through the shared workspace journal."""
    from .workspace_core import Change, WorkspaceCoreError, apply, recover, version_at

    policy = migration_path_policy()
    with WorkspaceWriterLock(home):
        if recover((plan.root,), policy=policy):
            raise WorkspaceLayoutError("Recovered an interrupted migration; run again")
        fresh = build_migration_plan(plan.root)
        if fresh != plan:
            raise WorkspaceLayoutError("Workspace changed after migration planning")
        if plan.blocked:
            raise WorkspaceLayoutError("Migration has blockers; no files changed")
        if not plan.removes:
            return
        agents_dir = plan.root / "agents"
        if agents_dir.exists() and (not agents_dir.is_dir() or agents_dir.is_symlink()):
            raise WorkspaceLayoutError(f"Unsafe Agent directory: {agents_dir}")
        agents_dir.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="aikito-layout-") as temporary:
            stage = Path(temporary)
            changes: list[Change] = []
            for relative, content in (
                *plan.creates,
                *plan.updates,
                ("layout.toml", plan.marker_content),
            ):
                source = stage / relative
                source.parent.mkdir(parents=True, exist_ok=True)
                with source.open("w", encoding="utf-8", newline="") as stream:
                    stream.write(content)
                kind = (
                    "layout"
                    if relative == "layout.toml"
                    else "agent"
                    if relative.startswith("agents/")
                    else "subagent"
                )
                before = version_at(plan.root, relative, kind, policy)
                changes.append(
                    Change(
                        0,
                        relative,
                        kind,
                        source,
                        before.fingerprint if before else None,
                        hashlib.sha256(source.read_bytes()).hexdigest(),
                    )
                )
            marker = changes.pop()
            for relative in plan.removes:
                before = version_at(plan.root, relative, "legacy", policy)
                assert before is not None
                changes.append(
                    Change(0, relative, "legacy", None, before.fingerprint, None)
                )
            changes.append(marker)

            def verify() -> None:
                _read_agent_files(plan.root)
                from .subagent import validate_platform_opts

                for path in (plan.root / "subagents").iterdir():
                    if path.name in (".DS_Store", "Thumbs.db", "desktop.ini"):
                        continue
                    if path.suffix != ".md" or validate_resource_name(
                        path.stem, "subagent"
                    ):
                        raise WorkspaceLayoutError(
                            f"Unsupported subagent entry: {path}"
                        )
                    metadata, _ = parse_subagent_file(path)
                    for platform, options in metadata.items():
                        if platform not in ("description", "agents"):
                            validate_platform_opts(platform, path.stem, options)
                if any((plan.root / name).exists() for name in _LEGACY_FILES):
                    raise WorkspaceLayoutError("Legacy files remain after migration")

            try:
                apply((plan.root,), tuple(changes), verify=verify, policy=policy)
            except WorkspaceCoreError as exc:
                raise WorkspaceLayoutError(str(exc)) from exc
