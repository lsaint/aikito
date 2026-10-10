"""CLI handlers for 'aikito remote' commands."""

from __future__ import annotations

import argparse
import getpass
import json
import ssl
import sys
from pathlib import Path
from typing import Any

from .init import is_recognized_workspace
from .remote_service import (
    bind_workspace_remote,
    get_workspace_remote_binding,
    get_workspace_remote_status,
    save_workspace_remote_credential,
    start_workspace_remote_service,
    stop_workspace_remote_service,
    sync_workspace_remote,
    watch_workspace_remote,
)
from .render import render_remote_sync_plan
from .workspace.auto_sync import AutoSyncWorkerRunning
from .workspace.paths import resolve_workspace
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
from .workspace.remote_credentials import (
    get_credentials_path,
    read_credentials_file,
    save_keychain,
)
from .workspace.remote_store import (
    RecoveryRequired,
    StoreIdentityMismatch,
    StoreUnavailable,
)
from .workspace.remote_transport import TransportNotDelivered, TransportRejected

EXIT_SUCCESS = 0
EXIT_FAILURE = 1
EXIT_LOCKED = 75
EXIT_UNRECOVERABLE = 78


def _resolve_cli_workspace(args: argparse.Namespace, home: Path) -> Path:
    ws_arg = getattr(args, "workspace", None)
    if ws_arg:
        return Path(ws_arg).expanduser().resolve()
    return resolve_workspace(home)


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
    if isinstance(target, RuntimeError):
        return str(target)
    return f"Failed: {target}"


def render_remote_status(status: dict[str, Any]) -> str:
    """Format structured remote and background service status for terminal output."""
    ws = status["workspace"]
    svc = status.get("service", {})
    running = svc.get("running", False)
    pid = svc.get("pid")
    manager = svc.get("manager", "unknown")
    state = svc.get("state", "stopped")

    if running:
        svc_str = f"{state} (PID: {pid})" if pid else f"{state}"
    else:
        svc_str = "stopped"

    binding = status.get("binding")
    if binding:
        ep_str = binding.get("endpoint", "None")
        raw_sync_id = str(binding.get("sync_id", "None"))
        if raw_sync_id and len(raw_sync_id) > 12:
            sync_id_str = f"{raw_sync_id[:8]}...{raw_sync_id[-4:]}"
        else:
            sync_id_str = raw_sync_id
        token_env = binding.get("token_env", "None")
        source = status.get("credential_source")
        if source:
            cred_str = f"{token_env} (resolved from {source})"
        else:
            cred_str = f"{token_env} (not resolved)"
    else:
        ep_str = "None (not bound)"
        sync_id_str = "None"
        cred_str = "None"

    last_chk = status.get("last_checked") or "Never"
    last_succ = status.get("last_success") or "Never"
    last_rev = status.get("last_revision")
    rev_str = str(last_rev) if last_rev is not None else "None"
    pending = status.get("pending", 0)
    conflicts = status.get("conflicts", 0)
    last_err = status.get("last_error") or "None"

    lines = [
        "Aikito Remote Status:",
        f"  Workspace:       {ws}",
        f"  Service:         {svc_str}",
        f"  Service manager: {manager}",
        f"  Endpoint:        {ep_str}",
        f"  Sync ID:         {sync_id_str}",
        f"  Credential:      {cred_str}",
        f"  Last checked:    {last_chk}",
        f"  Last success:    {last_succ}",
        f"  Last revision:   {rev_str}",
        f"  Pending:         {pending}",
        f"  Conflicts:       {conflicts}",
        f"  Last error:      {last_err}",
    ]
    return "\n".join(lines)


def cmd_remote_bind(args: argparse.Namespace) -> None:
    """Handle 'aikito remote bind <endpoint> [--token-env <VAR>] [--workspace <PATH>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
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
    """Handle 'aikito remote sync (--dry-run | --apply) [--verbose] [--workspace <PATH>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
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
    """Handle 'aikito remote watch [--poll-interval <SEC>] [--debounce-ms <MS>] [--once] [--verbose] [--workspace <PATH>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
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
        sys.exit(EXIT_LOCKED)
    except KeyboardInterrupt:
        print("\n[INFO] Auto-sync watcher stopped.", file=sys.stderr)
        sys.exit(EXIT_SUCCESS)
    except (
        RemoteCredentialError,
        StoreIdentityMismatch,
        RemoteEndpointInsecure,
        RemoteEndpointCredentialsForbidden,
    ) as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_UNRECOVERABLE)
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(EXIT_FAILURE)


def cmd_remote_start(args: argparse.Namespace) -> None:
    """Handle 'aikito remote start [--workspace <PATH>] [--mode <auto|system|process>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
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

    mode = getattr(args, "mode", "auto")
    try:
        res = start_workspace_remote_service(workspace, home=home, mode=mode)
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=binding.endpoint, token_env=binding.auth.env)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if res["already_running"]:
        print(f"[INFO] Auto-sync service is already running (PID: {res['pid']}).")
        return

    print(f"[SUCCESS] Started background auto-sync service for workspace '{workspace}'")
    print(f"  Service type: {res['manager_name']} ({res['service_name']})")
    status_msg = f"running (PID: {res['pid']})" if res.get("pid") else "started"
    print(f"  Status:       {status_msg}")
    print(f"  Log file:     {res['log_path']}")


def cmd_remote_stop(args: argparse.Namespace) -> None:
    """Handle 'aikito remote stop [--workspace <PATH>] [--mode <auto|system|process>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = getattr(args, "mode", "auto")
    try:
        res = stop_workspace_remote_service(workspace, home=home, mode=mode)
    except Exception as exc:
        print(f"[ERROR] Failed to stop remote service: {exc}", file=sys.stderr)
        sys.exit(1)

    if res["was_running"]:
        if not res["is_running"]:
            print(f"[SUCCESS] Stopped auto-sync service for workspace '{workspace}'.")
        else:
            print(
                f"[ERROR] Service process could not be terminated (PID: {res.get('pid')}).",
                file=sys.stderr,
            )
            sys.exit(1)
    else:
        print("[INFO] Auto-sync service is not running.")


def cmd_remote_status(args: argparse.Namespace) -> None:
    """Handle 'aikito remote status [--workspace <PATH>] [--json] [--mode <auto|system|process>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = getattr(args, "mode", "auto")
    as_json = getattr(args, "json", False)

    status = get_workspace_remote_status(workspace, home=home, mode=mode)

    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
    else:
        print(render_remote_status(status))


def cmd_remote_credential(args: argparse.Namespace) -> None:
    """Handle 'aikito remote credential {set,list,path}'."""
    home = Path.home()
    cred_action = getattr(args, "cred_action", None)

    if cred_action == "set":
        key = args.key
        use_stdin = getattr(args, "stdin", False)
        if use_stdin or not sys.stdin.isatty():
            value = sys.stdin.read().strip()
        else:
            value = getpass.getpass(f"Enter token for '{key}': ").strip()

        if not value:
            print("[ERROR] Credential value cannot be empty.", file=sys.stderr)
            sys.exit(1)

        use_keychain = getattr(args, "keychain", False)
        if use_keychain:
            try:
                save_keychain(key, value)
                print(
                    f"[SUCCESS] Saved credential '{key}' to operating system keychain."
                )
            except Exception as exc:
                print(
                    f"[ERROR] Failed to save credential to keychain: {exc}",
                    file=sys.stderr,
                )
                sys.exit(EXIT_FAILURE)
        else:
            try:
                target = save_workspace_remote_credential(key, value, home=home)
                print(
                    f"[SUCCESS] Saved credential '{key}' to secure credentials file ({target})."
                )
            except Exception as exc:
                print(f"[ERROR] Failed to save credential: {exc}", file=sys.stderr)
                sys.exit(EXIT_FAILURE)
    elif cred_action == "path":
        target = get_credentials_path(home)
        print(str(target))
    elif cred_action == "list":
        target = get_credentials_path(home)
        creds = read_credentials_file(target) if target.exists() else {}
        print("Configured credentials:")
        if not creds:
            print("  (none)")
        else:
            for k in sorted(creds.keys()):
                print(f"  {k}: [configured]")
    else:
        print("Usage: aikito remote credential {set,list,path}", file=sys.stderr)
        sys.exit(1)
