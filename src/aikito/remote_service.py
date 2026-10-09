"""High-level service facade for workspace remote binding and reconciliation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path

from .workspace.auto_sync import AutoSyncScheduler
from .workspace.reconcile import ReconcilePlan, run_reconciliation
from .workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
)
from .workspace.remote_factory import bind_remote, open_bound_remote


def bind_workspace_remote(
    workspace: Path,
    endpoint: str,
    token_env: str = "AIKITO_REMOTE_TOKEN",
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> RemoteBinding:
    """Bind a workspace to an external remote store endpoint."""
    auth = RemoteAuth("bearer_env", token_env)
    return bind_remote(
        workspace,
        endpoint,
        auth,
        home=home,
        environ=environ,
    )


def get_workspace_remote_binding(workspace: Path) -> RemoteBinding | None:
    """Load the current remote binding if configured."""
    return load_remote_binding(workspace)


def sync_workspace_remote(
    workspace: Path,
    *,
    dry_run: bool,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
) -> ReconcilePlan:
    """Run reconciliation against the bound remote store."""
    home = Path.home() if home is None else home
    remote = open_bound_remote(workspace, environ=environ)
    return run_reconciliation(
        workspace,
        remote,
        home=home,
        dry_run=dry_run,
    )


def watch_workspace_remote(
    workspace: Path,
    *,
    home: Path | None = None,
    environ: Mapping[str, str] | None = None,
    poll_interval: float = 5.0,
    debounce_delay: float = 0.5,
    once: bool = False,
    logger: Callable[[str], None] | None = None,
) -> None:
    """Run continuous automatic synchronization for a workspace."""
    home = Path.home() if home is None else home
    remote = open_bound_remote(workspace, environ=environ)
    scheduler = AutoSyncScheduler(
        workspace,
        remote,
        home=home,
        poll_interval=poll_interval,
        debounce_delay=debounce_delay,
        logger=logger,
    )
    scheduler.run(once=once)
