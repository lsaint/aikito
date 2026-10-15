"""Ciphertext publication, logical Base confirmation and durable retries."""

import threading
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace

from aikito.workspace import encrypted_sync as sync
from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import (
    decode_recovery_code,
    recover_vault,
    EncryptedRemoteError,
)
from aikito.workspace.encrypted_discovery import DISCOVERY_STATE
from aikito.workspace.remote_binding import (
    RemoteBinding,
    RemoteAuth,
    save_remote_binding,
)
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_store import CommitOutcomeUnknown, SnapshotExpired
from aikito.workspace.remote_protocol import encode_commit_request
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommitStore, PendingCommit
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.replica_state import load_replica_state

pytest.importorskip("cryptography")
SYNC = "11223344556677889900aabbccddeeff"
NOTE = "memory/notes/shared.md"


class FaultRemote:
    def __init__(self, backend):
        self.backend = backend
        self.failure = None
        self.requests = []

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def commit(self, request):
        self.requests.append(encode_commit_request(request))
        failure, self.failure = self.failure, None
        if failure == "before":
            raise CommitOutcomeUnknown("Simulated delivery uncertainty")
        result = self.backend.commit(request)
        if failure == "after":
            raise CommitOutcomeUnknown("Simulated lost response")
        return result


@pytest.fixture
def clients(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    remote = FaultRemote(InMemoryRemote(SYNC))
    monkeypatch.setattr(sync, "_open_remote", lambda *args, **kwargs: remote)
    a, b = _workspace(tmp_path / "a"), _workspace(tmp_path / "b")
    ha, hb = tmp_path / "ha", tmp_path / "hb"
    ha.mkdir()
    hb.mkdir()
    for workspace, home in ((a, ha), (b, hb)):
        save_remote_binding(
            workspace,
            RemoteBinding(
                1,
                "http://127.0.0.1:1/v1/remote",
                SYNC,
                RemoteAuth("bearer_env", "TEST_TOKEN"),
            ),
            home=home,
        )
    return remote, a, ha, b, hb


def initialize(clients):
    remote, a, ha, b, hb = clients
    codes = []
    vault_id = sync.initialize_encrypted_workspace(a, ha, codes.append)
    snapshot = remote.read()
    header = remote.fetch(snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID]
    vault = recover_vault(header.data, SYNC, decode_recovery_code(codes[0]))
    DeviceKeyStore(home=hb, workspace=b).save(vault.vault_id, vault.vrk)
    return vault_id, codes[0]


def apply(workspace, home):
    return sync.run_encrypted_reconciliation(workspace, home, dry_run=False)


def test_two_devices_create_update_delete_and_noop_reuse_ciphertext(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    (a / NOTE).write_text("initial secret payload\n")
    result = apply(a, ha)
    assert not result.blocked
    apply(b, hb)
    assert (b / NOTE).read_text() == "initial secret payload\n"
    before = remote.read()
    requests = len(remote.requests)
    apply(a, ha)
    assert remote.read() == before and len(remote.requests) == requests
    (b / NOTE).write_text("second device edit\n")
    apply(b, hb)
    apply(a, ha)
    assert (a / NOTE).read_text() == "second device edit\n"
    (a / NOTE).unlink()
    apply(a, ha)
    apply(b, hb)
    assert not (b / NOTE).exists()
    assert all(
        k == FORMAT_RESOURCE_ID or k.startswith("e1:") for k in remote.read().resources
    )
    assert b"initial secret payload" not in b"".join(remote.requests)
    assert b"memory:notes/shared.md" not in b"".join(remote.requests)
    assert "memory:notes/shared.md" not in load_replica_state(a)[0].base


def test_preview_does_not_write_discovery_or_replica_state(clients):
    remote, a, ha, _, _ = clients
    initialize(clients)
    before = remote.read()
    count = len(remote.requests)
    result = sync.run_encrypted_reconciliation(a, ha, dry_run=True)
    assert result.changes
    assert remote.read() == before and len(remote.requests) == count
    assert not (a / DISCOVERY_STATE).exists()
    assert load_replica_state(a)[0] is None


@pytest.mark.parametrize("failure", ["before", "after"])
def test_pending_is_ciphertext_and_recovery_resends_exact_bytes(clients, failure):
    remote, a, ha, _, _ = clients
    initialize(clients)
    apply(a, ha)
    (a / NOTE).write_text("pending-only secret content\n")
    remote.failure = failure
    with pytest.raises(ValueError):
        apply(a, ha)
    store = PendingCommitStore(a, ha)
    pending = store.load()
    assert pending.vault_id and PendingCommit.decode(pending.encode()) == pending
    assert b"pending-only secret content" not in pending.encode().encode()
    first = remote.requests[-1]
    # The original device may edit after a request was persisted. Recovery
    # confirms captured uploads without overwriting those later local edits.
    (a / NOTE).write_text("later local edit\n")
    fresh = sync.EncryptedRemote(remote, SYNC, a, ha)
    assert recover_pending(a, fresh, ha)
    assert store.load() is None and (a / NOTE).read_text() == "later local edit\n"
    if failure == "before":
        assert remote.requests[-1] == first
    else:
        assert remote.requests.count(first) == 1
    state, _ = load_replica_state(a)
    assert "memory:notes/shared.md" in state.base
    assert not any(k.startswith("e1:") for k in state.base)


def test_delete_pending_recovery_confirms_logical_absence(clients):
    remote, a, ha, _, _ = clients
    initialize(clients)
    (a / NOTE).write_text("delete me\n")
    apply(a, ha)
    (a / NOTE).unlink()
    remote.failure = "after"
    with pytest.raises(ValueError):
        apply(a, ha)
    assert recover_pending(a, sync.EncryptedRemote(remote, SYNC, a, ha), ha)
    assert "memory:notes/shared.md" not in load_replica_state(a)[0].base


def test_initialization_response_loss_keeps_exact_request_and_delivers_once(clients):
    remote, a, ha, _, _ = clients
    codes = []
    remote.failure = "after"
    with pytest.raises(CommitOutcomeUnknown):
        sync.initialize_encrypted_workspace(a, ha, codes.append)
    path = a / sync.INITIALIZATION_STATE
    encoded = path.read_bytes()
    assert codes[0].encode() not in encoded
    before = remote.read()
    count = len(remote.requests)
    deliver = Mock(side_effect=AssertionError("Never redisplay recovery code"))
    vault_id = sync.initialize_encrypted_workspace(a, ha, deliver)
    assert remote.read() == before and len(remote.requests) == count
    assert not path.exists() and vault_id in DeviceKeyStore(home=ha).path(vault_id).name
    deliver.assert_not_called()


def test_concurrent_initialization_has_exactly_one_vault(clients, monkeypatch):
    # Separate devices do not share the process-wide workspace writer lock.
    # The HTTP smoke covers actual independent device processes.
    monkeypatch.setattr(sync, "WorkspaceWriterLock", lambda home: nullcontext())
    remote, a, ha, b, hb = clients
    barrier = threading.Barrier(2)
    results = []
    codes = {}

    def run(workspace, home):
        def deliver(code):
            codes[str(workspace)] = code
            barrier.wait(timeout=10)

        try:
            results.append(
                (
                    workspace,
                    sync.initialize_encrypted_workspace(workspace, home, deliver),
                )
            )
        except SnapshotExpired:
            results.append((workspace, None))

    threads = [threading.Thread(target=run, args=pair) for pair in ((a, ha), (b, hb))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    assert not any(t.is_alive() for t in threads)
    assert len(results) == 2 and sum(vault is not None for _, vault in results) == 1
    snapshot = remote.read()
    assert set(snapshot.resources) == {FORMAT_RESOURCE_ID}
    header = remote.fetch(snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID].data
    for workspace, vault_id in results:
        if vault_id is not None:
            assert (
                recover_vault(
                    header, SYNC, decode_recovery_code(codes[str(workspace)])
                ).vault_id
                == vault_id
            )
        else:
            with pytest.raises(EncryptedRemoteError):
                recover_vault(header, SYNC, decode_recovery_code(codes[str(workspace)]))
    assert (
        not (a / sync.INITIALIZATION_STATE).exists()
        and not (b / sync.INITIALIZATION_STATE).exists()
    )


def test_nonempty_plaintext_store_is_never_initialized_or_migrated(clients):
    remote, a, ha, _, _ = clients
    payload = FilePayload(b"existing plaintext\n")
    descriptor = ResourceDescriptor(
        "0" * 64, payload_hash(payload), len(encode_payload(payload))
    )
    remote.commit(
        build_commit_request(
            "setup",
            "setup",
            remote.read(),
            (ResourceMutation("memory:notes/existing.md", None, descriptor, payload),),
        )
    )
    before = remote.read()
    deliver = Mock()
    with pytest.raises(EncryptedRemoteError):
        sync.initialize_encrypted_workspace(a, ha, deliver)
    assert remote.read() == before and not (a / sync.INITIALIZATION_STATE).exists()
    deliver.assert_not_called()


def test_failed_secure_output_cannot_publish_a_format_header(clients):
    remote, a, ha, _, _ = clients
    with pytest.raises(OSError):
        sync.initialize_encrypted_workspace(
            a, ha, Mock(side_effect=OSError("closed descriptor"))
        )
    assert not remote.read().resources and not (a / sync.INITIALIZATION_STATE).exists()


def test_opaque_container_cost_blocks_plaintext_that_would_fit(clients, monkeypatch):
    remote, a, ha, _, _ = clients
    initialize(clients)
    apply(a, ha)
    (a / NOTE).write_text("secret budget payload\n" * 70)
    # Ciphertext wraps an inner portable payload and is larger than plaintext.
    from aikito.workspace import remote_limits

    adapter = sync.EncryptedRemote(remote, SYNC, a, ha)
    assert adapter.prepare()
    payload = FilePayload((a / NOTE).read_bytes())
    descriptor = ResourceDescriptor(
        "0" * 64, payload_hash(payload), len(encode_payload(payload))
    )
    plain = build_commit_request(
        "0" * 32,
        "1" * 32,
        adapter.logical,
        (ResourceMutation("memory:notes/shared.md", None, descriptor, payload),),
    )
    assert len(encode_commit_request(plain)) < 7000
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 7000)
    before = remote.read()
    preview = sync.run_encrypted_reconciliation(a, ha, dry_run=True)
    assert preview.blocked_items or preview.deferred
    assert remote.read() == before and PendingCommitStore(a, ha).load() is None


def test_later_local_edit_wins_and_replaced_remote_content_is_archived(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    (a / NOTE).write_text("base\n")
    apply(a, ha)
    apply(b, hb)
    (a / NOTE).write_text("earlier accepted edit\n")
    apply(a, ha)
    (b / NOTE).write_text("later local edit\n")
    apply(b, hb)
    apply(a, ha)
    assert (a / NOTE).read_text() == "later local edit\n"
    archives = b / ".local/sync-conflicts"
    assert any(
        p.read_text(encoding="utf-8") == "earlier accepted edit\n"
        for p in archives.rglob("*.md")
    )


def test_skill_tree_bytes_and_execution_flags_survive_encrypted_sync(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    directory = a / "skills/example"
    directory.mkdir(parents=True)
    (directory / "SKILL.md").write_text("# Example\n")
    (directory / "run.sh").write_bytes(b"echo example\n")
    (directory / "run.sh").chmod(0o755)
    apply(a, ha)
    apply(b, hb)
    assert (b / "skills/example/run.sh").read_bytes() == b"echo example\n"
    if __import__("os").name != "nt":
        assert (b / "skills/example/run.sh").stat().st_mode & 0o111

