"""Independent server acceptance, durable receipts and bound recovery over HTTP."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path
from types import SimpleNamespace

from http_remote_server import HTTPRemoteServer
from workspace_external_contract import (
    STORES,
    TOKEN_A,
    TOKEN_B,
    access_rejections,
    concurrent_cas,
    isolation,
    restart,
)
from workspace_http_remote import ExternalServer
from workspace_http_remote_smoke import HTTPBackend
from workspace_reconcile_acceptance import exercise_behavior
from workspace_reconcile_smoke import _workspace

from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile import WorkspaceReconcileError, run_reconciliation
from aikito.workspace.remote_binding import RemoteAuth
from aikito.workspace.remote_factory import bind_remote, open_bound_remote
from aikito.workspace.remote_protocol import Operation
from aikito.workspace.resource_state import REMOTE_BINDING_STATE, PENDING_COMMIT_STATE


def main(base):
    os.environ.setdefault(
        "AIKITO_REMOTE_TEST_SERVER_CMD",
        shlex.join(
            [
                sys.executable,
                str(Path(__file__).with_name("http_remote_server.py")),
                "--root",
                "{root}",
                "--test-config",
                "{config}",
            ]
        ),
    )
    if "{config}" not in os.environ["AIKITO_REMOTE_TEST_SERVER_CMD"]:
        raise ValueError("Service acceptance requires {config}")
    with ExternalServer(base / "center", STORES) as service:
        isolation(service)
        access_rejections(service)
        restart(service)
        concurrent_cas(service)
        exercise_behavior(
            base / "acceptance", HTTPBackend(service.remote("beta", TOKEN_B))
        )
        remote = service.remote("alpha", TOKEN_A)
        local = _workspace(base / "local")
        home = base / "home"
        auth = RemoteAuth("bearer_env", "AIKITO_EXTERNAL_TEST_TOKEN")
        environ = {auth.env: TOKEN_A}
        # The relay forwards exact protocol bytes, then loses one accepted response.
        with HTTPRemoteServer(None, token=TOKEN_A) as relay:
            relay.handler = SimpleNamespace(
                handle=HTTPTransport(
                    service.endpoint("alpha"),
                    authorization="Bearer " + TOKEN_A,
                ).exchange
            )
            bind_remote(local, relay.url, auth, home=home, environ=environ)

            def run():
                run_reconciliation(
                    local,
                    open_bound_remote(local, environ=environ),
                    home,
                    dry_run=False,
                )

            run()
            note = local / "memory/notes/lost-response.md"
            note.write_text("durable bound recovery\n", encoding="utf-8")
            previous = remote.read().revision
            relay.arm("drop_after_handler", operation=Operation.COMMIT)
            try:
                run()
            except WorkspaceReconcileError:
                pass
            else:
                raise AssertionError("Lost response succeeded locally")
            pending = PendingCommitStore(local, home).load()
            assert pending is not None
            assert remote.read().revision == previous + 1
            assert (
                remote.resolve_commit(
                    pending.request.expected.sync_id,
                    pending.request.client_id,
                    pending.request.request_id,
                    pending.request.mutation_digest,
                )
                is not None
            )
            service.restart()
            run()
            assert remote.read().revision == previous + 1
            assert not (local / PENDING_COMMIT_STATE).exists()
            assert TOKEN_A not in (local / REMOTE_BINDING_STATE).read_text(
                encoding="utf-8"
            )
            fetched = remote.fetch(remote.read(), ["memory:notes/lost-response.md"])
            assert fetched["memory:notes/lost-response.md"].data == note.read_bytes()
        (base / "lost-response-checked.txt").write_text(
            "Recovery passed\n", encoding="utf-8"
        )
    (base / "checked.txt").write_text(
        "External server checks passed\n", encoding="utf-8"
    )
    print("[SUCCESS] External server isolation and recovery checks passed")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
