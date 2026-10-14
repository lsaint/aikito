"""Local recovery commands never publish a remote format or print vault keys."""

from __future__ import annotations

import argparse
import getpass
import os
import sys
import warnings
from pathlib import Path

from .workspace.remote_format import EncryptedStoreUnsupported
from .init import is_recognized_workspace
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
        vault = recover_vault(encoded, args.sync_id, decode_recovery_code(text))
        store.save(vault.vault_id, vault.vrk)
        print("[SUCCESS] Device key recovered. Remote synchronization was not changed.")
    except (EncryptedRemoteError, EncryptedStoreUnsupported) as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from None
    except (OSError, ValueError, UnicodeError, getpass.GetPassWarning):
        print(
            "[ERROR] Recovery input or device storage is unavailable.", file=sys.stderr
        )
        raise SystemExit(1) from None
