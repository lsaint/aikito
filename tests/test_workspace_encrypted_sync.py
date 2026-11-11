"""Ciphertext publication, logical Base confirmation and durable retries."""

import json
import threading
from contextlib import nullcontext
from unittest.mock import Mock

import pytest
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace

from aikito.render import render_remote_sync_plan
from aikito.workspace import encrypted_sync as sync
from aikito.workspace import reconcile, remote_limits
from aikito.workspace.auto_sync import AutoSyncScheduler
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_discovery import DISCOVERY_STATE
from aikito.workspace.encrypted_remote import (
    EncryptedRemoteError,
    decode_recovery_code,
    recover_vault,
)
from aikito.workspace.payload import (
    FilePayload,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommit, PendingCommitStore
from aikito.workspace.reconcile_budget import fetch_response_size
from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    save_remote_binding,
    remove_remote_binding,
)
from aikito.workspace.remote_factory import bind_remote
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_protocol import encode_commit_request
from aikito.workspace.remote_store import CommitOutcomeUnknown, SnapshotExpired
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.replica_state import load_replica_state
from aikito.workspace.resources import snapshot_workspace

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
def clients(tmp_path, monkeypatch, isolated_windows_key_permissions):
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
    assert b"global-memory:notes/shared.md" not in b"".join(remote.requests)
    assert "global-memory:notes/shared.md" not in load_replica_state(a)[0].base


def test_join_preserves_local_differences_without_upload_and_then_syncs(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    (a / NOTE).write_text("retired note\n")
    for workspace in (a, b):
        skill = workspace / "skills/example"
        (skill / "agents").mkdir(parents=True)
        (skill / "SKILL.md").write_text("# Example\n")
        (skill / "agents/openai.yaml").write_text("retired configuration\n")
    apply(a, ha)
    (a / NOTE).unlink()
    (a / "skills/example/agents/openai.yaml").unlink()
    (a / "memory/notes/current.md").write_text("remote current\n")
    (a / "config.toml").write_text('[review]\nlabel = "remote"\n')
    apply(a, ha)
    (b / NOTE).write_text("retired note\n")
    (b / "memory/notes/current.md").write_text("local different\n")
    (b / "config.toml").write_text('[review]\nlabel = "local"\nextra = true\n')
    before = remote.read()
    requests = len(remote.requests)
    preview = sync.run_encrypted_reconciliation(b, hb, dry_run=True)
    assert preview.pairing_remote and preview.pairing_backups
    assert all(item.target == "local" for item in preview.changes)
    assert "[LOCAL BACKUP]" in render_remote_sync_plan(preview, dry_run=True)
    assert not (b / ".local/sync-join").exists()
    assert not preview.backup_indexes and not preview.backup_paths
    assert load_replica_state(b)[0] is None
    result = apply(b, hb)
    assert not result.blocked and not result.conflicts
    assert remote.read() == before and len(remote.requests) == requests
    assert not (b / NOTE).exists()
    assert not (b / "skills/example/agents/openai.yaml").exists()
    assert (b / "memory/notes/current.md").read_text() == "remote current\n"
    assert 'label = "remote"' in (b / "config.toml").read_text()
    assert "extra" not in (b / "config.toml").read_text()
    backups = b / ".local/sync-join"
    assert any(p.read_text() == "retired note\n" for p in backups.rglob("shared.md"))
    assert any(
        p.read_text() == "retired configuration\n" for p in backups.rglob("openai.yaml")
    )
    assert any('label = "local"' in p.read_text() for p in backups.rglob("config.toml"))
    indexes = list(backups.glob("*/index.json"))
    assert len(indexes) == 1
    archive = indexes[0].parent
    assert "T" in archive.name and "Z-" in archive.name
    index = json.loads(indexes[0].read_text())
    assert index["resources"]["skill:example"]["path"] == "skills/example"
    assert index["resources"]["config:review.label"]["field"] == ["review", "label"]
    for entry in index["resources"].values():
        assert (archive / entry["path"]).exists()
    assert len(list(backups.rglob("config.toml"))) == 1
    assert not list(backups.rglob("resource.json"))
    assert "retired note" not in indexes[0].read_text()
    assert "retired configuration" not in indexes[0].read_text()
    assert "Local differences saved" in render_remote_sync_plan(result)
    assert result.backup_indexes == tuple(indexes)
    rendered = render_remote_sync_plan(result)
    assert str(archive) in rendered and str(indexes[0]) in rendered
    for identity, path in result.backup_paths:
        assert path.is_absolute() and path.exists()
        assert identity in rendered
        assert str(path) in rendered
    state, _ = load_replica_state(b)
    assert state.pairing_remote and not state.unpaired_ids
    unchanged = apply(b, hb)
    assert not unchanged.backup_indexes and not unchanged.backup_paths
    assert "Backup directory:" not in render_remote_sync_plan(unchanged)
    assert remote.read() == before
    # Restoring a reviewed backup after joining is an ordinary new local resource.
    saved = next(backups.rglob("shared.md"))
    (b / NOTE).write_bytes(saved.read_bytes())
    apply(b, hb)
    apply(a, ha)
    assert (a / NOTE).read_text() == "retired note\n"


@pytest.mark.parametrize("bind_empty", [True, False])
def test_first_sync_uses_current_vault_after_binding_gap(
    clients, monkeypatch, bind_empty
):
    remote, a, ha, b, hb = clients
    initialize(clients)
    remove_remote_binding(b, home=hb)
    monkeypatch.setattr(
        "aikito.workspace.remote_factory._open_remote", lambda *args, **kwargs: remote
    )
    (a / NOTE).write_text("deleted after binding\n")
    (b / NOTE).write_text("deleted after binding\n")
    if not bind_empty:
        apply(a, ha)
    bind_remote(
        b,
        "http://127.0.0.1:1/v1/remote",
        RemoteAuth("bearer_env", "TEST_TOKEN"),
        home=hb,
    )
    assert load_replica_state(b)[0] is None
    if bind_empty:
        apply(a, ha)
    (a / NOTE).unlink()
    apply(a, ha)
    (b / "memory/notes/late.md").write_text("appeared after binding\n")
    before = remote.read()
    result = apply(b, hb)
    assert not result.blocked and remote.read() == before
    assert not (b / NOTE).exists()
    assert not (b / "memory/notes/late.md").exists()
    backups = b / ".local/sync-join"
    assert next(backups.rglob("shared.md")).read_text() == "deleted after binding\n"
    assert next(backups.rglob("late.md")).read_text() == "appeared after binding\n"


def test_join_backup_failure_keeps_local_content_and_baseline(clients, monkeypatch):
    remote, a, ha, b, hb = clients
    initialize(clients)
    apply(a, ha)
    (b / NOTE).write_text("only local\n")
    before = remote.read()
    monkeypatch.setattr(
        reconcile, "_write_archive_bytes", Mock(side_effect=OSError("disk full"))
    )
    with pytest.raises(reconcile.WorkspaceReconcileError, match="disk full"):
        apply(b, hb)
    assert (b / NOTE).read_text() == "only local\n"
    assert load_replica_state(b)[0] is None
    assert remote.read() == before


def test_join_protects_a_previously_used_vault_with_no_remaining_resources(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    apply(a, ha)
    snapshot = remote.read()
    remote.commit(
        build_commit_request(
            "f" * 32,
            "e" * 32,
            snapshot,
            tuple(
                ResourceMutation(identity, descriptor, None, None)
                for identity, descriptor in snapshot.resources.items()
                if identity != FORMAT_RESOURCE_ID
            ),
        )
    )
    before = remote.read()
    assert set(before.resources) == {FORMAT_RESOURCE_ID} and before.revision > 1
    (b / NOTE).write_text("stale checkout\n")
    apply(b, hb)
    assert not (b / NOTE).exists() and remote.read() == before
    assert load_replica_state(b)[0].pairing_remote


def test_join_replans_before_backing_up_when_remote_changes(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    (a / NOTE).write_text("remote first\n")
    apply(a, ha)
    (b / NOTE).write_text("local old\n")
    adapter = sync.EncryptedRemote(remote, SYNC, b, hb)
    plan = reconcile.build_reconcile_plan(b, adapter)
    (a / NOTE).write_text("remote newer\n")
    apply(a, ha)
    with pytest.raises(reconcile.WorkspaceReconcileError):
        reconcile.apply_reconcile_plan(plan, hb, remote=adapter)
    assert (b / NOTE).read_text() == "local old\n"
    assert not (b / ".local/sync-join").exists()
    apply(b, hb)
    assert (b / NOTE).read_text() == "remote newer\n"


def test_rejected_initial_upload_adopts_vault_populated_by_another_device(
    clients, monkeypatch
):
    remote, a, ha, b, hb = clients
    initialize(clients)
    (a / NOTE).write_text("first device content\n")
    (b / "memory/notes/old-checkout.md").write_text("old checkout\n")
    commit = remote.commit
    adapter = sync.EncryptedRemote(remote, SYNC, a, ha)
    adapter.read()
    resource = snapshot_workspace(a).resources["global-memory:notes/shared.md"]
    identity, descriptor, payload = adapter.vault.encrypt(
        resource, FilePayload((a / NOTE).read_bytes()), SYNC
    )
    competing = build_commit_request(
        "f" * 32,
        "e" * 32,
        remote.read(),
        (ResourceMutation(identity, None, descriptor, payload),),
    )

    def race(request):
        monkeypatch.setattr(remote, "commit", commit)
        commit(competing)
        return commit(request)

    monkeypatch.setattr(remote, "commit", race)
    apply(b, hb)
    assert (b / NOTE).read_text() == "first device content\n"
    assert not (b / "memory/notes/old-checkout.md").exists()
    assert load_replica_state(b)[0].pairing_remote
    apply(a, ha)
    assert not (a / "memory/notes/old-checkout.md").exists()


def test_join_policy_survives_bounded_rounds_and_process_restart(clients, monkeypatch):
    remote, a, ha, b, hb = clients
    initialize(clients)
    for index in range(3):
        name = f"memory/notes/{index}.md"
        (a / name).write_text("remote" * 700)
        (b / name).write_text("local" * 700)
    apply(a, ha)
    before = remote.read()
    adapter = sync.EncryptedRemote(remote, SYNC, b, hb)
    adapter.read()
    one_note = adapter.vault.opaque_id("global-memory:notes/0.md")
    limit = fetch_response_size(before, (FORMAT_RESOURCE_ID, one_note)) + 128
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", limit)
    monkeypatch.setattr(reconcile, "MAX_RECONCILIATION_ROUNDS", 1)
    result = apply(b, hb)
    assert result.deferred and result.applied_items
    assert result.backup_indexes
    assert all(
        str(path) in render_remote_sync_plan(result) for path in result.backup_indexes
    )
    state, text = load_replica_state(b)
    assert state.pairing_remote and state.unpaired_ids
    assert json.loads(text)["pairing_remote"] is True
    monkeypatch.setattr(reconcile, "MAX_RECONCILIATION_ROUNDS", 1024)
    apply(b, hb)
    assert not load_replica_state(b)[0].unpaired_ids
    assert remote.read() == before
    for index in range(3):
        assert (b / f"memory/notes/{index}.md").read_text() == "remote" * 700


def test_background_join_establishes_baseline_even_when_all_content_matches(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    apply(a, ha)
    logs = []
    scheduler = AutoSyncScheduler(
        b, sync.EncryptedRemote(remote, SYNC, b, hb), home=hb, logger=logs.append
    )
    scheduler.step()
    assert load_replica_state(b)[0] is not None
    (b / NOTE).write_text("new content after baseline\n")
    scheduler.step()
    apply(a, ha)
    assert (a / NOTE).read_text() == "new content after baseline\n"


def test_background_join_keeps_local_only_resources_out_of_vault(clients):
    remote, a, ha, b, hb = clients
    initialize(clients)
    apply(a, ha)
    (b / NOTE).write_text("old snapshot\n")
    before = remote.read()
    logs = []
    AutoSyncScheduler(
        b, sync.EncryptedRemote(remote, SYNC, b, hb), home=hb, logger=logs.append
    ).step()
    assert remote.read() == before and not (b / NOTE).exists()
    assert any(".local/sync-join/" in message for message in logs)
    index_path = next((b / ".local/sync-join").glob("*/index.json"))
    assert any(str(index_path) in message for message in logs)
    assert any(str(index_path.parent / NOTE) in message for message in logs)


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
    assert "global-memory:notes/shared.md" in state.base
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
    assert "global-memory:notes/shared.md" not in load_replica_state(a)[0].base


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
            (
                ResourceMutation(
                    "global-memory:notes/existing.md", None, descriptor, payload
                ),
            ),
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
        (ResourceMutation("global-memory:notes/shared.md", None, descriptor, payload),),
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


@pytest.mark.parametrize("dry_run", [True, False])
def test_reconciliation_reports_real_stages(clients, dry_run):
    remote, a, ha, b, hb = clients
    initialize(clients)
    before = remote.read()
    stages = []
    sync.run_encrypted_reconciliation(a, ha, dry_run=dry_run, on_progress=stages.append)
    assert stages[0] == "Opening remote connection"
    assert any(
        stage.startswith("Reading and decrypting remote resources (")
        for stage in stages
    )
    assert "Comparing local resources" in stages
    if dry_run:
        assert remote.read() == before
        assert not any(stage.startswith("Uploading") for stage in stages)
    else:
        assert any(stage.startswith("Uploading") for stage in stages)


def test_recovery_backup_failure_prevents_initialization_commit(clients, monkeypatch):
    remote, workspace, home, _, _ = clients
    monkeypatch.setattr(
        sync, "save_recovery_code", Mock(side_effect=OSError("disk failure"))
    )
    deliver = Mock()
    with pytest.raises(OSError):
        sync.initialize_encrypted_workspace(workspace, home, deliver)
    assert remote.requests == []
    deliver.assert_not_called()
    assert not (workspace / sync.INITIALIZATION_STATE).exists()
