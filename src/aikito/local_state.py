"""Inspect host-local skill state and approval; remove proven obsolete bindings.

Missing paths outside temporary storage may be offline, so they are diagnostic
only. Cleanup rechecks the inventory under the existing writer lock and never
changes state images while a transaction journal is present.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import tomllib
from dataclasses import replace
from pathlib import Path

from .compat import get_physical_path, is_reparse_point, is_windows
from .diagnostics import Finding, FindingAction
from .directory_identity import directory_identity
from .global_approval import global_approval_error
from .init import is_recognized_workspace
from .project_approval import candidate_checkout, checkout_approval_error
from .project_auto_apply import _pending_workspace
from .project_config import get_project_candidate_paths
from .skill_state import (
    WorkspaceWriterLock,
    get_binding_hash,
    validate_state_store_root,
)


def _warning(path: Path, code: str, reason: str) -> Finding:
    return Finding(
        status="WARN",
        code=f"local-state.{code}",
        resource="project-skill-state",
        source=str(path),
        reason=reason,
        message=f"{path}: {reason}",
    )


def _missing(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return True
    return False


def _strip_unc(path: Path) -> Path:
    s = str(path)
    if s.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + s[8:])
    elif s.startswith("\\\\?\\"):
        return Path(s[4:])
    return path


def _resolve_existing_parent(path: Path) -> Path:
    p = _strip_unc(path)
    curr = p
    tail: list[str] = []
    while not curr.exists() and curr != curr.parent:
        tail.append(curr.name)
        curr = curr.parent
    if curr.exists():
        try:
            resolved_curr = _strip_unc(curr.resolve())
            for part in reversed(tail):
                resolved_curr = resolved_curr / part
            return resolved_curr
        except OSError:
            pass
    try:
        return _strip_unc(p.resolve())
    except OSError:
        return p


def _is_subpath(path: Path, root: Path) -> bool:
    try:
        return _strip_unc(path).is_relative_to(_strip_unc(root))
    except (ValueError, AttributeError):
        return False


def _temporary_roots() -> list[Path]:
    roots: list[Path] = [Path(tempfile.gettempdir())]
    if is_windows():
        for env_var in ("TEMP", "TMP", "RUNNER_TEMP", "LOCALAPPDATA"):
            val = os.environ.get(env_var)
            if val:
                p = Path(val)
                roots.append(p)
                if env_var == "LOCALAPPDATA":
                    roots.append(p / "Temp")
        roots.extend((Path("C:/Temp"), Path("C:/Windows/Temp")))
    else:
        for env_var in ("TMPDIR", "RUNNER_TEMP"):
            val = os.environ.get(env_var)
            if val:
                roots.append(Path(val))
        roots.extend(
            (
                Path("/tmp"),
                Path("/var/folders"),
                Path("/private/tmp"),
                Path("/private/var/folders"),
            )
        )
    return roots


def _temporary(path: Path) -> bool:
    roots = _temporary_roots()
    raw_path = _strip_unc(path)
    res_path = _resolve_existing_parent(path)

    for root in roots:
        raw_root = _strip_unc(root)
        try:
            res_root = _strip_unc(root.resolve())
        except OSError:
            res_root = raw_root

        if (
            _is_subpath(res_path, res_root)
            or _is_subpath(raw_path, raw_root)
            or _is_subpath(res_path, raw_root)
            or _is_subpath(raw_path, res_root)
        ):
            return True
    return False


def _matches_binding(ws: Path, proj: str, co: Path, expected: str) -> bool:
    if get_binding_hash(ws, proj, co) == expected:
        return True
    if is_windows():
        folded = (
            f"{ws.as_posix().lower()}:{proj.strip()}:{co.as_posix().lower()}".encode(
                "utf-8"
            )
        )
        if hashlib.sha256(folded).hexdigest() == expected:
            return True
        phys_folded = f"{get_physical_path(ws).as_posix().lower()}:{proj.strip()}:{get_physical_path(co).as_posix().lower()}".encode(
            "utf-8"
        )
        if hashlib.sha256(phys_folded).hexdigest() == expected:
            return True
        non_folded = f"{ws.as_posix()}:{proj.strip()}:{co.as_posix()}".encode("utf-8")
        if hashlib.sha256(non_folded).hexdigest() == expected:
            return True
    return False


def _inspect_file(path: Path, cleanup_allowed: bool) -> Finding | None:
    try:
        if is_reparse_point(path) or not path.is_file():
            raise ValueError("not a regular state file")
        if not re.fullmatch(r"[0-9a-f]{64}\.json", path.name):
            raise ValueError("unrecognized state filename")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] != 1
            or not isinstance(raw.get("records"), dict)
            or any(
                not isinstance(key, str) or not isinstance(value, dict)
                for key, value in raw["records"].items()
            )
        ):
            raise ValueError("invalid state metadata")
        fields = [
            raw.get(key)
            for key in ("workspace_root", "project_name", "physical_checkout")
        ]
        if any(not isinstance(value, str) or not value for value in fields):
            raise ValueError("missing binding identity")
        workspace, checkout = Path(fields[0]), Path(fields[2])
        if not workspace.is_absolute() or not checkout.is_absolute():
            raise ValueError("binding paths must be absolute")
        if not _matches_binding(workspace, fields[1], checkout, path.stem):
            raise ValueError("binding identity does not match state filename")
        workspace_missing, checkout_missing = _missing(workspace), _missing(checkout)
        if not workspace_missing and not checkout_missing:
            return None
        if (
            workspace_missing
            and checkout_missing
            and _temporary(workspace)
            and _temporary(checkout)
        ):
            finding = _warning(
                path, "stale", "temporary workspace and checkout no longer exist"
            )
            if cleanup_allowed:
                return replace(
                    finding,
                    fix_hint="aikito doctor --fix",
                    actions=(FindingAction("Clean up", "aikito doctor --fix"),),
                )
            return finding
        return _warning(
            path, "unavailable", "workspace or checkout is unavailable; state retained"
        )
    except (OSError, ValueError, RuntimeError) as exc:
        return _warning(path, "invalid", f"cannot verify state: {exc}")


def _stale_approval(path: Path, reason: str, cleanup_allowed: bool) -> Finding:
    finding = _warning(path, "stale", reason)
    if not cleanup_allowed:
        return finding
    return replace(
        finding,
        fix_hint="aikito doctor --fix",
        actions=(FindingAction("Clean up", "aikito doctor --fix"),),
    )


def _inspect_approval(path: Path, home: Path, cleanup_allowed: bool) -> Finding | None:
    """Revoke only demonstrably obsolete approval; offline paths remain diagnostic."""
    try:
        if is_reparse_point(path) or not path.is_file():
            raise ValueError("not a regular approval file")
        if path.name.startswith("global-"):
            return _inspect_global_approval(path, home, cleanup_allowed)
        if not re.fullmatch(r"[0-9a-f]{64}\.approval", path.name):
            raise ValueError("unrecognized approval filename")
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (
            not isinstance(raw, dict)
            or type(raw.get("version")) is not int
            or raw["version"] != 1
        ):
            raise ValueError("invalid approval version")
        for key in ("workspace", "project", "checkout"):
            if not isinstance(raw.get(key), str) or not raw[key]:
                raise ValueError("missing approval identity")
        for key in ("device", "inode"):
            if type(raw.get(key)) is not int or raw[key] < (1 if key == "inode" else 0):
                raise ValueError("invalid directory identity")
        workspace, checkout, project = (
            Path(raw["workspace"]),
            Path(raw["checkout"]),
            raw["project"],
        )
        if (
            not workspace.is_absolute()
            or not checkout.is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", project)
        ):
            raise ValueError("invalid approval binding")
        if not _matches_binding(workspace, project, checkout, path.stem):
            raise ValueError("approval identity does not match filename")
        if _missing(workspace):
            if _missing(checkout) and _temporary(workspace) and _temporary(checkout):
                return _stale_approval(
                    path,
                    "temporary workspace and checkout no longer exist",
                    cleanup_allowed,
                )
            return _warning(
                path, "unavailable", "workspace is unavailable; approval retained"
            )
        if is_reparse_point(workspace) or not is_recognized_workspace(workspace):
            return _warning(
                path, "unavailable", "workspace cannot be verified; approval retained"
            )
        project_dir = workspace / "projects" / project
        config = project_dir / "agent.toml"
        if any(
            is_reparse_point(item)
            for item in (workspace / "projects", project_dir, config)
        ):
            raise ValueError("unsafe project configuration path")
        if _missing(config):
            return _stale_approval(
                path, "project is no longer registered", cleanup_allowed
            )
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        for field in ("path", "paths"):
            if field not in data:
                continue
            value = data[field]
            values = [value] if isinstance(value, str) else value
            if field == "paths" and isinstance(values, dict):
                values = list(values.values())
            if not isinstance(values, list) or any(
                not isinstance(item, str) or not item.strip() for item in values
            ):
                raise ValueError("invalid project paths; approval retained")
        candidates = [
            candidate_checkout(raw_path, home).resolve()
            for _, raw_path in get_project_candidate_paths(data)
        ]
        if checkout.resolve() not in candidates:
            return _stale_approval(
                path, "checkout is no longer registered", cleanup_allowed
            )
        if _missing(checkout):
            return _warning(
                path, "unavailable", "checkout is unavailable; approval retained"
            )
        info = checkout.stat()
        if is_reparse_point(checkout):
            raise ValueError("checkout is a symbolic link or reparse point")
        if info.st_dev != raw["device"] or info.st_ino != raw["inode"]:
            return _stale_approval(
                path, "checkout directory identity changed", cleanup_allowed
            )
        error = checkout_approval_error(workspace, home, project, checkout)
        if error:
            raise ValueError(error)
        return None
    except (OSError, ValueError, RuntimeError, KeyError, TypeError) as exc:
        return _warning(path, "invalid", f"cannot verify approval: {exc}")


def _inspect_global_approval(
    path: Path, home: Path, cleanup_allowed: bool
) -> Finding | None:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(raw, dict)
        or type(raw.get("version")) is not int
        or raw["version"] != 1
        or not isinstance(raw.get("workspace"), str)
        or not isinstance(raw.get("path"), str)
        or type(raw.get("device")) is not int
        or type(raw.get("inode")) is not int
        or raw["device"] < 0
        or raw["inode"] < 1
    ):
        raise ValueError("invalid global approval")
    workspace = Path(raw["workspace"])
    container = home / ".agents" / "skills"
    expected = f"global-{get_binding_hash(workspace, 'global', container)}.approval"
    if (
        not workspace.is_absolute()
        or not Path(raw["path"]).is_absolute()
        or path.name != expected
    ):
        raise ValueError("invalid global approval binding")
    if _missing(workspace) and _temporary(workspace):
        return _stale_approval(
            path, "temporary workspace no longer exists", cleanup_allowed
        )
    if not workspace.exists() or not is_recognized_workspace(workspace):
        return _warning(
            path, "unavailable", "workspace cannot be verified; approval retained"
        )
    if not container.exists():
        return _warning(
            path, "unavailable", "skill container is unavailable; approval retained"
        )
    if _pending_workspace(workspace):
        cleanup_allowed = False
    try:
        identity = directory_identity(container)
    except (OSError, ValueError):
        return _warning(
            path,
            "unavailable",
            "container identity cannot be verified; approval retained",
        )
    if any(raw.get(key) != identity[key] for key in ("path", "device", "inode")):
        return _stale_approval(
            path, "global skill container identity changed", cleanup_allowed
        )
    error = global_approval_error(workspace, home, container)
    if error:
        raise ValueError(error)
    return None


def inspect_local_state(home: Path) -> list[Finding]:
    """Inspect all local bindings read-only, independently of the active workspace."""
    state_dir, error = validate_state_store_root(home)
    if error or is_reparse_point(state_dir):
        return [
            _warning(
                state_dir,
                "invalid",
                error or "state directory is a symlink or reparse point",
            )
        ]
    if not state_dir.exists():
        return []
    try:
        transactions = state_dir / "transactions"
        if is_reparse_point(transactions):
            return [
                _warning(
                    transactions,
                    "invalid",
                    "transaction directory is a symlink or reparse point",
                )
            ]
        journals = []
        if transactions.exists():
            for transaction in sorted(transactions.iterdir()):
                if is_reparse_point(transaction) or not transaction.is_dir():
                    raise ValueError(
                        f"unverifiable transaction directory: {transaction}"
                    )
                journal = transaction / "journal.json"
                if not _missing(journal):
                    journals.append(journal)
        findings = [
            _warning(
                path,
                "recovery-required",
                "transaction journal present; local state cleanup deferred",
            )
            for path in journals
        ]
        for path in sorted(state_dir.iterdir()):
            if path.suffix == ".approval":
                finding = _inspect_approval(path, home, cleanup_allowed=not journals)
            elif path.suffix == ".json":
                finding = _inspect_file(path, cleanup_allowed=not journals)
            else:
                continue
            if finding is not None:
                findings.append(finding)
        return findings
    except (OSError, ValueError) as exc:
        return [_warning(state_dir, "invalid", f"cannot inspect local state: {exc}")]


def clean_local_state(home: Path) -> list[str]:
    """Delete only verified stale bindings, rechecking after acquiring the lock."""
    findings = inspect_local_state(home)
    if not any(finding.actions for finding in findings):
        return []
    removed = []
    with WorkspaceWriterLock(home):
        for finding in inspect_local_state(home):
            if finding.code == "local-state.stale" and finding.actions:
                path = Path(finding.source)
                path.unlink()
                kind = (
                    "global approval"
                    if path.name.startswith("global-")
                    else "checkout approval"
                    if path.suffix == ".approval"
                    else "skill state"
                )
                removed.append(f"Removed stale local {kind}: {path}")
    return removed
