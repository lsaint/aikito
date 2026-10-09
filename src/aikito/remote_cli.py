"""CLI handlers for 'aikito remote' commands."""

from __future__ import annotations

import argparse
import ssl
import sys
from pathlib import Path

from .init import is_recognized_workspace
from .workspace.paths import resolve_workspace
from .workspace.auto_sync import AutoSyncWorkerRunning
from .remote_service import (
    bind_workspace_remote,
    get_workspace_remote_binding,
    sync_workspace_remote,
    watch_workspace_remote,
)
from .render import render_remote_sync_plan
from .workspace.reconcile import (
    InterruptedRoundRecovered,
    WorkspaceReconcileError,
)
from .workspace.remote_access import (
    RemoteAccessDenied,
    RemoteBindingError,
    RemoteBindingExists,
    RemoteBindingMissing,
    RemoteCredentialError,
    RemoteEndpointCredentialsForbidden,
    RemoteEndpointInsecure,
)
from .workspace.remote_store import (
    RecoveryRequired,
    StoreIdentityMismatch,
    StoreUnavailable,
)
from .workspace.remote_transport import TransportNotDelivered, TransportRejected


def _explain_remote_error(
    exc: BaseException,
    *,
    endpoint: str | None = None,
    token_env: str | None = None,
) -> str:
    target = exc
    if isinstance(target, WorkspaceReconcileError) and target.__cause__ is not None:
        target = target.__cause__

    if isinstance(target, RemoteBindingExists):
        return (
            "Remote binding already exists for this workspace. "
            "Existing bindings cannot be replaced silently."
        )
    if isinstance(target, RemoteBindingMissing):
        return (
            "No remote binding configured for this workspace. "
            "Run 'aikito remote bind <endpoint>' first."
        )
    if isinstance(target, RemoteCredentialError):
        if token_env:
            return (
                f"Remote credential in environment variable '{token_env}' "
                "is missing or invalid."
            )
        return "Remote credential is missing or invalid."
    if isinstance(target, RemoteEndpointInsecure):
        return "Insecure endpoint: Remote credentials require HTTPS outside loopback."
    if isinstance(target, RemoteEndpointCredentialsForbidden):
        return "Security violation: Credentials must not appear in endpoint URL query parameters."
    if isinstance(target, (RemoteAccessDenied, TransportRejected)):
        status = getattr(target, "status", None)
        if status == 401:
            hint = f" Please verify the token in '{token_env}'." if token_env else ""
            return f"Remote authentication failed (HTTP 401):{hint}"
        if status == 403:
            return "Remote authorization denied (HTTP 403): Access to the remote store was denied."
        return f"Remote server rejected request (HTTP {status})."
    if isinstance(target, ssl.SSLError):
        ep_hint = f" for '{endpoint}'" if endpoint else ""
        return f"SSL/TLS certificate verification failed{ep_hint}: {target}"
    if isinstance(target, (StoreUnavailable, TransportNotDelivered)):
        ep_hint = f" to '{endpoint}'" if endpoint else ""
        return f"Connection failed: Could not establish connection{ep_hint}."
    if isinstance(target, StoreIdentityMismatch):
        return "Store identity mismatch: The remote store does not match the bound store or local replica."
    if isinstance(target, InterruptedRoundRecovered):
        return (
            "An interrupted synchronization round was recovered. "
            "Run 'aikito remote sync' again to proceed."
        )
    if isinstance(target, RecoveryRequired):
        return f"Remote store requires recovery: {target}"
    if isinstance(target, RemoteBindingError):
        return f"Remote binding error: {target}"
    if isinstance(target, WorkspaceReconcileError):
        return f"Synchronization error: {target}"
    return f"Failed: {target}"


def cmd_remote_bind(args: argparse.Namespace) -> None:
    """Handle 'aikito remote bind <endpoint> [--token-env <VAR>]'."""
    home = Path.home()
    workspace = resolve_workspace(home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = args.endpoint
    token_env = getattr(args, "token_env", "AIKITO_REMOTE_TOKEN")

    try:
        binding = bind_workspace_remote(workspace, endpoint, token_env, home=home)
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[SUCCESS] Bound workspace to remote store at {binding.endpoint}")
    print(f"  Sync ID:   {binding.sync_id}")
    print(f"  Token env: {binding.auth.env}")
    print("\nNext: Run 'aikito remote sync --dry-run' to preview synchronization.")


def cmd_remote_sync(args: argparse.Namespace) -> None:
    """Handle 'aikito remote sync (--dry-run | --apply) [--verbose]'."""
    home = Path.home()
    workspace = resolve_workspace(home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        print(
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind <endpoint>' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = binding.endpoint
    token_env = binding.auth.env
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)

    try:
        plan = sync_workspace_remote(workspace, dry_run=dry_run, home=home)
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(1)

    rendered = render_remote_sync_plan(plan, dry_run=dry_run, verbose=verbose)
    print(rendered)

    if (
        plan.blocked
        or plan.conflicts
        or bool(plan.blocked_items)
        or bool(plan.stop_reason)
        or bool(plan.deferred)
    ):
        sys.exit(1)


def cmd_remote_watch(args: argparse.Namespace) -> None:
    """Handle 'aikito remote watch [--poll-interval <SEC>] [--debounce-ms <MS>] [--once] [--verbose]'."""
    home = Path.home()
    workspace = resolve_workspace(home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        print(
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind <endpoint>' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = binding.endpoint
    token_env = binding.auth.env
    poll_interval = getattr(args, "poll_interval", 5.0)
    debounce_delay = getattr(args, "debounce_ms", 500) / 1000.0
    once = getattr(args, "once", False)
    verbose = getattr(args, "verbose", False)

    def log(msg: str) -> None:
        if (
            verbose
            or msg.startswith("[ERROR]")
            or msg.startswith("[CONFLICT]")
            or msg.startswith("[SYNC]")
        ):
            print(msg)

    try:
        watch_workspace_remote(
            workspace,
            home=home,
            poll_interval=poll_interval,
            debounce_delay=debounce_delay,
            once=once,
            logger=log,
        )
    except AutoSyncWorkerRunning as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        sys.exit(1)
    except KeyboardInterrupt:
        print("\n[INFO] Auto-sync watcher stopped.", file=sys.stderr)
        sys.exit(0)
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(1)
