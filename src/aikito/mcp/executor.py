"""Execution and state management for MCP synchronization."""

import json
import os
import shutil
import stat
import sys
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..compat import secure_file_permissions
from ..config_runtime import StaleConfigPlanError
from .model import (
    BACKUP_DIR,
    STATE_FILE,
    STATE_VERSION,
    AgentSpec,
    MCPConfigError,
    MCPExecutionResult,
    MCPFilePlan,
    MCPPlan,
)


def _load_state(home: Path) -> dict[str, Any]:
    path = home / STATE_FILE
    if not path.exists():
        return {"version": STATE_VERSION, "entries": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MCPConfigError(f"Cannot read MCP state file {path}: {exc}") from exc
    if state.get("version") != STATE_VERSION or not isinstance(
        state.get("entries"), dict
    ):
        raise MCPConfigError(f"Unsupported MCP state file: {path}")
    return state


def _atomic_write(path: Path, content: str, secure_permissions: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    if secure_permissions:
        if not secure_file_permissions(temp_path):
            print(
                f"[WARN] Could not secure file permissions on credential file: {path}",
                file=sys.stderr,
            )
    elif mode is not None:
        try:
            temp_path.chmod(mode)
        except OSError:
            pass
    os.replace(temp_path, path)


def _get_atomic_write() -> Callable[..., None]:
    mcp_mod = sys.modules.get("aikito.mcp")
    if mcp_mod is not None and hasattr(mcp_mod, "_atomic_write"):
        return mcp_mod._atomic_write
    return _atomic_write


def _get_backup_config() -> Callable[..., Path | None]:
    mcp_mod = sys.modules.get("aikito.mcp")
    if mcp_mod is not None and hasattr(mcp_mod, "_backup_config"):
        return mcp_mod._backup_config
    return _backup_config


def _save_state(home: Path, state: dict[str, Any]) -> None:
    path = home / STATE_FILE
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _get_atomic_write()(path, content)


def _backup_config(home: Path, spec: AgentSpec) -> Path | None:
    if not spec.config_path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / BACKUP_DIR / spec.agent / f"{timestamp}-{spec.config_path.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec.config_path, backup)
    return backup


def _backup_file_plan(home: Path, file_plan: MCPFilePlan) -> Path | None:
    if not file_plan.path.exists():
        return None
    agent = file_plan.operations[0].target.agent if file_plan.operations else "common"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / BACKUP_DIR / agent / f"{timestamp}-{file_plan.path.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_plan.path, backup)
    return backup


def execute_mcp_plan(
    plan: MCPPlan,
    home: Path,
    *,
    output: Callable[[str], None] = print,
) -> MCPExecutionResult:
    """Apply an MCPPlan transactionally with atomic write once, backup, rollback, and state commit.

    Enforces INV-MCP-03, INV-MCP-06, INV-MCP-08.
    """
    # 1. Validate preconditions
    try:
        plan.validate_preconditions(home)
    except StaleConfigPlanError as exc:
        output(f"[ERROR] MCP plan is stale: {exc}")
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=0,
            skipped_count=0,
            conflict_count=0,
            failed_count=len(plan.operations),
            error_message=f"Plan is stale: {exc}",
        )

    # 2. Check conflicts / authorization
    if not plan.can_apply:
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=plan.conflicts_count,
            failed_count=0,
            error_message="Plan contains unauthorized conflicts",
        )

    # 3. Prepare new state in memory
    state = _load_state(home)
    new_entries = dict(state.get("entries", {}))

    for op in plan.operations:
        if not op.is_authorized:
            continue
        if op.action in ("NOOP", "CREATE", "UPDATE") and op.state_transition:
            state_key, fp = op.state_transition
            new_entries[state_key] = {
                "fingerprint": fp,
                "config_path": str(op.target.path),
                "target_name": op.target.target_name,
            }
        elif op.action == "REMOVE":
            if op.state_transition:
                new_entries.pop(op.state_transition[0], None)
            if op.spec:
                new_entries.pop(op.spec.state_key, None)
            srv_suffix = f":{op.target.logical_identity}"
            to_del = [k for k in new_entries if k.endswith(srv_suffix)]
            for k in to_del:
                new_entries.pop(k, None)

    new_state = dict(state, entries=new_entries)
    state_path = home / STATE_FILE
    state_tmp: Path | None = None

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=state_path.parent,
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        ) as sf:
            sf.write(
                json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            state_tmp = Path(sf.name)
    except Exception as exc:
        output(f"[ERROR] Failed to prepare state file for atomic save: {exc}")
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=len(plan.operations),
            error_message=f"Failed to prepare state file: {exc}",
        )

    mutating_files = [fp for fp in plan.file_plans if fp.will_mutate]
    if not mutating_files:
        try:
            os.replace(state_tmp, state_path)
        except Exception as exc:
            if state_tmp and state_tmp.exists():
                try:
                    state_tmp.unlink()
                except Exception:
                    pass
            output(f"[ERROR] Failed to update state file: {exc}")
            return MCPExecutionResult(
                success=False,
                applied_count=0,
                noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
                skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
                conflict_count=0,
                failed_count=1,
                error_message=f"Failed to update state: {exc}",
            )
        return MCPExecutionResult(
            success=True,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=0,
        )

    # 4. Take backups for all eligible files BEFORE modifying any runtime files
    backups_created: list[tuple[MCPFilePlan, Path]] = []
    backup_error: Exception | None = None
    failed_fp: MCPFilePlan | None = None

    atomic_write_fn = _get_atomic_write()
    backup_config_fn = _get_backup_config()

    for fp in mutating_files:
        if not fp.should_backup:
            continue
        try:
            first_spec = (
                fp.operations[0].spec
                if fp.operations and fp.operations[0].spec
                else None
            )
            bk = (
                backup_config_fn(home, first_spec)
                if first_spec
                else _backup_file_plan(home, fp)
            )
            if bk:
                backups_created.append((fp, bk))
        except Exception as exc:
            backup_error = exc
            failed_fp = fp
            first_op = fp.operations[0] if fp.operations else None
            agent_srv = (
                f"{first_op.target.agent}/{first_op.target.logical_identity}"
                if first_op
                else str(fp.path)
            )
            output(
                f"[ERROR] {agent_srv}: backup failed ({exc}); "
                "aborting before modifying runtime files"
            )
            break

    if backup_error is not None:
        for _f, bk in backups_created:
            try:
                bk.unlink(missing_ok=True)
            except Exception:
                pass
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            failed_files=(failed_fp.path,) if failed_fp else (),
            error_message=f"Backup failed: {backup_error}",
        )

    # 5. Atomic write of each mutating file
    committed: list[tuple[MCPFilePlan, Path | None]] = []
    write_error: Exception | None = None
    failed_write_fp: MCPFilePlan | None = None

    for fp in mutating_files:
        backup_path = next((b for f, b in backups_created if f.path == fp.path), None)
        try:
            atomic_write_fn(
                fp.path, fp.final_content or "", secure_permissions=fp.sensitive
            )
            committed.append((fp, backup_path))
        except Exception as exc:
            write_error = exc
            failed_write_fp = fp
            first_op = fp.operations[0] if fp.operations else None
            agent_srv = (
                f"{first_op.target.agent}/{first_op.target.logical_identity}"
                if first_op
                else str(fp.path)
            )
            output(
                f"[ERROR] {agent_srv}: write failed ({exc}); "
                "rolling back all committed agent configs"
            )
            break

    def _rollback() -> tuple[bool, set[Path], list[str]]:
        all_succeeded = True
        retained_backups: set[Path] = set()
        warnings: list[str] = []
        for c_fp, c_bk in committed:
            rb_ok = False
            try:
                if c_fp.pre_image.exists:
                    atomic_write_fn(
                        c_fp.path,
                        c_fp.orig_content if c_fp.orig_content is not None else "",
                        secure_permissions=c_fp.sensitive,
                    )
                elif c_fp.path.exists():
                    c_fp.path.unlink()
                rb_ok = True
            except Exception as rb_exc:
                all_succeeded = False
                recovery_hint = (
                    f"; backup retained at {c_bk}" if c_bk is not None else ""
                )
                first_op = c_fp.operations[0] if c_fp.operations else None
                agent_srv = (
                    f"{first_op.target.agent}/{first_op.target.logical_identity}"
                    if first_op
                    else str(c_fp.path)
                )
                msg = f"{agent_srv}: rollback failed ({rb_exc}); manual inspection required{recovery_hint}"
                output(f"[WARN] {msg}")
                warnings.append(msg)

            if c_bk is not None:
                if not rb_ok:
                    retained_backups.add(c_bk)
                else:
                    try:
                        c_bk.unlink(missing_ok=True)
                    except Exception:
                        pass
        return all_succeeded, retained_backups, warnings

    if write_error is not None:
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        rb_success, retained_bks, rb_warnings = _rollback()
        for _f, bk in backups_created:
            if bk not in retained_bks:
                try:
                    bk.unlink(missing_ok=True)
                except Exception:
                    pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            failed_files=(failed_write_fp.path,) if failed_write_fp else (),
            backups_created=tuple(retained_bks),
            backup_warnings=tuple(rb_warnings),
            error_message=f"Write failed: {write_error}",
            recovery_required=not rb_success,
            recovery_guidance="; ".join(rb_warnings) if not rb_success else None,
        )

    # 6. Promote state temp file
    try:
        os.replace(state_tmp, state_path)
    except Exception as exc:
        output(
            f"[ERROR] All agent configs written but state save failed: {exc}; "
            "rolling back runtime changes to keep state consistent"
        )
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        rb_success, retained_bks, rb_warnings = _rollback()
        for _f, bk in backups_created:
            if bk not in retained_bks:
                try:
                    bk.unlink(missing_ok=True)
                except Exception:
                    pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            backups_created=tuple(retained_bks),
            backup_warnings=tuple(rb_warnings),
            error_message=f"State promotion failed: {exc}",
            recovery_required=not rb_success,
            recovery_guidance="; ".join(rb_warnings) if not rb_success else None,
        )

    # 7. Success! Output sync logs
    for fp, bk in committed:
        first_spec = True
        for op in fp.operations:
            if op.is_authorized and op.action in ("CREATE", "UPDATE", "REMOVE"):
                action_str = (
                    f"{op.action.lower()}d" if op.action != "REMOVE" else "removed from"
                )
                output(
                    f"[SYNC] {op.target.agent}/{op.target.logical_identity}: {action_str} {fp.path}"
                )
                if first_spec and bk:
                    output(f"[BACKUP] {bk}")
                    first_spec = False
                if op.spec and op.spec.auth_command:
                    output(
                        f"[AUTH] aikito auth mcp {op.target.agent} {op.target.logical_identity}"
                    )

    all_backups = tuple(b for _f, b in backups_created)
    return MCPExecutionResult(
        success=True,
        applied_count=plan.changes_count,
        noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
        skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
        conflict_count=0,
        failed_count=0,
        backups_created=all_backups,
    )
