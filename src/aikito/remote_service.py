"""High-level service facade for workspace remote binding and reconciliation."""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from .workspace.access_catalog import publish_catalog
from .workspace.auto_sync import (
    AutoSyncScheduler,
    is_worker_running,
    load_worker_status,
)
from .workspace.encrypted_sync import run_encrypted_reconciliation
from .workspace.pending_commit import PendingCommitStore
from .workspace.reconcile import ReconcilePlan
from .workspace.remote_access import RemoteBindingMissing, RemoteCredentialError
from .workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
    remove_remote_binding,
)
from .workspace.remote_credentials import (
    resolve_credential_with_source,
    save_credential,
)
from .workspace.remote_factory import bind_remote, open_bound_remote
from .workspace.remote_store import RemoteStore
from .workspace.service import get_service_manager


def bind_workspace_remote(
    workspace: Path,
    endpoint: str,
    token_env: str = "AIKITO_REMOTE_TOKEN",
    *,
    rebind: bool = False,
    recovery_code: str | None = None,
    recovery_code_provider: Callable[[str], str] | None = None,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteBinding:
    """Bind a workspace to an external remote store endpoint."""
    auth = RemoteAuth("bearer_env", token_env)
    return bind_remote(
        workspace,
        endpoint,
        auth,
        rebind=rebind,
        recovery_code=recovery_code,
        recovery_code_provider=recovery_code_provider,
        home=home,
        environ=environ,
    )


def unbind_workspace_remote(
    workspace: Path,
    *,
    force: bool = False,
    home: Path | None = None,
    mode: str = "auto",
) -> bool:
    """Safely unbind workspace from remote store, stopping service if active."""
    home = Path.home() if home is None else home.expanduser().resolve()
    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        return False
    try:
        stop_workspace_remote_service(workspace, home=home, mode=mode)
    except Exception:
        if not force:
            raise
    remove_remote_binding(workspace, force=force, home=home)
    return True


def get_workspace_remote_binding(workspace: Path) -> RemoteBinding | None:
    """Load the current remote binding if configured."""
    return load_remote_binding(workspace)


def sync_workspace_remote(
    workspace: Path,
    *,
    dry_run: bool,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> ReconcilePlan:
    """Run reconciliation against the bound remote store."""
    home = Path.home() if home is None else home
    return run_encrypted_reconciliation(
        workspace, home, dry_run=dry_run, environ=environ, on_progress=on_progress
    )


def watch_workspace_remote(
    workspace: Path,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    poll_interval: float = 5.0,
    debounce_delay: float = 0.5,
    version_check_interval: float = 60.0,
    verbose: bool = False,
    once: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Run continuous automatic synchronization for a workspace."""
    home = Path.home() if home is None else home

    def remote_opener() -> RemoteStore:
        return open_bound_remote(workspace, home=home, environ=environ)

    try:
        remote: RemoteStore | None = remote_opener()
    except RemoteCredentialError:
        if once:
            raise
        remote = None

    last_catalog_attempt = None

    def publish_directory(_plan):
        nonlocal last_catalog_attempt
        if (
            last_catalog_attempt is not None
            and time.monotonic() - last_catalog_attempt < 60
        ):
            return
        last_catalog_attempt = time.monotonic()
        try:
            publish_catalog(workspace, home)
        except Exception:  # noqa: BLE001 - Optional directory updates cannot stop sync.
            if logger is not None:
                logger(
                    "Resource directory unavailable; encrypted synchronization continues"
                )

    scheduler = AutoSyncScheduler(
        workspace,
        remote,
        home=home,
        remote_opener=remote_opener,
        on_reconciled=publish_directory,
        poll_interval=poll_interval,
        debounce_delay=debounce_delay,
        version_check_interval=version_check_interval,
        verbose=verbose,
        logger=logger,
    )
    scheduler.run(once=once)


def start_workspace_remote_service(
    workspace: Path,
    *,
    home: Path | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Start the background synchronization service idempotently."""
    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        raise RemoteBindingMissing("Remote binding is missing")

    manager = get_service_manager(workspace, home=home, mode=mode)
    running, pid = manager.is_running()
    if running:
        return {
            "already_running": True,
            "pid": pid,
            "service_name": manager.get_service_name(),
            "manager_name": manager.manager_name(),
            "log_path": str(manager.get_log_path()),
        }

    manager.start(timeout=3.0)
    running, pid = manager.is_running()

    return {
        "already_running": False,
        "pid": pid,
        "service_name": manager.get_service_name(),
        "manager_name": manager.manager_name(),
        "log_path": str(manager.get_log_path()),
    }


def restart_workspace_remote_service(
    workspace: Path,
    *,
    home: Path | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Restart the background synchronization service idempotently."""
    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        raise RemoteBindingMissing("Remote binding is missing")

    manager = get_service_manager(workspace, home=home, mode=mode)
    was_running, old_pid = manager.is_running()
    if was_running:
        manager.stop(timeout=35.0)

    manager.start(timeout=5.0)
    running, pid = manager.is_running()

    return {
        "was_running": was_running,
        "old_pid": old_pid,
        "pid": pid,
        "service_name": manager.get_service_name(),
        "manager_name": manager.manager_name(),
        "log_path": str(manager.get_log_path()),
        "is_running": running,
    }


def stop_workspace_remote_service(
    workspace: Path,
    *,
    home: Path | None = None,
    mode: str = "auto",
) -> dict[str, Any]:
    """Stop the background synchronization service safely and disable autostart."""
    manager = get_service_manager(workspace, home=home, mode=mode)
    running, pid = manager.is_running()
    was_running = running
    stopped_pid = pid

    if running or manager.is_installed():
        manager.stop(timeout=35.0)
        manager.uninstall()
        running, _ = manager.is_running()

    return {
        "was_running": was_running,
        "pid": stopped_pid,
        "service_name": manager.get_service_name(),
        "manager_name": manager.manager_name(),
        "is_running": running,
    }


def get_workspace_remote_status(
    workspace: Path,
    *,
    home: Path | None = None,
    mode: str = "auto",
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Gather comprehensive structured status of remote binding and service."""
    home = Path.home() if home is None else home.expanduser().resolve()
    binding = get_workspace_remote_binding(workspace)
    running, pid = is_worker_running(workspace)
    manager = get_service_manager(workspace, home=home, mode=mode)
    raw_status = load_worker_status(workspace) or {}

    pending_store = PendingCommitStore(workspace, home)
    pending_item = pending_store.load()
    pending_count = 1 if pending_item is not None else 0

    cred_source: str | None = None
    cred_error: str | None = None
    if binding is not None:
        try:
            _, cred_source = resolve_credential_with_source(
                binding.auth,
                environ=environ,
                home=home,
            )
        except Exception as exc:
            cred_error = str(exc)

    state = raw_status.get("state", "idle") if running else "stopped"

    return {
        "workspace": str(workspace.expanduser().resolve()),
        "binding": {
            "endpoint": binding.endpoint,
            "sync_id": binding.sync_id,
            "token_env": binding.auth.env,
        }
        if binding
        else None,
        "service": {
            "running": running,
            "pid": pid,
            "state": state,
            "manager": manager.manager_name(),
            "name": manager.get_service_name(),
            "installed": manager.is_installed(),
            "log_path": str(manager.get_log_path()),
        },
        "last_checked": raw_status.get("last_checked"),
        "last_success": raw_status.get("last_success"),
        "last_revision": raw_status.get("last_revision"),
        "last_error": raw_status.get("last_error") or cred_error,
        "conflicts": raw_status.get("conflicts", 0),
        "pending": pending_count,
        "credential_source": cred_source,
    }


def save_workspace_remote_credential(
    key: str,
    value: str,
    *,
    home: Path | None = None,
) -> Path:
    """Save a credential securely to the user credentials file."""
    return save_credential(key, value, home=home)
