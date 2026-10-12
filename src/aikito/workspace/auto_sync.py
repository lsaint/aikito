"""Automatic synchronization scheduler, watcher, and lifecycle lock.

Coordinates continuous two-way synchronization between an active Aikito
workspace and an external remote store. Provides non-blocking single-worker
lifecycle locking, debounce aggregation, local filesystem watching,
periodic remote revision polling, and automatic conflict resolution.
"""

from __future__ import annotations

import errno
import json
import os
import shutil
import signal
import sys
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

if sys.platform != "win32":
    import fcntl
else:
    import msvcrt

from ..compat import (
    is_reparse_point,
    is_windows,
    secure_file_permissions,
)
from ..config import get_inbox_path
from .payload import FilePayload, TomlPayload, TreePayload
from .reconcile import (
    ReconcilePlan,
    build_reconcile_plan,
    recover_reconciliation,
    run_reconciliation,
)
from .remote_store import (
    RemoteSnapshot,
    RemoteStore,
    StoreUnavailable,
)
from .remote_transport import TransportNotDelivered
from .resource_state import (
    SYNC_CONFLICTS_DIR,
    WORKER_LOCK_STATE,
    WORKER_LOG_STATE,
    WORKER_PID_STATE,
    WORKER_STATUS_STATE,
    WORKER_STOP_STATE,
    local_resource_for_id,
    state_path,
)
from .resources import (
    IGNORED_NAMES,
    LOCAL_ONLY_NAMES,
    TOP_LEVEL_DIRS,
    TOP_LEVEL_FILES,
    is_ignored_name,
)
from .transactions import WorkspaceCoreError


class AutoSyncWorkerRunning(WorkspaceCoreError):
    """Another auto-sync worker process is currently running for this workspace."""


class AutoSyncState(str, Enum):
    IDLE = "idle"
    SYNCING = "syncing"
    RETRY_WAIT = "retry_wait"
    ATTENTION_REQUIRED = "attention_required"
    STOPPED = "stopped"


def _atomic_write_bytes(dest: Path, data: bytes) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = dest.parent / f".tmp-{dest.name}-{uuid.uuid4().hex}"
    try:
        tmp_path.write_bytes(data)
        os.replace(tmp_path, dest)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except Exception:
                pass


def _is_network_transient_error(exc: BaseException) -> bool:
    """Check whether an exception represents a transient network/connection failure."""
    target: BaseException | None = exc
    while target is not None:
        if isinstance(
            target, (StoreUnavailable, TransportNotDelivered, ConnectionError)
        ):
            return True
        if isinstance(target, OSError) and not isinstance(
            target,
            (
                FileNotFoundError,
                PermissionError,
                IsADirectoryError,
                NotADirectoryError,
            ),
        ):
            if getattr(target, "errno", None) in {
                errno.ECONNREFUSED,
                errno.ETIMEDOUT,
                errno.ENETUNREACH,
                errno.EHOSTUNREACH,
                errno.ECONNRESET,
                errno.EPIPE,
            }:
                return True
        target = getattr(target, "__cause__", None)
    return False


class AutoSyncLifecycleLock:
    """Non-blocking cross-process lifecycle lock for an auto-sync worker."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.lock_path = state_path(self.workspace, WORKER_LOCK_STATE, create=True)
        self.pid_path = state_path(self.workspace, WORKER_PID_STATE, create=True)
        self._file: Any = None

    def acquire(self) -> None:
        if self._file is not None:
            return

        if is_reparse_point(self.lock_path):
            raise WorkspaceCoreError(
                f"Worker lock file is a reparse point or symlink: {self.lock_path}"
            )

        f = open(self.lock_path, "a+", encoding="utf-8")
        try:
            secure_file_permissions(self.lock_path)
            if not is_windows():
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            else:
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
        except (BlockingIOError, OSError) as exc:
            f.close()
            raise AutoSyncWorkerRunning(
                f"Another auto-sync worker is already running for workspace '{self.workspace}'"
            ) from exc
        except Exception:
            f.close()
            raise

        try:
            f.seek(0)
            f.truncate()
            f.write(f"{os.getpid()}\n")
            f.flush()
        except Exception:
            pass

        try:
            self.pid_path.write_text(f"{os.getpid()}\n", encoding="utf-8")
            secure_file_permissions(self.pid_path)
        except Exception:
            pass

        self._file = f

    def release(self) -> None:
        if self._file is None:
            return
        f = self._file
        self._file = None
        try:
            if not is_windows():
                fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            else:
                try:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                except Exception:
                    pass
        finally:
            try:
                if self.pid_path.exists():
                    self.pid_path.unlink()
            except Exception:
                pass
            f.close()

    def __enter__(self) -> AutoSyncLifecycleLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


def _is_pid_alive(pid: int) -> bool:
    """Check if a process with the given PID is currently alive."""
    if pid <= 0:
        return False
    if not is_windows():
        try:
            os.kill(pid, 0)
            return True
        except OSError as err:
            return err.errno == errno.EPERM
    else:
        try:
            import ctypes

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(
                0x1000, False, pid
            )  # PROCESS_QUERY_LIMITED_INFORMATION
            if handle:
                kernel32.CloseHandle(handle)
                return True
            return False
        except Exception:
            return False


def is_worker_running(workspace: Path) -> tuple[bool, int | None]:
    """Check if an auto-sync worker process is currently running for this workspace.

    Returns (True, pid) if a worker holds the lifecycle lock, or (False, None).
    """
    workspace = workspace.expanduser().resolve()
    lock_path = state_path(workspace, WORKER_LOCK_STATE, create=False)
    if not lock_path.exists():
        return False, None

    pid: int | None = None
    pid_path = state_path(workspace, WORKER_PID_STATE, create=False)
    if pid_path.exists():
        try:
            content = pid_path.read_text(encoding="utf-8").strip()
            if content.isdigit():
                pid = int(content)
        except Exception:
            pass

    if pid is None:
        try:
            content = lock_path.read_text(encoding="utf-8").strip()
            if content.isdigit():
                pid = int(content)
        except Exception:
            pass

    try:
        f = open(lock_path, "a+", encoding="utf-8")
    except Exception:
        if pid is not None and _is_pid_alive(pid):
            return True, pid
        return False, None

    try:
        if not is_windows():
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            try:
                if pid_path.exists():
                    pid_path.unlink()
            except Exception:
                pass
            return False, None
        else:
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            f.seek(0)
            msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
            try:
                if pid_path.exists():
                    pid_path.unlink()
            except Exception:
                pass
            return False, None
    except (BlockingIOError, OSError):
        return True, pid
    finally:
        f.close()


def load_worker_status(workspace: Path) -> dict[str, Any] | None:
    """Load persisted worker status dictionary if available."""
    path = state_path(workspace, WORKER_STATUS_STATE, create=False)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def save_worker_status(workspace: Path, data: dict[str, Any]) -> None:
    """Atomically save worker status dictionary."""
    path = state_path(workspace, WORKER_STATUS_STATE, create=True)
    text = json.dumps(data, indent=2, sort_keys=True) + "\n"
    _atomic_write_bytes(path, text.encode("utf-8"))


@dataclass(frozen=True)
class ArchiveResult:
    status: str  # "archived", "no_content", "failed"
    path: Path | None = None
    error: str | None = None


@dataclass(frozen=True)
class ConflictResolution:
    winner: str  # "local" or "remote"
    loser: str  # "remote" or "local"
    archive_path: Path | None
    archive_status: str  # "archived", "no_content", "failed"
    error: str | None = None


def archive_conflict_loser(
    workspace: Path,
    item_id: str,
    loser_side: str,
    *,
    remote_store: RemoteStore | None = None,
    remote_snapshot: RemoteSnapshot | None = None,
    timestamp: str | None = None,
) -> ArchiveResult:
    """Archive the losing side of a conflicted resource to .local/sync-conflicts."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    try:
        canonical = local_resource_for_id(item_id, "0" * 64)
        rel_path = (
            canonical.parts[0].path if canonical.parts else item_id.replace(":", "/")
        )
    except Exception:
        try:
            canonical = local_resource_for_id(item_id, "")
            rel_path = (
                canonical.parts[0].path
                if canonical.parts
                else item_id.replace(":", "/")
            )
        except Exception:
            rel_path = item_id.replace(":", "/")

    dest_path = workspace / SYNC_CONFLICTS_DIR / timestamp / rel_path

    if loser_side == "local":
        local_file = workspace / rel_path
        if not local_file.exists():
            return ArchiveResult(status="no_content")
        try:
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if local_file.is_dir():
                shutil.copytree(
                    local_file, dest_path, symlinks=True, dirs_exist_ok=True
                )
            else:
                tmp_path = (
                    dest_path.parent / f".tmp-{dest_path.name}-{uuid.uuid4().hex}"
                )
                try:
                    shutil.copy2(local_file, tmp_path)
                    os.replace(tmp_path, dest_path)
                finally:
                    if tmp_path.exists():
                        try:
                            tmp_path.unlink()
                        except Exception:
                            pass
            return ArchiveResult(status="archived", path=dest_path)
        except Exception as exc:
            return ArchiveResult(status="failed", error=f"{type(exc).__name__}: {exc}")

    elif loser_side == "remote":
        if remote_store is None or remote_snapshot is None:
            return ArchiveResult(
                status="failed", error="Remote store or snapshot unavailable"
            )
        if item_id not in remote_snapshot.resources:
            return ArchiveResult(status="no_content")
        try:
            payloads = remote_store.fetch(remote_snapshot, [item_id])
            payload = payloads.get(item_id)
            if payload is None:
                return ArchiveResult(status="no_content")
            dest_path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(payload, FilePayload):
                _atomic_write_bytes(dest_path, payload.data)
            elif isinstance(payload, TreePayload):
                dest_path.mkdir(parents=True, exist_ok=True)
                for entry in payload.entries:
                    entry_dest = dest_path / entry.path
                    _atomic_write_bytes(entry_dest, entry.data)
            elif isinstance(payload, TomlPayload):
                _atomic_write_bytes(dest_path, payload.text.encode("utf-8"))
            else:
                return ArchiveResult(
                    status="failed",
                    error=f"Unsupported payload type {type(payload).__name__}",
                )
            return ArchiveResult(status="archived", path=dest_path)
        except Exception as exc:
            return ArchiveResult(status="failed", error=f"{type(exc).__name__}: {exc}")

    return ArchiveResult(status="failed", error=f"Invalid loser_side: {loser_side}")


def prune_conflict_archives(workspace: Path, max_retained: int = 50) -> None:
    """Keep the number of retained conflict archives bounded."""
    conflicts_root = workspace / SYNC_CONFLICTS_DIR
    if not conflicts_root.is_dir():
        return
    try:
        entries = sorted(
            [p for p in conflicts_root.iterdir() if p.is_dir()],
            key=lambda p: p.name,
        )
        if len(entries) > max_retained:
            for old_dir in entries[: len(entries) - max_retained]:
                shutil.rmtree(old_dir, ignore_errors=True)
    except Exception:
        pass


def plan_conflict_resolutions(
    plan: ReconcilePlan,
    workspace: Path,
    remote: RemoteStore,
    *,
    timestamp: str | None = None,
) -> tuple[dict[str, str], dict[str, ConflictResolution]]:
    """Determine winner/loser for all conflicts and archive the loser."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    resolutions: dict[str, str] = {}
    details: dict[str, ConflictResolution] = {}

    local_resources = plan.local_snapshot.resources
    remote_resources = plan.remote_snapshot.resources

    for item in plan.conflicts:
        local_exists = item.id in local_resources
        remote_exists = item.id in remote_resources

        if local_exists and not remote_exists:
            winner = "local"
            loser = "remote"
        elif not local_exists and remote_exists:
            winner = "remote"
            loser = "local"
        else:
            winner = "local"
            loser = "remote"

        res = archive_conflict_loser(
            workspace,
            item.id,
            loser,
            remote_store=remote,
            remote_snapshot=plan.remote_snapshot,
            timestamp=timestamp,
        )

        if res.status in ("archived", "no_content"):
            resolutions[item.id] = winner

        details[item.id] = ConflictResolution(
            winner=winner,
            loser=loser,
            archive_path=res.path,
            archive_status=res.status,
            error=res.error,
        )

    prune_conflict_archives(workspace)
    return resolutions, details


class LocalWatcher:
    """Monitors workspace managed directories for modifications with debouncing."""

    def __init__(
        self,
        workspace: Path,
        on_dirty: Callable[[], None],
        debounce_delay: float = 0.5,
        scan_interval: float = 1.0,
    ) -> None:
        self.workspace = workspace
        self.on_dirty = on_dirty
        self.debounce_delay = debounce_delay
        self.scan_interval = scan_interval
        self._running = False
        self._thread: threading.Thread | None = None
        self._debounce_timer: threading.Timer | None = None
        self._lock = threading.Lock()
        self._known_mtimes: dict[str, int] = {}

        managed_dirs = set(TOP_LEVEL_DIRS)
        try:
            inbox_path = get_inbox_path(self.workspace)
            rel_inbox = (
                inbox_path.resolve().relative_to(self.workspace.resolve()).as_posix()
            )
            top_inbox = rel_inbox.split("/")[0]
            managed_dirs.add(top_inbox)
        except Exception:
            managed_dirs.add("inbox")
        self.managed_dirs = frozenset(managed_dirs)
        self.managed_files = frozenset(TOP_LEVEL_FILES)

    def _scan_managed_mtimes(self) -> dict[str, int]:
        mtimes: dict[str, int] = {}
        try:
            children = list(self.workspace.iterdir())
        except OSError:
            return mtimes

        for child in children:
            if child.name in LOCAL_ONLY_NAMES or child.name in IGNORED_NAMES:
                continue
            if child.is_file() and child.name in self.managed_files:
                try:
                    mtimes[child.name] = child.stat().st_mtime_ns
                except OSError:
                    pass
            elif child.is_dir() and child.name in self.managed_dirs:
                try:
                    for root, dirs, files in os.walk(child):
                        dirs[:] = [
                            d
                            for d in dirs
                            if d not in LOCAL_ONLY_NAMES
                            and d not in IGNORED_NAMES
                            and not is_ignored_name(d)
                        ]
                        for f in files:
                            if f in IGNORED_NAMES or is_ignored_name(f):
                                continue
                            f_path = Path(root) / f
                            try:
                                rel = str(f_path.relative_to(self.workspace))
                                mtimes[rel] = f_path.stat().st_mtime_ns
                            except OSError:
                                pass
                except OSError:
                    pass
        return mtimes

    def _trigger_debounced_event(self) -> None:
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
            self._debounce_timer = threading.Timer(
                self.debounce_delay, self._on_debounce_expired
            )
            self._debounce_timer.daemon = True
            self._debounce_timer.start()

    def _on_debounce_expired(self) -> None:
        with self._lock:
            self._debounce_timer = None
        self.on_dirty()

    def _polled_loop(self) -> None:
        while self._running:
            time.sleep(self.scan_interval)
            if not self._running:
                break
            current = self._scan_managed_mtimes()
            if current != self._known_mtimes:
                self._known_mtimes = current
                self._trigger_debounced_event()

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._known_mtimes = self._scan_managed_mtimes()
        self._thread = threading.Thread(
            target=self._polled_loop,
            name=f"LocalWatcher-{self.workspace.name}",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        with self._lock:
            if self._debounce_timer is not None:
                self._debounce_timer.cancel()
                self._debounce_timer = None
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None


class AutoSyncScheduler:
    """Serial event-driven reconciliation scheduler for a single workspace worker."""

    def __init__(
        self,
        workspace: Path,
        remote: RemoteStore | None,
        home: Path,
        *,
        remote_opener: Callable[[], RemoteStore] | None = None,
        poll_interval: float = 5.0,
        debounce_delay: float = 0.5,
        scan_interval: float = 1.0,
        fallback_scan_interval: float = 30.0,
        on_status_change: Callable[[AutoSyncState, str | None], None] | None = None,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.remote = remote
        self._remote_opener = remote_opener
        self.home = home.expanduser().resolve()
        self.poll_interval = poll_interval
        self.debounce_delay = debounce_delay
        self.scan_interval = scan_interval
        self.fallback_scan_interval = fallback_scan_interval
        self.on_status_change = on_status_change
        self.logger = logger or (lambda msg: None)

        self.state = AutoSyncState.IDLE
        self._wake_event = threading.Event()
        self._lock = threading.Lock()
        self._event_seq = 0
        self._processed_seq = 0
        self._stopped = False

        self._status: dict[str, Any] = {
            "state": AutoSyncState.IDLE.value,
            "pid": os.getpid(),
            "last_checked": None,
            "last_success": None,
            "last_revision": None,
            "last_error": None,
            "conflicts": 0,
            "pending": 0,
        }
        self._last_logged_blocked: set[tuple[str, str]] = set()
        self._last_logged_conflicts: set[tuple[str, str]] = set()
        self._last_logged_findings: set[str] = set()
        self._last_logged_stop_reason: str | None = None

        existing = load_worker_status(self.workspace)
        if existing and isinstance(existing, dict):
            for k in ("last_success", "last_revision"):
                if k in existing:
                    self._status[k] = existing[k]

        self._watcher = LocalWatcher(
            self.workspace,
            self.notify_dirty,
            debounce_delay=self.debounce_delay,
            scan_interval=self.scan_interval,
        )

    def _check_stop_requested(self) -> bool:
        stop_path = state_path(self.workspace, WORKER_STOP_STATE, create=False)
        if stop_path.exists():
            try:
                stop_path.unlink()
            except OSError:
                pass
            self.stop()
            return True
        return False

    def _rotate_log(self, max_bytes: int = 5 * 1024 * 1024) -> None:
        log_path = state_path(self.workspace, WORKER_LOG_STATE, create=False)
        if not log_path.exists():
            return
        try:
            if log_path.stat().st_size >= max_bytes:
                backup = log_path.with_name(log_path.name + ".1")
                if backup.exists():
                    backup.unlink()
                log_path.rename(backup)
        except Exception:
            pass

    def _set_state(self, new_state: AutoSyncState, detail: str | None = None) -> None:
        self.state = new_state
        self._status["state"] = new_state.value
        if detail is not None:
            self._status["last_error"] = detail
        elif new_state in (AutoSyncState.IDLE, AutoSyncState.SYNCING):
            self._status["last_error"] = None
        save_worker_status(self.workspace, self._status)
        if self.on_status_change:
            try:
                self.on_status_change(new_state, detail)
            except Exception:
                pass

    def notify_dirty(self) -> None:
        with self._lock:
            self._event_seq += 1
        self._wake_event.set()

    def step(self) -> ReconcilePlan:
        """Run a single check and reconciliation step, resolving any conflicts."""
        self._rotate_log()
        self._status["last_checked"] = datetime.now(timezone.utc).isoformat()
        self._set_state(AutoSyncState.SYNCING)

        if self.remote is None and self._remote_opener is not None:
            self.remote = self._remote_opener()

        # 1. Recover pending commits or journals first (Fix Issue 1)
        if recover_reconciliation(self.workspace, self.remote, self.home):
            self.logger("[INFO] Recovered interrupted reconciliation round")

        plan = build_reconcile_plan(self.workspace, self.remote)
        attention_reasons: list[str] = []

        if plan.conflicts:
            resolutions, details = plan_conflict_resolutions(
                plan, self.workspace, self.remote
            )
            for cid, res in details.items():
                if res.archive_status == "archived":
                    self.logger(
                        f"[CONFLICT] Auto-resolved {cid}: {res.winner} won (archived to {res.archive_path})"
                    )
                elif res.archive_status == "no_content":
                    self.logger(
                        f"[CONFLICT] Auto-resolved {cid}: {res.winner} won (loser was deleted, nothing to archive)"
                    )
                else:
                    self.logger(
                        f"[ERROR] Failed to archive losing side for conflict '{cid}': {res.error or 'unknown error'}; skipped resolution"
                    )
                    attention_reasons.append(
                        f"Failed to archive conflict loser for {cid}: {res.error}"
                    )

            result = run_reconciliation(
                self.workspace,
                self.remote,
                self.home,
                dry_run=False,
                resolutions=resolutions,
            )
        elif plan.changes:
            result = run_reconciliation(
                self.workspace,
                self.remote,
                self.home,
                dry_run=False,
            )
        else:
            result = plan

        if result.applied_items:
            max_log_items = 20
            for item in result.applied_items[:max_log_items]:
                target = getattr(item, "target", None)
                target_norm = str(target).upper() if target else ""
                if target_norm == "LOCAL":
                    direction = "pull"
                elif target_norm == "REMOTE":
                    direction = "push"
                else:
                    direction = str(target).lower() if target else "sync"
                self.logger(
                    f"[SYNC] (rev {result.revision}) {direction} {item.id}"
                )
            if len(result.applied_items) > max_log_items:
                self.logger(
                    f"[SYNC] (rev {result.revision}) ... and {len(result.applied_items) - max_log_items} more change(s)"
                )

        # Check for blocked items, findings, stop reasons (Fix Issue 5)
        current_findings = set(result.findings)
        if current_findings != self._last_logged_findings:
            new_findings = current_findings - self._last_logged_findings
            if new_findings:
                self.logger(
                    f"[ERROR] Synchronization blocked by security findings: {', '.join(sorted(new_findings))}"
                )
            self._last_logged_findings = current_findings

        if result.findings:
            attention_reasons.append(f"Security findings: {', '.join(result.findings)}")

        current_blocked = {
            (item.id, item.reason or "security policy") for item in result.blocked_items
        }
        if current_blocked != self._last_logged_blocked:
            new_blocked = current_blocked - self._last_logged_blocked
            for item in result.blocked_items:
                if (item.id, item.reason or "security policy") in new_blocked:
                    self.logger(
                        f"[BLOCKED] Resource {item.id} is blocked: {item.reason or 'security policy'}"
                    )
            unblocked = self._last_logged_blocked - current_blocked
            if unblocked and not current_blocked:
                self.logger("[INFO] All previously blocked resources have been cleared")
            self._last_logged_blocked = current_blocked

        if result.blocked_items:
            attention_reasons.append(f"{len(result.blocked_items)} resource(s) blocked")

        current_conflicts = {
            (item.id, item.reason or "unresolved") for item in result.conflicts
        }
        if current_conflicts != self._last_logged_conflicts:
            new_conflicts = current_conflicts - self._last_logged_conflicts
            for item in result.conflicts:
                if (item.id, item.reason or "unresolved") in new_conflicts:
                    self.logger(
                        f"[CONFLICT] Resource {item.id} has unresolved conflict: {item.reason or 'unresolved'}"
                    )
            self._last_logged_conflicts = current_conflicts

        if result.conflicts:
            attention_reasons.append(f"{len(result.conflicts)} unresolved conflict(s)")

        if result.stop_reason != self._last_logged_stop_reason:
            if result.stop_reason:
                self.logger(f"[WARN] Synchronization stopped: {result.stop_reason}")
            self._last_logged_stop_reason = result.stop_reason

        if result.stop_reason:
            attention_reasons.append(f"Stopped: {result.stop_reason}")

        if result.deferred:
            self.logger(f"[WARN] {len(result.deferred)} resource(s) deferred")

        if hasattr(result, "conflicts"):
            self._status["conflicts"] = len(result.conflicts)

        if attention_reasons:
            self._set_state(
                AutoSyncState.ATTENTION_REQUIRED, "; ".join(attention_reasons)
            )
        else:
            self._status["last_success"] = datetime.now(timezone.utc).isoformat()
            if (
                hasattr(result, "remote_snapshot")
                and result.remote_snapshot is not None
            ):
                self._status["last_revision"] = result.remote_snapshot.revision
            self._set_state(AutoSyncState.IDLE)
        save_worker_status(self.workspace, self._status)

        return result

    def run(self, *, once: bool = False) -> None:
        """Run the scheduler with lifecycle locking."""
        lifecycle = AutoSyncLifecycleLock(self.workspace)
        with lifecycle:
            self._status["pid"] = os.getpid()
            save_worker_status(self.workspace, self._status)

            prev_sigterm = None
            prev_sigint = None
            try:
                if threading.current_thread() is threading.main_thread():

                    def _sig_handler(signum: int, frame: Any) -> None:
                        self.stop()

                    prev_sigterm = signal.signal(signal.SIGTERM, _sig_handler)
                    prev_sigint = signal.signal(signal.SIGINT, _sig_handler)
            except (ValueError, AttributeError):
                pass

            if once:
                self.step()
                return

            self._watcher.start()
            self.logger(f"[INFO] Auto-sync worker active for {self.workspace}")

            backoff = 5.0
            max_backoff = 300.0
            last_remote_revision: int | None = None
            last_full_scan = 0.0

            try:
                # Initial convergence step
                try:
                    result = self.step()
                    last_remote_revision = result.remote_snapshot.revision
                    last_full_scan = time.monotonic()
                    backoff = 5.0
                except Exception as exc:
                    if _is_network_transient_error(exc):
                        self._set_state(AutoSyncState.RETRY_WAIT, str(exc))
                        self.logger(
                            f"[WARN] Connection unavailable, retrying in {backoff:.0f}s: {exc}"
                        )
                        backoff = min(backoff * 2.0, max_backoff)
                    else:
                        self._set_state(AutoSyncState.ATTENTION_REQUIRED, str(exc))
                        self.logger(f"[ERROR] Sync error requires attention: {exc}")

                while not self._stopped:
                    if self._check_stop_requested() or self._stopped:
                        break

                    if self.state in (
                        AutoSyncState.RETRY_WAIT,
                        AutoSyncState.ATTENTION_REQUIRED,
                    ):
                        timeout = backoff
                    else:
                        timeout = self.poll_interval

                    self._wake_event.wait(timeout=timeout)
                    local_triggered = self._wake_event.is_set()
                    self._wake_event.clear()

                    if self._check_stop_requested() or self._stopped:
                        break

                    with self._lock:
                        target_seq = self._event_seq

                    should_step = False

                    if local_triggered:
                        should_step = True
                    elif self.state in (
                        AutoSyncState.RETRY_WAIT,
                        AutoSyncState.ATTENTION_REQUIRED,
                    ):
                        should_step = True
                    elif self.remote is None:
                        should_step = True
                    else:
                        # Serial remote polling in main thread
                        try:
                            remote_snapshot = self.remote.read()
                            if (
                                last_remote_revision is None
                                or remote_snapshot.revision != last_remote_revision
                            ):
                                should_step = True
                            elif (
                                time.monotonic() - last_full_scan
                                >= self.fallback_scan_interval
                            ):
                                should_step = True
                        except Exception as exc:
                            if _is_network_transient_error(exc):
                                self._set_state(AutoSyncState.RETRY_WAIT, str(exc))
                                self.logger(
                                    f"[WARN] Connection unavailable, retrying in {backoff:.0f}s: {exc}"
                                )
                                backoff = min(backoff * 2.0, max_backoff)
                            else:
                                self._set_state(
                                    AutoSyncState.ATTENTION_REQUIRED, str(exc)
                                )
                                self.logger(
                                    f"[ERROR] Sync error requires attention: {exc}"
                                )
                            continue

                    if not should_step:
                        continue

                    try:
                        result = self.step()
                        last_remote_revision = result.remote_snapshot.revision
                        last_full_scan = time.monotonic()
                        backoff = 5.0
                        with self._lock:
                            self._processed_seq = target_seq
                            # Only wake if there are new events arrived DURING step()
                            if self._event_seq > self._processed_seq:
                                self._wake_event.set()
                    except Exception as exc:
                        # Do NOT set _wake_event on exception! Unprocessed events remain
                        # tracked in _event_seq and will be processed upon retry when backoff expires.
                        if _is_network_transient_error(exc):
                            self._set_state(AutoSyncState.RETRY_WAIT, str(exc))
                            self.logger(
                                f"[WARN] Connection unavailable, retrying in {backoff:.0f}s: {exc}"
                            )
                            backoff = min(backoff * 2.0, max_backoff)
                        else:
                            self._set_state(AutoSyncState.ATTENTION_REQUIRED, str(exc))
                            self.logger(f"[ERROR] Sync error requires attention: {exc}")
                            backoff = min(backoff * 2.0, max_backoff)

            finally:
                if prev_sigterm is not None:
                    try:
                        signal.signal(signal.SIGTERM, prev_sigterm)
                    except Exception:
                        pass
                if prev_sigint is not None:
                    try:
                        signal.signal(signal.SIGINT, prev_sigint)
                    except Exception:
                        pass
                self._watcher.stop()
                self._set_state(AutoSyncState.STOPPED)
                self.logger("[INFO] Auto-sync worker stopped")

    def stop(self) -> None:
        """Signal the scheduler loop to terminate."""
        self._stopped = True
        self._wake_event.set()
