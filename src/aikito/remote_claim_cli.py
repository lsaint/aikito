"""Explicit account intent for an existing Store ownership proof."""

import argparse
import sys
from pathlib import Path

from .init import is_recognized_workspace
from .workspace.remote_claim import RemoteClaimError, claim_bound_store


def cmd_remote_claim(args: argparse.Namespace):
    try:
        workspace = Path(args.workspace).expanduser().resolve()
        if not is_recognized_workspace(workspace):
            raise RemoteClaimError("claim_workspace_invalid")
        claim_bound_store(
            workspace, Path(args.challenge), args.account, home=Path.home()
        )
        print("[SUCCESS] Store ownership confirmed for the selected account.")
    except RemoteClaimError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        raise SystemExit(1) from None
