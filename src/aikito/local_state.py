"""Inspect host-local skill state and remove proven abandoned temporary bindings.

Missing paths outside temporary storage may be offline, so they are diagnostic
only. Cleanup rechecks the inventory under the existing writer lock and never
changes state images while a transaction journal is present.
"""

from __future__ import annotations

import json
import re
import tempfile
from dataclasses import replace
from pathlib import Path

from .compat import is_reparse_point, is_windows
from .diagnostics import Finding, FindingAction
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


def _temporary(path: Path) -> bool:
    roots = [Path(tempfile.gettempdir())]
    if not is_windows():
        roots.extend((Path("/tmp"), Path("/var/folders")))
    resolved = path.resolve()
    return any(resolved.is_relative_to(root.resolve()) for root in roots)


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
        if get_binding_hash(workspace, fields[1], checkout) != path.stem:
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
        for path in sorted(state_dir.glob("*.json")):
            finding = _inspect_file(path, cleanup_allowed=not journals)
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
                removed.append(f"Removed stale local skill state: {path}")
    return removed
