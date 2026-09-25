"""Shared comparison and crash recovery for workspace resource changes.

Callers choose which changes are allowed. This module owns the filesystem
transaction: staged copies, a durable journal, replacement, and rollback.
Every root is locked by the caller before applying or recovering a transaction.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .add import validate_resource_name
from .compat import (
    is_reparse_point,
    secure_directory_permissions,
    secure_file_permissions,
)
from .init import _validate_project_name, is_recognized_workspace
from .memory import validate_memory_name
from .templating import BUNDLED_SKILL_NAMES
from .workspace_resources import (
    WorkspaceResourceError,
    fingerprint_resource,
    is_ignored_name,
    snapshot_workspace,
)


class WorkspaceCoreError(ValueError):
    """A resource or transaction cannot be handled safely."""


@dataclass(frozen=True)
class Version:
    kind: str
    fingerprint: str


@dataclass(frozen=True)
class Decision:
    path: str
    kind: str
    action: str
    target: str | None
    reason: str


@dataclass(frozen=True)
class Change:
    target: int
    path: str
    kind: str
    source: Path | None
    before: str | None
    after: str | None


@dataclass(frozen=True)
class StateUpdate:
    target: int
    path: str
    before: str | None
    after: str


@dataclass(frozen=True)
class PathPolicy:
    """Caller-owned resource and state paths admitted to a transaction."""

    resources: tuple[tuple[str, str], ...] = ()
    states: tuple[str, ...] = ()


def validate_roots(left: Path, right: Path) -> tuple[Path, Path]:
    left, right = left.expanduser().resolve(), right.expanduser().resolve()
    if not is_recognized_workspace(left) or not is_recognized_workspace(right):
        raise WorkspaceCoreError("Both paths must be Aikito workspaces")
    if left == right or left in right.parents or right in left.parents:
        raise WorkspaceCoreError("Workspaces must be separate")
    return left, right


def validate_resource_path(
    path: str, kind: str, policy: PathPolicy = PathPolicy()
) -> Path:
    relative = Path(path)
    parts = relative.parts
    if (
        not path
        or relative.is_absolute()
        or "\\" in path
        or any(part in (".", "..") for part in parts)
    ):
        raise WorkspaceCoreError(f"Unsafe resource path: {path}")
    if (kind, path) in policy.resources:
        return relative
    if kind == "memory" and len(parts) == 3 and parts[:2] == ("memory", "notes"):
        name = parts[2]
    elif (
        kind == "memory"
        and len(parts) == 5
        and parts[0] == "projects"
        and parts[2:4] == ("memory", "notes")
        and not _validate_project_name(parts[1])
    ):
        name = parts[4]
    elif (
        kind == "skill"
        and len(parts) == 2
        and parts[0] == "skills"
        and not validate_resource_name(parts[1], "skill")
        and parts[1] not in BUNDLED_SKILL_NAMES
    ):
        return relative
    elif (
        kind == "agent"
        and len(parts) == 2
        and parts[0] == "agents"
        and relative.suffix == ".toml"
        and not validate_resource_name(relative.stem, "agent")
    ):
        return relative
    elif (
        kind == "subagent"
        and len(parts) == 2
        and parts[0] == "subagents"
        and relative.suffix == ".md"
        and not validate_resource_name(relative.stem, "subagent")
    ):
        return relative
    else:
        raise WorkspaceCoreError(f"Unsupported resource path: {path}")
    if not name.endswith(".md") or validate_memory_name(Path(name).stem):
        raise WorkspaceCoreError(f"Unsafe memory path: {path}")
    return relative


def entry_type(path: Path) -> str:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return "missing"
    except OSError:
        return "unsafe"
    if stat.S_ISLNK(mode) or is_reparse_point(path):
        return "unsafe"
    if stat.S_ISREG(mode):
        return "file"
    if stat.S_ISDIR(mode):
        return "directory"
    return "unsafe"


def require_ancestors(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if entry_type(current) != "directory":
            raise WorkspaceCoreError(f"Unsafe resource parent: {current}")


def version_at(
    root: Path, path: str, kind: str, policy: PathPolicy = PathPolicy()
) -> Version | None:
    relative = validate_resource_path(path, kind, policy)
    require_ancestors(root, relative)
    target = root / relative
    actual = entry_type(target)
    if actual == "missing":
        return None
    if actual != ("directory" if kind == "skill" else "file"):
        raise WorkspaceCoreError(f"Resource type changed: {target}")
    try:
        return Version(kind, fingerprint_resource(target, kind))
    except WorkspaceResourceError as exc:
        raise WorkspaceCoreError(str(exc)) from exc


def read_supported_snapshot(
    root: Path,
) -> tuple[dict[str, Version], tuple[str, ...], tuple[str, ...]]:
    """Read one workspace and return all scan findings alongside its resources."""
    snapshot = snapshot_workspace(root)
    result: dict[str, Version] = {}
    findings = [
        f"{finding.resource}: {finding.message}" for finding in snapshot.findings
    ]
    for resource in snapshot.resources.values():
        if resource.kind in ("memory", "project-memory", "skill"):
            part = resource.parts[0]
            kind = "skill" if resource.kind == "skill" else "memory"
            try:
                validate_resource_path(part.path, kind)
            except WorkspaceCoreError as exc:
                findings.append(str(exc))
                continue
            result[part.path] = Version(kind, resource.fingerprint)
    return result, snapshot.skipped, tuple(findings)


def supported_snapshot(root: Path) -> tuple[dict[str, Version], tuple[str, ...]]:
    """Read supported resources and require a valid single-workspace snapshot."""
    resources, skipped, findings = read_supported_snapshot(root)
    if findings:
        raise WorkspaceCoreError("; ".join(findings))
    return resources, skipped


def compare_versions(
    base: dict[str, Version], left: dict[str, Version], right: dict[str, Version]
) -> tuple[Decision, ...]:
    """Compute deterministic three-way decisions without filesystem access."""
    items = []
    for path in sorted(base.keys() | left.keys() | right.keys()):
        ancestor, a, b = base.get(path), left.get(path), right.get(path)
        reference = ancestor or a or b
        assert reference is not None
        kind = reference.kind
        if any(v is not None and v.kind != kind for v in (ancestor, a, b)):
            items.append(
                Decision(path, kind, "CONFLICT", None, "Resource type differs")
            )
        elif a == b:
            items.append(Decision(path, kind, "NOOP", None, "Both sides agree"))
        elif a == ancestor:
            items.append(
                Decision(
                    path,
                    kind,
                    "DELETE" if b is None else "COPY",
                    "left",
                    "Right changed",
                )
            )
        elif b == ancestor:
            items.append(
                Decision(
                    path,
                    kind,
                    "DELETE" if a is None else "COPY",
                    "right",
                    "Left changed",
                )
            )
        else:
            items.append(Decision(path, kind, "CONFLICT", None, "Both sides changed"))
    return tuple(items)


def compare_import(
    source: dict[str, Version], target: dict[str, Version]
) -> tuple[Decision, ...]:
    """Plan additive changes, leaving differing target resources untouched."""
    items = []
    for path, version in sorted(source.items()):
        current = target.get(path)
        if current is None:
            items.append(
                Decision(
                    path,
                    version.kind,
                    "CREATE",
                    "target",
                    "Resource is absent from target",
                )
            )
        elif current == version:
            items.append(Decision(path, version.kind, "NOOP", None, "Contents match"))
        else:
            items.append(
                Decision(path, version.kind, "CONFLICT", None, "Contents differ")
            )
    return tuple(items)


def _secure_dir(path: Path) -> None:
    if entry_type(path) == "missing":
        path.mkdir(mode=0o700)
        if not secure_directory_permissions(path):
            raise WorkspaceCoreError(f"Cannot secure directory: {path}")
    elif entry_type(path) != "directory":
        raise WorkspaceCoreError(f"Unsafe directory: {path}")


def _state_dir(root: Path, create: bool) -> Path | None:
    current = root
    for part in (".local", "state", "aikito", "workspace-transactions"):
        current /= part
        if entry_type(current) == "missing" and not create:
            return None
        _secure_dir(current)
    return current


def _fsync_dir(path: Path) -> None:
    if os.name != "nt":
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def atomic_text(path: Path, content: str) -> None:
    """Replace a private state file after syncing its content and directory."""
    _secure_dir(path.parent)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if not secure_file_permissions(temporary):
            raise WorkspaceCoreError(f"Cannot secure state file: {temporary}")
        os.replace(temporary, path)
        _fsync_dir(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_resource(source: Path, dest: Path, kind: str) -> None:
    if entry_type(source) != ("directory" if kind == "skill" else "file"):
        raise WorkspaceCoreError(f"Unsafe source: {source}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind == "skill":
        dest.mkdir()
        for child in sorted(source.iterdir()):
            if is_ignored_name(child.name):
                continue
            _copy_resource(
                child,
                dest / child.name,
                "skill" if entry_type(child) == "directory" else "memory",
            )
    else:
        shutil.copy2(source, dest, follow_symlinks=False)


def _remove_resource(path: Path, kind: str) -> None:
    if kind == "skill":
        shutil.rmtree(path)
    else:
        path.unlink()


def _tx_dir(root: Path, txid: str, create: bool) -> Path:
    state = _state_dir(root, create)
    if state is None:
        raise WorkspaceCoreError(f"Missing transaction state: {root}")
    parent = state / "tx"
    if create:
        _secure_dir(parent)
    elif entry_type(parent) != "directory":
        raise WorkspaceCoreError(f"Unsafe transaction state: {parent}")
    tx = parent / txid
    if create:
        _secure_dir(tx)
    elif entry_type(tx) != "directory":
        raise WorkspaceCoreError(f"Missing transaction: {tx}")
    return tx


def _journal_path(root: Path, create: bool = False) -> Path | None:
    state = _state_dir(root, create)
    return state / "pending.json" if state is not None else None


def _state_path(root: Path, relative: str, policy: PathPolicy) -> Path:
    if relative not in policy.states:
        raise WorkspaceCoreError(f"Unsafe state path: {relative}")
    path = root / relative
    if entry_type(path.parent) != "directory":
        raise WorkspaceCoreError(f"Missing state directory: {path.parent}")
    return path


def _validate_journal(
    data: object, roots: tuple[Path, ...], policy: PathPolicy
) -> dict:
    if (
        not isinstance(data, dict)
        or data.get("version") != 1
        or data.get("roots") != [str(root) for root in roots]
    ):
        raise WorkspaceCoreError("Invalid workspace journal")
    txid = data.get("txid")
    if (
        not isinstance(txid, str)
        or len(txid) != 32
        or any(c not in "0123456789abcdef" for c in txid)
    ):
        raise WorkspaceCoreError("Invalid transaction ID")
    if (
        data.get("phase") not in ("pending", "committed")
        or not isinstance(data.get("changes"), list)
        or not isinstance(data.get("states"), list)
    ):
        raise WorkspaceCoreError("Invalid workspace journal contents")
    for item in data["changes"]:
        if (
            not isinstance(item, dict)
            or type(item.get("target")) is not int
            or not 0 <= item["target"] < len(roots)
        ):
            raise WorkspaceCoreError("Invalid journal target")
        if not isinstance(item.get("path"), str) or item.get("kind") not in (
            "memory",
            "skill",
            "agent",
            "subagent",
            *(kind for kind, _ in policy.resources),
        ):
            raise WorkspaceCoreError("Invalid journal resource")
        validate_resource_path(item["path"], item["kind"], policy)
        for key in ("before", "after"):
            if item.get(key) is not None and (
                not isinstance(item[key], str)
                or len(item[key]) != 64
                or any(c not in "0123456789abcdef" for c in item[key])
            ):
                raise WorkspaceCoreError("Invalid journal fingerprint")
        if item.get("before") is None and item.get("after") is None:
            raise WorkspaceCoreError("Empty journal change")
    for item in data["states"]:
        if (
            not isinstance(item, dict)
            or type(item.get("target")) is not int
            or not 0 <= item["target"] < len(roots)
        ):
            raise WorkspaceCoreError("Invalid journal state target")
        if (
            not isinstance(item.get("path"), str)
            or not isinstance(item.get("after"), str)
            or (item.get("before") is not None and not isinstance(item["before"], str))
        ):
            raise WorkspaceCoreError("Invalid journal state")
        _state_path(roots[item["target"]], item["path"], policy)
    return data


def _read_journal(roots: tuple[Path, ...], policy: PathPolicy) -> dict | None:
    found = []
    for root in roots:
        path = _journal_path(root)
        if path is None or entry_type(path) == "missing":
            continue
        if entry_type(path) != "file":
            raise WorkspaceCoreError(f"Unsafe journal: {path}")
        try:
            found.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise WorkspaceCoreError(f"Cannot read journal: {path}") from exc
    if not found:
        return None
    for item in found:
        _validate_journal(item, roots, policy)
    if any(item != found[0] for item in found):
        # A crash may interrupt the final phase update on one root.
        if any(
            {key: value for key, value in item.items() if key != "phase"}
            != {key: value for key, value in found[0].items() if key != "phase"}
            for item in found
        ):
            raise WorkspaceCoreError("Workspace journals differ")
        found[0]["phase"] = "pending"
    return _validate_journal(found[0], roots, policy)


def has_pending(roots: tuple[Path, ...], *, policy: PathPolicy = PathPolicy()) -> bool:
    """Report an unfinished or not yet cleaned transaction without writing."""
    return _read_journal(roots, policy) is not None


def pending_kinds(roots: tuple[Path, ...]) -> frozenset[str]:
    """Return resource kinds in an unfinished transaction without writing."""
    kinds: set[str] = set()
    for root in roots:
        path = _journal_path(root)
        if path is None or entry_type(path) == "missing":
            continue
        if entry_type(path) != "file":
            raise WorkspaceCoreError(f"Unsafe journal: {path}")
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            changes = data["changes"]
            if not isinstance(changes, list):
                raise ValueError("Invalid journal changes")
            kinds.update(item["kind"] for item in changes)
        except (OSError, KeyError, TypeError, ValueError) as exc:
            raise WorkspaceCoreError(f"Invalid workspace journal: {path}") from exc
    return frozenset(kinds)


def _fingerprint_private(path: Path, kind: str) -> str | None:
    actual = entry_type(path)
    if actual == "missing":
        return None
    if actual != ("directory" if kind == "skill" else "file"):
        raise WorkspaceCoreError(f"Unsafe private resource: {path}")
    try:
        return fingerprint_resource(path, kind)
    except WorkspaceResourceError as exc:
        raise WorkspaceCoreError(str(exc)) from exc


def _cleanup(roots: tuple[Path, ...], txid: str) -> None:
    for root in roots:
        state = _state_dir(root, False)
        if state is not None and entry_type(state / "tx" / txid) != "missing":
            shutil.rmtree(_tx_dir(root, txid, False))
    for root in roots:
        path = _journal_path(root)
        if path is not None:
            path.unlink(missing_ok=True)


def recover(roots: tuple[Path, ...], *, policy: PathPolicy = PathPolicy()) -> bool:
    """Rollback an interrupted transaction; keep externally changed data."""
    data = _read_journal(roots, policy)
    if data is None:
        return False
    txid = data["txid"]
    if data["phase"] == "committed":
        _cleanup(roots, txid)
        return True
    restore = []
    for item in reversed(data["changes"]):
        root, path, kind = roots[item["target"]], item["path"], item["kind"]
        current = version_at(root, path, kind, policy)
        current_fp = current.fingerprint if current else None
        tx = _tx_dir(root, txid, False)
        moved = tx / "moved" / path
        backup = tx / "backup" / path
        before, after = item["before"], item["after"]
        between_moves = (
            current_fp is None
            and before is not None
            and _fingerprint_private(moved, kind) == before
        )
        if current_fp not in (before, after) and not between_moves:
            raise WorkspaceCoreError(
                f"Cannot recover externally changed resource: {root / path}"
            )
        source = moved if _fingerprint_private(moved, kind) == before else backup
        if (
            before is not None
            and current_fp != before
            and _fingerprint_private(source, kind) != before
        ):
            raise WorkspaceCoreError(f"Missing recovery copy: {root / path}")
        restore.append((root, path, kind, current_fp, before, source))
    for item in data["states"]:
        path = _state_path(roots[item["target"]], item["path"], policy)
        if entry_type(path) not in ("file", "missing"):
            raise WorkspaceCoreError(f"Unsafe transaction state: {path}")
        current = (
            path.read_text(encoding="utf-8") if entry_type(path) == "file" else None
        )
        if current not in (item["before"], item["after"]):
            raise WorkspaceCoreError(f"Cannot recover externally changed state: {path}")
    for root, path, kind, current, before, source in restore:
        if current == before:
            continue
        dest = root / path
        if current is not None:
            _remove_resource(dest, kind)
        if before is not None:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if "moved" in source.parts:
                os.replace(source, dest)
            else:
                _copy_resource(source, dest, kind)
    for item in data["states"]:
        path = _state_path(roots[item["target"]], item["path"], policy)
        if item["before"] is None:
            path.unlink(missing_ok=True)
        else:
            atomic_text(path, item["before"])
    data["phase"] = "committed"
    for root in roots:
        journal = _journal_path(root, True)
        assert journal is not None
        atomic_text(journal, json.dumps(data, sort_keys=True))
    _cleanup(roots, txid)
    return True


def apply(
    roots: tuple[Path, ...],
    changes: tuple[Change, ...],
    *,
    states: tuple[StateUpdate, ...] = (),
    verify: Callable[[], None] | None = None,
    policy: PathPolicy = PathPolicy(),
) -> None:
    """Apply validated changes with one journal and rollback protocol."""
    if _read_journal(roots, policy) is not None:
        raise WorkspaceCoreError("Pending transaction needs recovery")
    txid = uuid.uuid4().hex
    entries = []
    try:
        for change in changes:
            validate_resource_path(change.path, change.kind, policy)
            root = roots[change.target]
            current = version_at(root, change.path, change.kind, policy)
            if (current.fingerprint if current else None) != change.before:
                raise WorkspaceCoreError(
                    f"Target changed before staging: {root / change.path}"
                )
            tx = _tx_dir(root, txid, True)
            if change.before is not None:
                backup = tx / "backup" / change.path
                _copy_resource(root / change.path, backup, change.kind)
                if _fingerprint_private(backup, change.kind) != change.before:
                    raise WorkspaceCoreError(
                        f"Target changed during staging: {root / change.path}"
                    )
            if change.after is not None:
                if change.source is None:
                    raise WorkspaceCoreError("Missing source for resource change")
                stage = tx / "stage" / change.path
                _copy_resource(change.source, stage, change.kind)
                if _fingerprint_private(stage, change.kind) != change.after:
                    raise WorkspaceCoreError(
                        f"Source changed during staging: {change.source}"
                    )
            entries.append(
                {
                    "target": change.target,
                    "path": change.path,
                    "kind": change.kind,
                    "before": change.before,
                    "after": change.after,
                }
            )
    except Exception:
        _cleanup(roots, txid)
        raise
    data = {
        "version": 1,
        "roots": [str(root) for root in roots],
        "txid": txid,
        "phase": "pending",
        "changes": entries,
        "states": [vars(item) for item in states],
    }
    try:
        for root in roots:
            journal = _journal_path(root, True)
            assert journal is not None
            atomic_text(journal, json.dumps(data, sort_keys=True))
        for item in entries:
            root, path, kind = roots[item["target"]], item["path"], item["kind"]
            current = version_at(root, path, kind, policy)
            if (current.fingerprint if current else None) != item["before"]:
                raise WorkspaceCoreError(f"Target changed during apply: {root / path}")
            tx = _tx_dir(root, txid, False)
            stage = tx / "stage" / path
            if (
                item["after"] is not None
                and _fingerprint_private(stage, kind) != item["after"]
            ):
                raise WorkspaceCoreError(f"Staged resource changed: {stage}")
            if item["before"] is not None:
                moved = tx / "moved" / path
                moved.parent.mkdir(parents=True, exist_ok=True)
                os.replace(root / path, moved)
            if item["after"] is not None:
                os.replace(stage, root / path)
        if verify is not None:
            verify()
        for item in states:
            path = _state_path(roots[item.target], item.path, policy)
            if entry_type(path) not in ("file", "missing"):
                raise WorkspaceCoreError(f"Unsafe transaction state: {path}")
            current = (
                path.read_text(encoding="utf-8") if entry_type(path) == "file" else None
            )
            if current != item.before:
                raise WorkspaceCoreError(f"State changed during apply: {path}")
            atomic_text(path, item.after)
        data["phase"] = "committed"
        for root in roots:
            journal = _journal_path(root, True)
            assert journal is not None
            atomic_text(journal, json.dumps(data, sort_keys=True))
    except Exception:
        recover(roots, policy=policy)
        raise
    _cleanup(roots, txid)
