"""Local recovery commands never publish a remote format or print vault keys."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from pathlib import Path

from .skill_state import WorkspaceWriterLock
from .workspace.remote_binding import (
    RemoteBinding,
    RemoteAuth,
    load_remote_binding,
    save_remote_binding,
)
from .workspace.remote_factory import _open_remote, BoundRemoteReader
from .workspace.encrypted_discovery import _fetch
from .workspace.encrypted_remote import metadata_resource
from .workspace.remote_format import FORMAT_RESOURCE_ID
from .workspace.payload import FilePayload, encode_payload

from .workspace.remote_format import EncryptedStoreUnsupported
from .init import is_recognized_workspace
from .workspace.encrypted_discovery import inspect_bound_workspace
from .workspace.encrypted_sync import (
    initialize_encrypted_workspace,
    run_encrypted_reconciliation,
)
from .workspace.encrypted_migration import MAX_MIGRATION_ROUNDS, run_migration
from .workspace.scoped_export import export_bound_scoped_read
from .workspace.device_keys import DeviceKeyStore
from .workspace.encrypted_remote import (
    MAX_METADATA_BYTES,
    EncryptedRemoteError,
    decode_recovery_code,
    recover_vault,
    require_encrypted_support,
)


def cmd_remote_key(args: argparse.Namespace) -> None:
    try:
        home = Path.home()
        workspace = (
            Path(args.workspace).expanduser().resolve()
            if getattr(args, "workspace", None)
            else None
        )
        store = DeviceKeyStore(home=home, workspace=workspace)
        if args.key_action == "path":
            print(store.path(args.vault_id) if args.vault_id else store.root)
            return
        if workspace is None or not is_recognized_workspace(workspace):
            raise ValueError(
                "Recovery requires --workspace pointing to an initialized workspace."
            )
        require_encrypted_support()
        if args.key_action == "export":
            export_bound_scoped_read(
                workspace,
                home,
                collection_id=args.collection,
                recipient=args.recipient,
                authorization=args.authorization,
                output=args.output,
            )
            print("[SUCCESS] Scoped read key exported.")
            return
        if args.key_action == "initialize":
            descriptor = args.recovery_output_fd
            if descriptor is None:
                if not sys.stdout.isatty():
                    raise ValueError(
                        "Recovery output requires a secure terminal or explicit descriptor."
                    )
                descriptor = sys.stdout.fileno()
            if descriptor < 0:
                raise ValueError("Invalid recovery output descriptor.")

            def deliver(code):
                with os.fdopen(os.dup(descriptor), "wb") as output:
                    output.write((code + "\n").encode("ascii"))
                    output.flush()

            vault_id = initialize_encrypted_workspace(workspace, home, deliver)
            print(
                f"[SUCCESS] Empty store initialized: vault_id={vault_id}",
                file=sys.stderr,
            )
            return
        if args.key_action == "migrate":

            def deliver_migration(code):
                descriptor = args.recovery_output_fd
                if descriptor is None:
                    if not sys.stdout.isatty():
                        raise ValueError(
                            "Recovery output requires a secure terminal or explicit descriptor."
                        )
                    descriptor = sys.stdout.fileno()
                if descriptor < 0:
                    raise ValueError("Invalid recovery output descriptor.")
                with os.fdopen(os.dup(descriptor), "wb") as output:
                    output.write((code + "\n").encode("ascii"))
                    output.flush()

            if not 1 <= args.max_rounds <= MAX_MIGRATION_ROUNDS:
                raise ValueError("Invalid migration round limit.")
            result = run_migration(
                workspace,
                home,
                rollback=args.rollback,
                takeover=args.takeover,
                dry_run=args.dry_run,
                maximum=args.max_rounds,
                deliver_recovery=deliver_migration,
            )
            print(
                f"[SUCCESS] Migration {result.status}: resources={result.resources}, rounds={result.rounds}, epoch={result.epoch}",
                file=sys.stderr,
            )
            if result.status == "paused":
                raise SystemExit(2)
            return
        if args.key_action == "sync":
            result = run_encrypted_reconciliation(workspace, home, dry_run=args.dry_run)
            print(
                f"[SUCCESS] Encrypted synchronization: changes={len(result.applied_items or result.changes)}, conflicts={len(result.conflicts)}, deferred={len(result.deferred)}, blocked={len(result.blocked_items)}, rounds={result.rounds}"
            )
            if result.blocked or result.blocked_items or result.conflicts:
                raise SystemExit(2)
            return
        if args.key_action == "inspect":
            result = inspect_bound_workspace(workspace, home=home)
            status = "complete" if result.complete else "incomplete"
            print(
                f"[SUCCESS] Encrypted discovery {status}: verified={len(result.resources)}, remaining={result.remaining}, fetched={result.fetched}, revision={result.snapshot.revision}"
            )
            return
        joining = getattr(args, "endpoint", None)
        new_binding = None
        sync_id = args.sync_id
        if joining:
            auth = RemoteAuth("bearer_env", args.token_env)
            raw = _open_remote(joining, auth, None, home=home)
            snapshot = raw.read()
            if sync_id is not None and snapshot.sync_id != sync_id:
                raise ValueError("Unexpected remote identity")
            sync_id = snapshot.sync_id
            descriptor = snapshot.resources.get(FORMAT_RESOURCE_ID)
            if descriptor is None or descriptor.size > len(
                encode_payload(FilePayload(bytes(MAX_METADATA_BYTES)))
            ):
                raise ValueError("Invalid recovery header")
            reader = BoundRemoteReader(raw, sync_id)
            encoded = _fetch(reader, snapshot, (FORMAT_RESOURCE_ID,))[
                FORMAT_RESOURCE_ID
            ].data
            if metadata_resource(encoded)[0] != descriptor:
                raise ValueError("Invalid recovery descriptor")
            new_binding = RemoteBinding(1, joining, sync_id, auth)
        else:
            if sync_id is None:
                raise ValueError("Local recovery requires --sync-id")
            with Path(args.header).open("rb") as stream:
                encoded = stream.read(MAX_METADATA_BYTES + 1)
        if args.recovery_fd is not None:
            if args.recovery_fd < 0:
                raise ValueError("Recovery input requires a valid file descriptor.")
            with os.fdopen(os.dup(args.recovery_fd), "rb") as stream:
                text = stream.read(128).decode("ascii")
        else:
            if not sys.stdin.isatty():
                raise ValueError(
                    "Use a secure terminal or --recovery-fd for recovery input."
                )
            with warnings.catch_warnings():
                warnings.simplefilter("error", getpass.GetPassWarning)
                text = getpass.getpass("Recovery code: ")
        vault = recover_vault(encoded, sync_id, decode_recovery_code(text))
        store.save(vault.vault_id, vault.vrk)
        if new_binding is not None:
            with WorkspaceWriterLock(home):
                existing = load_remote_binding(workspace)
                if existing is not None and existing != new_binding:
                    raise ValueError("An unrelated remote binding already exists")
                if existing is None:
                    save_remote_binding(workspace, new_binding, home=home)
        print("[SUCCESS] Device key recovered. Remote synchronization was not changed.")
    except (EncryptedRemoteError, EncryptedStoreUnsupported) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, UnicodeError, getpass.GetPassWarning):
        print(
            "[ERROR] Recovery input or device storage is unavailable.", file=sys.stderr
        )
        raise SystemExit(1) from None
