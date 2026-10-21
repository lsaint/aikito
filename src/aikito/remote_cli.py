"""CLI handlers for 'aikito remote' commands."""

from __future__ import annotations

import argparse
import getpass
import io
import json
import os
import shutil
import ssl
import sys
import time
import warnings
from datetime import datetime
from pathlib import Path
from typing import Any

from .init import is_recognized_workspace
from .remote_service import (
    bind_workspace_remote,
    get_workspace_remote_binding,
    get_workspace_remote_status,
    restart_workspace_remote_service,
    save_workspace_remote_credential,
    start_workspace_remote_service,
    stop_workspace_remote_service,
    sync_workspace_remote,
    unbind_workspace_remote,
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
from .workspace.remote_pairing import pair_device
from .workspace.encrypted_sync import initialize_encrypted_workspace
from .workspace.remote_store import (
    RecoveryRequired,
    StoreIdentityMismatch,
    StoreUnavailable,
)
from .workspace.remote_transport import TransportNotDelivered, TransportRejected
from .workspace.service import tail_log

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
            "Run 'aikito remote bind' first."
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


def render_remote_status(
    status: dict[str, Any],
    *,
    tail_lines: int | None = 5,
    following: bool = False,
) -> str:
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

    log_path_str = svc.get("log_path")
    if log_path_str:
        log_path = Path(log_path_str)
        lines.extend(["", f"Log file: {log_path}"])
        if tail_lines is not None and tail_lines > 0:
            act_header = (
                "Recent activity (following live logs, press Ctrl+C to exit):"
                if following
                else "Recent activity:"
            )
            lines.append(act_header)
            recent = tail_log(log_path, max_lines=tail_lines)
            if recent:
                for rline in recent.splitlines():
                    lines.append(f"  {rline}")
            else:
                lines.append("  (no recent activity logged)")

    return "\n".join(lines)


def cmd_remote_bind(args: argparse.Namespace) -> None:
    """Handle 'aikito remote bind [endpoint] [--rebind] [--token-env <VAR>] [--recovery-fd <FD>] [--recovery-output-fd <FD>] [--no-initialize] [--workspace <PATH>]'."""
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
    rebind = getattr(args, "rebind", False)
    recovery_fd = getattr(args, "recovery_fd", None)
    recovery_output_fd = getattr(args, "recovery_output_fd", None)
    no_initialize = getattr(args, "no_initialize", False)
    if endpoint is not None and getattr(args, "service", None):
        print("[ERROR] Use either an endpoint or --service.", file=sys.stderr)
        raise SystemExit(1)
    if endpoint is None:
        existing = get_workspace_remote_binding(workspace)
        if existing is not None and not rebind:
            # Resume interrupted encryption setup without allocating a new device.
            endpoint, token_env = existing.endpoint, existing.auth.env
            rebind = True
        else:
            try:
                service = getattr(args, "service", None) or os.environ.get(
                    "AIKITO_REMOTE_SERVICE_URL", ""
                )
                if not service:
                    if not sys.stdin.isatty():
                        raise RemoteBindingError(
                            "Provide --service with your HTTPS service URL."
                        )
                    service = input(
                        "Service URL (shown on your account page): "
                    ).strip()
                endpoint, token_env = pair_device(service, home=home)
            except (RemoteBindingError, EOFError, KeyboardInterrupt) as exc:
                print(f"[ERROR] {exc}", file=sys.stderr)
                raise SystemExit(1) from None

    def prompt_recovery_code(vault_id: str) -> str:
        if recovery_fd is not None:
            if recovery_fd < 0:
                raise ValueError("Recovery input requires a valid file descriptor.")
            with os.fdopen(os.dup(recovery_fd), "rb") as stream:
                return stream.read(128).decode("ascii").strip()
        if not sys.stdin.isatty():
            raise RecoveryRequired(
                "Detected existing encrypted synchronization data. Recovery code is required to authorize this device. "
                "Use an interactive terminal or pass --recovery-fd."
            )
        prefix = f" (vault: {vault_id[:8]}...)" if vault_id else ""
        print(f"[INFO] Connected to existing encrypted synchronization data{prefix}.")
        print("To authorize this device, please enter your recovery code:")
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            return getpass.getpass("Recovery code: ").strip()

    try:
        binding = bind_workspace_remote(
            workspace,
            endpoint,
            token_env,
            rebind=rebind,
            recovery_code_provider=prompt_recovery_code,
            home=home,
        )
    except Exception as exc:
        print(
            f"[ERROR] {_explain_remote_error(exc, endpoint=endpoint, token_env=token_env)}",
            file=sys.stderr,
        )
        sys.exit(1)

    if binding.status == "recovered":
        print("[SUCCESS] Synchronization key authorized and saved to device keystore.")
        print("[SUCCESS] Synchronization connected.")
        print("\nNext: Run 'aikito remote sync --apply' to synchronize your workspace.")
    elif binding.status == "empty":
        auto_init = False
        deliver = None
        if not no_initialize:
            if recovery_output_fd is not None:
                if recovery_output_fd < 0:
                    print(
                        "[ERROR] Recovery output requires a valid file descriptor.",
                        file=sys.stderr,
                    )
                    sys.exit(1)

                def deliver(code: str) -> None:
                    with os.fdopen(os.dup(recovery_output_fd), "wb") as output:
                        output.write((code + "\n").encode("ascii"))
                        output.flush()

                auto_init = True
            elif sys.stdout.isatty():

                def deliver(code: str) -> None:
                    print(
                        "\n========================================================================"
                    )
                    print("⚠️  IMPORTANT: SYNC RECOVERY CODE")
                    print(
                        "Keep this recovery code safe. You will need it to connect other devices"
                    )
                    print("or recover your data if this device is lost:\n")
                    print(f"    {code}\n")
                    print(
                        "It will NOT be shown again. Your account password cannot replace it."
                    )
                    if (
                        not sys.stdin.isatty()
                        or input("Have you saved it safely? [y/N] ").strip().lower()
                        != "y"
                    ):
                        raise ValueError(
                            "Recovery code was not confirmed; encryption was not initialized."
                        )
                    print(
                        "========================================================================\n"
                    )

                auto_init = True

        if auto_init and deliver is not None:
            try:
                print(
                    "[INFO] Connected to empty synchronization space. Initializing end-to-end encryption..."
                )
                initialize_encrypted_workspace(workspace, home, deliver)
                print("[SUCCESS] Device key created and saved to device keystore.")
                print("[SUCCESS] Synchronization connected.")
                print(
                    "\nNext: Run 'aikito remote sync --apply' to synchronize your workspace."
                )
                return
            except Exception as exc:
                print(
                    f"[ERROR] Failed to initialize encryption: {exc}",
                    file=sys.stderr,
                )
                sys.exit(1)

        print("[SUCCESS] Synchronization connected.")
        print(
            "\nNext: Initialize encryption with 'aikito remote key initialize', or recover its device key. Then run 'aikito remote sync --dry-run'."
        )
    else:
        print("[SUCCESS] Synchronization connected.")
        print("\nNext: Run 'aikito remote sync --apply' to synchronize your workspace.")


def cmd_remote_unbind(args: argparse.Namespace) -> None:
    """Handle 'aikito remote unbind [--force] [--workspace <PATH>]'."""
    home = Path.home()
    workspace = _resolve_cli_workspace(args, home)
    if not is_recognized_workspace(workspace):
        print(
            f"[ERROR] '{workspace}' is not a recognized Aikito workspace. Run 'aikito init workspace' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    force = getattr(args, "force", False)
    mode = getattr(args, "mode", "auto")
    binding = get_workspace_remote_binding(workspace)
    if binding is None:
        print(f"[INFO] Workspace '{workspace}' has no active remote store binding.")
        return

    try:
        unbind_workspace_remote(workspace, force=force, home=home, mode=mode)
    except Exception as exc:
        print(
            f"[ERROR] Failed to unbind remote store: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"[SUCCESS] Unbound workspace from remote store ({binding.endpoint}).")


def cmd_remote_sync(args: argparse.Namespace) -> None:
    """Handle 'aikito remote sync [--dry-run | --apply] [--verbose] [--workspace <PATH>]'."""
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
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = binding.endpoint
    token_env = binding.auth.env
    dry_run = getattr(args, "dry_run", False)
    verbose = getattr(args, "verbose", False)

    on_progress = None
    if sys.stdout.isatty():

        def on_progress(stage: str | None) -> None:
            width = max(1, shutil.get_terminal_size().columns - 1)
            line = f"{stage}..."[:width] if stage else ""
            sys.stdout.write(f"\r{line}\033[K")
            sys.stdout.flush()

    try:
        try:
            plan = sync_workspace_remote(
                workspace, dry_run=dry_run, home=home, on_progress=on_progress
            )
        finally:
            if on_progress:
                on_progress(None)
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
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    endpoint = binding.endpoint
    token_env = binding.auth.env
    poll_interval = getattr(args, "poll_interval", 5.0)
    debounce_delay = getattr(args, "debounce_ms", 500) / 1000.0
    version_check_interval = getattr(args, "version_check_interval", 60.0)
    once = getattr(args, "once", False)
    verbose = getattr(args, "verbose", False)

    def log(msg: str) -> None:
        if (
            verbose
            or msg.startswith("[ERROR]")
            or msg.startswith("[CONFLICT]")
            or msg.startswith("[SYNC]")
            or msg.startswith("[BLOCKED]")
            or msg.startswith("[WARN]")
            or msg.startswith("[INFO]")
        ):
            if msg.startswith("["):
                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                print(f"{ts} {msg}", flush=True)
            else:
                print(msg, flush=True)

    try:
        watch_workspace_remote(
            workspace,
            home=home,
            poll_interval=poll_interval,
            debounce_delay=debounce_delay,
            version_check_interval=version_check_interval,
            verbose=verbose,
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
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind' first.",
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


def cmd_remote_restart(args: argparse.Namespace) -> None:
    """Handle 'aikito remote restart [--workspace <PATH>] [--mode <auto|system|process>]'."""
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
            "[ERROR] No remote binding configured for this workspace. Run 'aikito remote bind' first.",
            file=sys.stderr,
        )
        sys.exit(1)

    mode = getattr(args, "mode", "auto")
    try:
        res = restart_workspace_remote_service(workspace, home=home, mode=mode)
    except Exception as exc:
        print(
            f"[ERROR] Failed to restart remote service: {_explain_remote_error(exc, endpoint=binding.endpoint, token_env=binding.auth.env)}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(
        f"[SUCCESS] Restarted background auto-sync service for workspace '{workspace}'"
    )
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


def _follow_log(log_path: Path) -> None:
    """Follow and live stream worker log file until interrupted."""
    try:
        deadline = time.time() + 3.0
        while not log_path.exists() and time.time() < deadline:
            time.sleep(0.2)

        if not log_path.exists():
            print(f"  (waiting for log file {log_path} to be created...)")
            while not log_path.exists():
                time.sleep(0.5)

        f = open(log_path, "r", encoding="utf-8", errors="replace")
        try:
            f.seek(0, io.SEEK_END)
            current_stat = os.fstat(f.fileno())
            current_inode = getattr(current_stat, "st_ino", None)
            current_mtime = getattr(current_stat, "st_mtime_ns", 0)

            while True:
                line = f.readline()
                if line:
                    sys.stdout.write(f"  {line}")
                    sys.stdout.flush()
                else:
                    time.sleep(0.25)
                    if log_path.exists():
                        try:
                            new_stat = log_path.stat()
                            reopened = False
                            if (
                                current_inode is not None
                                and new_stat.st_ino != current_inode
                            ):
                                reopened = True
                            elif new_stat.st_size < f.tell():
                                reopened = True
                            elif (
                                new_stat.st_size == f.tell()
                                and getattr(new_stat, "st_mtime_ns", 0) > current_mtime
                            ):
                                # On Windows with copytruncate rotation, file may be rewritten
                                # with new content of similar length; rewind to catch new lines.
                                reopened = True

                            if reopened:
                                f.close()
                                f = open(
                                    log_path, "r", encoding="utf-8", errors="replace"
                                )
                                current_stat = os.fstat(f.fileno())
                                current_inode = getattr(current_stat, "st_ino", None)
                                current_mtime = getattr(current_stat, "st_mtime_ns", 0)
                                continue
                        except OSError:
                            pass
        finally:
            f.close()
    except KeyboardInterrupt:
        pass


def cmd_remote_status(args: argparse.Namespace) -> None:
    """Handle 'aikito remote status [--workspace <PATH>] [--json] [--mode <auto|system|process>] [--tail [N]] [-f | --follow]'."""
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
    follow = getattr(args, "follow", False)
    tail_lines = getattr(args, "tail", 5)

    if as_json and follow:
        print("[ERROR] Cannot combine --json with --follow (-f).", file=sys.stderr)
        sys.exit(1)

    status = get_workspace_remote_status(workspace, home=home, mode=mode)

    if as_json:
        print(json.dumps(status, indent=2, sort_keys=True))
        return

    print(render_remote_status(status, tail_lines=tail_lines, following=follow))

    if follow:
        log_path_str = status.get("service", {}).get("log_path")
        if log_path_str:
            _follow_log(Path(log_path_str))


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
