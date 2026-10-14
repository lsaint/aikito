"""Mixed snapshots, fenced ownership, rollback and encrypted journal recovery."""

import json
import subprocess
from dataclasses import replace
from unittest.mock import Mock

import pytest
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace

from aikito.workspace import encrypted_migration as migration
from aikito.workspace.device_keys import DeviceKeyStore
from aikito.workspace.encrypted_remote import (
    EncryptedRemoteError,
    decode_recovery_code,
    recover_vault,
    EncryptedVault,
)
from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    TreePayload,
    TreeEntry,
    TomlPayload,
    encode_payload,
    inspect_payload,
)
from aikito.workspace.reconcile import run_reconciliation
from aikito.workspace.remote_format import FORMAT_RESOURCE_ID
from aikito.workspace.remote_store import CommitOutcomeUnknown
from aikito.workspace.remote_protocol import encode_commit_request
from aikito.workspace.resource_state import local_resource_for_id
from aikito.workspace.encrypted_discovery import (
    discover_encrypted,
    EncryptedStoreMigrating,
)

pytest.importorskip("cryptography")
SYNC = "11223344556677889900aabbccddeeff"
NOTE = "memory/notes/migrate.md"


def git(root, *args):
    subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True)


def commit_workspace(root):
    git(root, "init")
    git(root, "config", "user.name", "Migration Test")
    git(root, "config", "user.email", "migration@example.invalid")
    (root / ".gitignore").write_text(".local/\n")
    git(root, "add", ".")
    git(root, "commit", "-m", "test: capture migration rollback baseline")


class ObservedRemote:
    def __init__(self):
        self.backend = InMemoryRemote(SYNC)
        self.requests = []
        self.failure = None
        self.snapshot_log = []

    def __getattr__(self, name):
        return getattr(self.backend, name)

    def commit(self, request):
        self.requests.append(encode_commit_request(request))
        failure, self.failure = self.failure, None
        if failure == "before":
            raise CommitOutcomeUnknown("Simulated uncertainty")
        result = self.backend.commit(request)
        snapshot = self.backend.read()
        assert all(
            set(d.references) <= snapshot.resources.keys()
            for d in snapshot.resources.values()
        )
        self.snapshot_log.append(snapshot)
        if failure == "after":
            raise CommitOutcomeUnknown("Simulated response loss")
        return result


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.delenv("APPDATA", raising=False)
    remote = ObservedRemote()
    workspace = _workspace(tmp_path / "workspace")
    home = tmp_path / "home"
    home.mkdir()
    (workspace / NOTE).write_text("migration private payload\n")
    (workspace / "projects/demo/memory/notes/shared.md").write_text(
        "shared project payload\n"
    )
    directory = workspace / "skills/example"
    directory.mkdir()
    (directory / "SKILL.md").write_text("# Example\n")
    (directory / "run.sh").write_text("echo test\n")
    (directory / "run.sh").chmod(0o755)
    run_reconciliation(workspace, remote, home, dry_run=False)
    commit_workspace(workspace)
    return remote, workspace, home


def engine(setup, **kwargs):
    remote, workspace, home = setup
    return migration.Migration(remote, SYNC, workspace, home, **kwargs)


def test_forward_preserves_graph_and_byte_containers_then_rollback_restores(setup):
    remote, workspace, home = setup
    original = remote.read()
    payloads = remote.fetch(original, original.resources)
    codes = []
    result = engine(setup, deliver_recovery=codes.append).run()
    assert result.status == "encrypted"
    encrypted = remote.read()
    assert all(
        k == FORMAT_RESOURCE_ID or k.startswith("e1:") for k in encrypted.resources
    )
    assert any(
        any(k.startswith("e1:") for k in s.resources)
        and any(k.startswith("project-memory:") for k in s.resources)
        for s in remote.snapshot_log
    )
    assert len(codes) == 1 and not (workspace / migration.MIGRATION_STATE).exists()
    assert (workspace / NOTE).read_text() == "migration private payload\n"
    result = engine(setup, rollback=True).run()
    assert result.status == "rolled-back"
    restored = remote.read()
    assert dict(restored.resources) == dict(original.resources)
    assert remote.fetch(restored, restored.resources) == payloads
    assert restored.sync_id == original.sync_id
    assert not (workspace / migration.MIGRATION_STATE).exists()


def test_preview_has_no_disk_or_remote_mutations(setup):
    remote, workspace, home = setup
    before = remote.read()
    deliver = Mock(side_effect=AssertionError("No code in preview"))
    assert (
        engine(setup, dry_run=True, deliver_recovery=deliver).run().status
        == "preflight"
    )
    assert remote.read() == before
    assert not (workspace / migration.MIGRATION_STATE).exists()
    assert not DeviceKeyStore(home=home).root.exists()


@pytest.mark.parametrize("failure", ["before", "after"])
def test_marker_uncertainty_resumes_exact_request_without_new_code(setup, failure):
    remote, workspace, home = setup
    remote.failure = failure
    codes = []
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup, deliver_recovery=codes.append).run()
    path = workspace / migration.MIGRATION_STATE
    encoded = path.read_bytes()
    assert (
        b"migration private payload" not in encoded and codes[0].encode() not in encoded
    )
    exact = remote.requests[-1]
    deliver = Mock(side_effect=AssertionError("No second code"))
    assert engine(setup, deliver_recovery=deliver).run().status == "encrypted"
    assert remote.requests.count(exact) == (2 if failure == "before" else 1)
    deliver.assert_not_called()


@pytest.mark.parametrize("failure", ["before", "after"])
def test_copy_uncertainty_is_ciphertext_and_resumable(setup, failure):
    remote, workspace, home = setup
    codes = []
    assert (
        engine(setup, deliver_recovery=codes.append).run(maximum=1).status == "paused"
    )
    remote.failure = failure
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup).run()
    exact = remote.requests[-1]
    assert engine(setup).run().status == "encrypted"
    assert remote.requests.count(exact) == (2 if failure == "before" else 1)


def test_takeover_reconstructs_from_remote_without_old_journal_and_fences_owner(
    setup, tmp_path
):
    remote, workspace, home = setup
    codes = []
    assert (
        engine(setup, deliver_recovery=codes.append).run(maximum=1).status == "paused"
    )
    snapshot = remote.read()
    header = remote.fetch(snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID].data
    vault = recover_vault(header, SYNC, decode_recovery_code(codes[0]))
    other = _workspace(tmp_path / "other")
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    commit_workspace(other)
    DeviceKeyStore(home=other_home, workspace=other).save(vault.vault_id, vault.vrk)
    challenger = migration.Migration(remote, SYNC, other, other_home, takeover=True)
    assert challenger.run(maximum=1).status == "paused"
    current = remote.read()
    updated = remote.fetch(current, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID].data
    assert migration.verify_metadata(updated, vault.vrk)["migration"]["epoch"] == 2
    count = len(remote.requests)
    with pytest.raises(migration.MigrationFenced):
        engine(setup).run()
    assert len(remote.requests) == count
    assert (
        migration.Migration(remote, SYNC, other, other_home).run().status == "encrypted"
    )


def test_unowned_migration_pauses_other_devices_before_writes(setup, tmp_path):
    remote, workspace, home = setup
    codes = []
    engine(setup, deliver_recovery=codes.append).run(maximum=1)
    snapshot = remote.read()
    vault = recover_vault(
        remote.fetch(snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID].data,
        SYNC,
        decode_recovery_code(codes[0]),
    )
    other = _workspace(tmp_path / "other")
    other_home = tmp_path / "other-home"
    other_home.mkdir()
    commit_workspace(other)
    keys = DeviceKeyStore(home=other_home, workspace=other)
    keys.save(vault.vault_id, vault.vrk)
    before = remote.read()
    with pytest.raises(migration.MigrationFenced):
        migration.Migration(remote, SYNC, other, other_home).run()
    with pytest.raises(EncryptedStoreMigrating):
        discover_encrypted(remote, keys)
    assert remote.read() == before and not (other / migration.MIGRATION_STATE).exists()


def test_rollback_during_partial_migration_restores_original_graph(setup):
    remote, workspace, _ = setup
    original = remote.read()
    engine(setup, deliver_recovery=lambda code: None).run(maximum=2)
    assert engine(setup, rollback=True).run().status == "rolled-back"
    assert dict(remote.read().resources) == dict(original.resources)


def test_rollback_journal_never_persists_plaintext_request(setup):
    remote, workspace, _ = setup
    engine(setup, deliver_recovery=lambda code: None).run()
    assert engine(setup, rollback=True).run(maximum=1).status == "paused"
    remote.failure = "before"
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup, rollback=True).run()
    assert (
        b"migration private payload"
        not in (workspace / migration.MIGRATION_STATE).read_bytes()
    )
    assert engine(setup, rollback=True).run().status == "rolled-back"


def test_dirty_git_or_changed_local_snapshot_fails_before_marker(setup):
    remote, workspace, _ = setup
    before = remote.read()
    (workspace / NOTE).write_text("unsynchronized edit\n")
    with pytest.raises(migration.MigrationPreflightFailed):
        engine(setup, deliver_recovery=Mock()).run()
    assert remote.read() == before and FORMAT_RESOURCE_ID not in before.resources


def test_ciphertext_expansion_rejected_before_key_or_journal_write(setup, monkeypatch):
    remote, workspace, home = setup
    from aikito.workspace import remote_limits

    monkeypatch.setattr(remote_limits, "MAX_RESOURCE_PAYLOAD_BYTES", 200)
    before = remote.read()
    with pytest.raises(migration.MigrationBudgetExceeded):
        engine(setup, deliver_recovery=Mock()).run()
    assert remote.read() == before
    assert (
        not DeviceKeyStore(home=home).root.exists()
        and not (workspace / migration.MIGRATION_STATE).exists()
    )


def test_tampered_encrypted_journal_stops_without_remote_change(setup):
    remote, workspace, _ = setup
    engine(setup, deliver_recovery=lambda code: None).run(maximum=1)
    path = workspace / migration.MIGRATION_STATE
    raw = json.loads(path.read_bytes())
    raw["ciphertext"] = "AAAA"
    path.write_bytes(migration._canonical(raw))
    before = remote.read()
    with pytest.raises(EncryptedRemoteError):
        engine(setup).run()
    assert remote.read() == before


def test_release_gate_fails_before_opening_remote(monkeypatch, tmp_path):
    monkeypatch.setattr(migration, "MINIMUM_GUARD_RELEASE", None)
    opened = Mock(side_effect=AssertionError("No real remote operation before release"))
    monkeypatch.setattr(migration, "_open_remote", opened)
    with pytest.raises(migration.MigrationReleaseRequired):
        migration.run_migration(tmp_path, tmp_path, deliver_recovery=Mock())
    opened.assert_not_called()


@pytest.mark.parametrize(
    "identity,payload",
    [
        ("memory:notes/size.md", FilePayload("你好\n".encode())),
        ("project:demo", MemberPayload()),
        ("config:feature.flag", TomlPayload(("feature", "flag"), "value = true\n")),
        (
            "skill:example",
            TreePayload(
                (
                    TreeEntry("SKILL.md", b"# Example\n"),
                    TreeEntry("run.sh", b"echo test\n", True),
                )
            ),
        ),
    ],
)
def test_envelope_budget_size_is_exact(identity, payload):
    initial = local_resource_for_id(
        identity, "" if isinstance(payload, MemberPayload) else "0" * 64
    )
    fingerprint, references = inspect_payload(initial, payload)
    resource = replace(initial, fingerprint=fingerprint, references=references)
    vault = EncryptedVault("1" * 32, bytes(range(32)))
    encrypted = vault.encrypt(resource, payload, SYNC)
    assert (
        migration.encrypted_size(resource, len(encode_payload(payload)))
        == encrypted[1].size
    )


@pytest.mark.parametrize("rollback", [False, True])
@pytest.mark.parametrize("failure", ["before", "after"])
def test_final_marker_receipt_survives_response_loss(
    setup, monkeypatch, rollback, failure
):
    remote, workspace, _ = setup
    original = remote.read()
    if rollback:
        engine(setup, deliver_recovery=lambda code: None).run()
    commit = remote.commit
    armed = True

    def lose_final(request):
        nonlocal armed
        mutations = request.mutations
        if armed and len(mutations) == 1 and mutations[0].id == FORMAT_RESOURCE_ID:
            mutation = mutations[0]
            final = (
                mutation.after is None
                or json.loads(mutation.payload.data)["format"] == "encrypted-v1"
            )
            if final:
                armed = False
                remote.failure = failure
        return commit(request)

    monkeypatch.setattr(remote, "commit", lose_final)
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup, rollback=rollback, deliver_recovery=lambda code: None).run()
    exact = remote.requests[-1]
    result = engine(setup, rollback=rollback).run()
    assert result.status == ("rolled-back" if rollback else "encrypted")
    assert remote.requests.count(exact) == (2 if failure == "before" else 1)
    assert not (workspace / migration.MIGRATION_STATE).exists()
    if rollback:
        assert dict(remote.read().resources) == dict(original.resources)


def test_explicit_retakeover_preserves_fenced_pending_request(setup, tmp_path):
    remote, workspace, _ = setup
    codes = []
    engine(setup, deliver_recovery=codes.append).run(maximum=1)
    remote.failure = "before"
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup).run()
    uncertain = remote.requests[-1]
    journal = (workspace / migration.MIGRATION_STATE).read_bytes()
    snapshot = remote.read()
    header = remote.fetch(snapshot, (FORMAT_RESOURCE_ID,))[FORMAT_RESOURCE_ID].data
    vault = recover_vault(header, SYNC, decode_recovery_code(codes[0]))
    other = _workspace(tmp_path / "challenger")
    other_home = tmp_path / "challenger-home"
    other_home.mkdir()
    commit_workspace(other)
    DeviceKeyStore(home=other_home, workspace=other).save(vault.vault_id, vault.vrk)
    assert (
        migration.Migration(remote, SYNC, other, other_home, takeover=True)
        .run(maximum=1)
        .epoch
        == 2
    )
    with pytest.raises(migration.MigrationFenced):
        engine(setup).run()
    result = engine(setup, takeover=True).run()
    assert result.status == "encrypted" and result.epoch == 3
    assert remote.requests.count(uncertain) == 1
    preserved = list(
        (workspace / migration.MIGRATION_STATE).parent.glob(
            "encrypted-migration.json.fenced-*"
        )
    )
    assert len(preserved) == 1 and preserved[0].read_bytes() == journal


@pytest.mark.parametrize("failure", ["before", "after"])
def test_preview_never_dispatches_or_acknowledges_uncertain_pending(setup, failure):
    remote, workspace, _ = setup
    engine(setup, deliver_recovery=lambda code: None).run(maximum=1)
    remote.failure = failure
    with pytest.raises(CommitOutcomeUnknown):
        engine(setup).run()
    before, count = remote.read(), len(remote.requests)
    journal = (workspace / migration.MIGRATION_STATE).read_bytes()
    with pytest.raises(migration.MigrationPendingUnresolved):
        engine(setup, dry_run=True).run()
    assert remote.read() == before and len(remote.requests) == count
    assert (workspace / migration.MIGRATION_STATE).read_bytes() == journal
