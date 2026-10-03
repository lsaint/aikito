"""Bounded rounds preserve dependency safety, pairing and recovery semantics."""

from __future__ import annotations


import sys
import time
import weakref

import pytest

from aikito.workspace import reconcile, remote_limits, remote_protocol
from aikito.workspace.commit_recovery import recover_pending
from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    TomlPayload,
    TreePayload,
    TreeEntry,
    ResourceDescriptor,
    ResourceMutation,
    encode_payload,
    payload_hash,
)
from aikito.workspace.pending_commit import PendingCommitStore
from aikito.workspace.reconcile_budget import (
    atomic_groups,
    fetch_response_size,
    operation_dependencies,
    select_round,
)
from aikito.workspace.remote_protocol import (
    commit_envelope_size,
    mutation_size,
    encode_commit_request,
    encode_fetch_request,
    encode_fetch_response,
)
from aikito.workspace.remote_store import (
    CommitOutcomeUnknown,
    RemoteSnapshot,
    ReceiptCursor,
    SnapshotExpired,
)
from aikito.workspace.remote_wire import build_commit_request
from aikito.workspace.replica_state import ReplicaState, load_replica_state
from aikito.workspace.resource_state import REPLICA_STATE
from aikito.workspace.toml_render import TomlValue
from aikito.workspace.resources import snapshot_workspace
from aikito.skill_state import WorkspaceWriterLock
from test_workspace_reconcile_fetch_scope import RecordingStore
from workspace_loopback_remote import LoopbackTransport
from workspace_memory_remote import InMemoryRemote
from workspace_reconcile_smoke import _workspace
from aikito.workspace.serialized_remote import SerializedRemoteStore


@pytest.fixture(params=[False, True])
def bounded(request, tmp_path, monkeypatch):
    backend = InMemoryRemote()
    remote = (
        SerializedRemoteStore(LoopbackTransport(backend).exchange)
        if request.param
        else backend
    )
    store = RecordingStore(remote)
    left, right = _workspace(tmp_path / "left"), _workspace(tmp_path / "right")
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 16 * 1024)
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 16 * 1024)
    return left, right, store, tmp_path / "home"


def apply(local, store, home, **kwargs):
    return reconcile.run_reconciliation(local, store, home, dry_run=False, **kwargs)


def notes(local, count=12, project=False):
    directory = local / ("projects/demo/memory/notes" if project else "memory/notes")
    for index in range(count):
        (directory / f"{index:02}.md").write_text(
            f"note {index}\n" + "x" * 2500, encoding="utf-8"
        )


def test_payload_total_exceeds_limit_but_each_round_fits(bounded):
    left, right, store, home = bounded
    notes(left)
    uploads = []
    original = store.commit

    def commit(request):
        assert (
            len(encode_commit_request(request))
            <= remote_limits.MAX_REMOTE_REQUEST_BYTES
        )
        uploads.append(request)
        return original(request)

    store.commit = commit
    first = apply(left, store, home)
    assert first.rounds > 1 and not first.deferred and not first.stop_reason
    assert len(first.applied_items) >= 12
    assert len(uploads) == first.rounds
    store.requests.clear()
    second = apply(right, store, home)
    assert second.rounds > 1 and not second.conflicts
    assert len(store.requests) == second.rounds
    snapshot = store.read()
    for batch in tuple(store.requests):
        assert (
            len(encode_fetch_request(snapshot, batch))
            <= remote_limits.MAX_REMOTE_REQUEST_BYTES
        )
        assert (
            len(encode_fetch_response(store.fetch(snapshot, batch)))
            <= remote_limits.MAX_REMOTE_RESPONSE_BYTES
        )
    assert snapshot_workspace(left).resources == snapshot_workspace(right).resources
    assert not load_replica_state(right)[0].unpaired_ids


@pytest.mark.parametrize("count", [12, 36])
def test_multiround_download_maps_center_once_per_phase(bounded, monkeypatch, count):
    left, right, store, home = bounded
    notes(left, count=count)
    apply(left, store, home)
    calls = []
    original = reconcile._center_resources

    def center_resources(snapshot):
        calls.append(1)
        return original(snapshot)

    monkeypatch.setattr(reconcile, "_center_resources", center_resources)
    store.requests.clear()
    result = apply(right, store, home)
    assert result.rounds > 1 and not result.stop_reason and not result.deferred
    # Initial planning, apply revalidation, write preparation and convergence
    # each map the manifest once per round, regardless of the download count.
    assert len(calls) == 4 * result.rounds
    assert len(store.requests) == result.rounds
    assert snapshot_workspace(left).resources == snapshot_workspace(right).resources


def test_large_project_does_not_form_one_atomic_group(bounded):
    left, right, store, home = bounded
    notes(left, project=True)
    assert apply(left, store, home).rounds > 1
    result = apply(right, store, home)
    assert result.rounds > 1
    assert not any(item.action == "BLOCKED" for item in result.items)
    assert (right / "projects/demo/memory/notes/11.md").exists()


def test_preview_selects_before_fetch_and_apply_reuses_cache(bounded):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    plan = reconcile.build_reconcile_plan(right, store)
    assert plan.deferred and len(store.requests) == 1
    cached = store.requests.copy()
    assert (
        reconcile.build_reconcile_plan(right, store, _cached=plan.payload_cache) == plan
    )
    assert store.requests == cached
    reconcile.apply_reconcile_plan(plan, home, remote=store)
    assert store.requests == cached
    state = load_replica_state(right)[0]
    deferred = {item.id for item in plan.deferred}
    assert not deferred & state.base.keys()
    assert deferred <= state.unpaired_ids
    apply(right, store, home)
    assert not load_replica_state(right)[0].unpaired_ids


def test_lost_response_confirms_only_uploads_and_keeps_unpaired(bounded, monkeypatch):
    left, _, store, home = bounded
    notes(left)
    plan = reconcile.build_reconcile_plan(left, store)
    deferred = {item.id for item in plan.deferred}
    original = store.commit

    def lose(request):
        original(request)
        raise CommitOutcomeUnknown("lost response")

    monkeypatch.setattr(store, "commit", lose)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="lost response"):
        reconcile.apply_reconcile_plan(plan, home, remote=store)
    pending = PendingCommitStore(left, home).load()
    assert deferred <= set(pending.excluded_resource_ids)
    assert not deferred & set(pending.safe_resource_ids)
    monkeypatch.setattr(store, "commit", original)
    with WorkspaceWriterLock(home):
        assert recover_pending(left, store, home)
    state = load_replica_state(left)[0]
    assert deferred <= state.unpaired_ids
    assert not deferred & state.base.keys()
    apply(left, store, home)
    assert not load_replica_state(left)[0].unpaired_ids
    assert not reconcile.build_reconcile_plan(left, store).changes


def test_template_ancestor_survives_deferred_pairing(bounded, monkeypatch):
    left, right, store, home = bounded
    for local in (left, right):
        (local / "projects/demo/AGENTS.md").write_text("", encoding="utf-8")
    (left / "projects/demo/AGENTS.md").write_text(
        "remote instructions\n" + "x" * 2500, encoding="utf-8"
    )
    notes(left)
    apply(left, store, home)
    monkeypatch.setattr(reconcile, "MAX_RECONCILIATION_ROUNDS", 1)
    first = apply(right, store, home)
    identity = "project-instructions:demo"
    assert identity in {item.id for item in first.deferred}
    assert identity in load_replica_state(right)[0].unpaired_ids
    monkeypatch.setattr(reconcile, "MAX_RECONCILIATION_ROUNDS", 1024)
    result = apply(right, store, home)
    assert not result.conflicts
    assert (right / "projects/demo/AGENTS.md").read_bytes() == (
        left / "projects/demo/AGENTS.md"
    ).read_bytes()


def test_first_pairing_preserves_template_absent_from_remote(bounded, monkeypatch):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    (right / "projects/demo/AGENTS.md").write_text("", encoding="utf-8")
    # Leave an explicit unfinished ID as a restart would; a template that is
    # absent remotely must be uploaded, never deleted in a later round.
    state = ReplicaState(
        store.read().sync_id,
        "0" * 32,
        store.read().revision,
        {},
        unpaired_ids=frozenset({"project-instructions:demo"}),
    )
    path = right / REPLICA_STATE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(state.encode(), encoding="utf-8")
    result = apply(right, store, home)
    assert not result.conflicts
    assert (right / "projects/demo/AGENTS.md").exists()
    assert "project-instructions:demo" in store.read().resources


def test_resolutions_are_consumed_only_when_applied(bounded):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    apply(right, store, home)
    choices = {}
    for index in range(12):
        relative = f"memory/notes/{index:02}.md"
        (left / relative).write_text("left\n" + "x" * 2500, encoding="utf-8")
        (right / relative).write_text("right\n" + "x" * 2500, encoding="utf-8")
        choices[f"memory:notes/{index:02}.md"] = "remote"
    apply(left, store, home)
    result = apply(right, store, home, resolutions=choices)
    assert result.rounds > 1 and not result.conflicts and not result.deferred
    assert {item.id for item in result.applied_items} == set(choices)


def test_manifest_limit_fails_before_any_fetch_or_state(bounded, monkeypatch):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    store.requests.clear()
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", 100)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="manifest"):
        apply(right, store, home)
    assert store.requests == []
    assert load_replica_state(right)[0] is None


def test_cache_invalidates_changed_hash_without_refetching_stale_plan(bounded):
    left, right, store, home = bounded
    notes(left, count=1)
    apply(left, store, home)
    plan = reconcile.build_reconcile_plan(right, store)
    (left / "memory/notes/00.md").write_text("new", encoding="utf-8")
    apply(left, store, home)
    store.requests.clear()
    with pytest.raises(
        reconcile.WorkspaceReconcileError, match="changed after planning"
    ):
        reconcile.apply_reconcile_plan(plan, home, remote=store)
    assert store.requests == []
    fresh = reconcile.build_reconcile_plan(right, store, _cached=plan.payload_cache)
    assert store.requests == [("memory:notes/00.md",)]
    assert fresh.payload_cache != plan.payload_cache


def test_exact_response_budget_accounts_for_unicode_and_base64_padding():
    payloads = {f'字"\\{index}': FilePayload(b"x" * index) for index in range(1, 7)}
    descriptors = {
        key: ResourceDescriptor(
            "fp", payload_hash(payload), len(encode_payload(payload))
        )
        for key, payload in payloads.items()
    }
    snapshot = RemoteSnapshot("center", 0, descriptors)
    assert fetch_response_size(snapshot, payloads) == len(
        encode_fetch_response(payloads)
    )


def test_cycles_are_atomic_and_dependency_order_is_deterministic():
    graph = {"c": {"b"}, "b": {"a"}, "a": {"b"}, "d": set()}
    assert atomic_groups(graph) == (("a", "b"), ("c",), ("d",))
    assert atomic_groups(dict(reversed(tuple(graph.items())))) == atomic_groups(graph)
    assert (
        len(
            atomic_groups(
                {
                    str(index): {str(index - 1)} if index else set()
                    for index in range(2000)
                }
            )
        )
        == 2000
    )


def test_oversized_cycle_blocks_group_and_defers_dependents(monkeypatch):
    payload = FilePayload(b"x" * 100)
    descriptor = ResourceDescriptor(
        "fp", payload_hash(payload), len(encode_payload(payload))
    )
    snapshot = RemoteSnapshot("center", 0, {})
    items = [
        reconcile.ReconcileItem(key, "CREATE", "remote", None, "fp", "new")
        for key in ("a", "b", "c")
    ]
    mutations = {
        item.id: ResourceMutation(item.id, None, descriptor, payload) for item in items
    }
    size = len(
        encode_commit_request(
            build_commit_request("0" * 32, "0" * 32, snapshot, (mutations["a"],))
        )
    )
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", size)
    result = select_round(
        items,
        {"a": {"b"}, "b": {"a"}, "c": {"b"}},
        snapshot,
        {key: mutation.after for key, mutation in mutations.items()},
        None,
    )
    assert [item.action for item in result] == ["BLOCKED", "BLOCKED", "DEFERRED"]


def test_create_and_delete_dependencies_follow_target_state(bounded):
    left, right, store, home = bounded
    (left / "skills/tool").mkdir()
    (left / "skills/tool/SKILL.md").write_text("# Tool", encoding="utf-8")
    (left / "skills.toml").write_text('skills = ["tool"]\n', encoding="utf-8")
    plan = reconcile.build_reconcile_plan(left, store)
    dependencies = operation_dependencies(plan.items, plan.local_snapshot.resources, {})
    assert dependencies["skill-selection:tool"] == {"skill:tool"}
    apply(left, store, home)
    apply(right, store, home)
    (left / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (left / "skills/tool/SKILL.md").unlink()
    (left / "skills/tool").rmdir()
    plan = reconcile.build_reconcile_plan(left, store)
    remote_resources = snapshot_workspace(right).resources
    dependencies = operation_dependencies(
        plan.items, plan.local_snapshot.resources, remote_resources
    )
    assert dependencies["skill:tool"] == {"skill-selection:tool"}
    apply(left, store, home)
    apply(right, store, home)
    assert not (right / "skills/tool").exists()


def test_partial_result_keeps_unresolved_prerequisites_visible(bounded):
    left, _, store, home = bounded
    (left / "skills/tool").mkdir()
    (left / "skills/tool/SKILL.md").write_text(
        'api_key = "abcdefghijklmnop123456"', encoding="utf-8"
    )
    (left / "skills.toml").write_text('skills = ["tool"]\n', encoding="utf-8")
    result = apply(left, store, home)
    assert not result.blocked and result.stop_reason.startswith(
        "No reconciliation progress"
    )
    assert result.applied_items
    assert (
        next(item for item in result.items if item.id == "skill:tool").action
        == "BLOCKED"
    )
    assert (
        next(item for item in result.items if item.id == "skill-selection:tool").action
        == "DEFERRED"
    )
    state = load_replica_state(left)[0]
    assert "skill-selection:tool" not in state.base


def test_future_manifest_capacity_rejected_before_pairing(bounded, monkeypatch):
    left, _, store, home = bounded
    notes(left, count=20)
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", 2048)
    with pytest.raises(reconcile.WorkspaceReconcileError, match="manifest"):
        apply(left, store, home)
    assert store.read().revision == 0 and load_replica_state(left)[0] is None
    assert store.requests == []


def test_snapshot_competition_during_fetch_stops_with_remaining_work(
    bounded, monkeypatch
):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    calls = []

    def expire(*args):
        calls.append(args)
        raise SnapshotExpired("concurrent revision")

    monkeypatch.setattr(store, "fetch", expire)
    result = apply(right, store, home)
    assert len(calls) == 2 and result.rounds == 0
    assert result.stop_reason == "Snapshot retry limit reached"
    assert result.deferred and not result.applied_items
    assert load_replica_state(right)[0] is None


@pytest.mark.parametrize("headroom", [-1, 0, 1])
def test_download_response_limit_is_exact_before_fetch(bounded, monkeypatch, headroom):
    left, right, store, home = bounded
    notes(left, count=1)
    apply(left, store, home)
    snapshot = store.read()
    identity = "memory:notes/00.md"
    size = fetch_response_size(snapshot, {identity})
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", size + headroom)
    store.requests.clear()
    plan = reconcile.build_reconcile_plan(right, store)
    item = next(item for item in plan.items if item.id == identity)
    assert item.action == ("BLOCKED" if headroom < 0 else "CREATE")
    assert store.requests == ([] if headroom < 0 else [(identity,)])


def test_fetch_request_manifest_and_ids_limit_selection(bounded, monkeypatch):
    left, right, store, home = bounded
    notes(left, count=3)
    apply(left, store, home)
    snapshot = store.read()
    identity = "memory:notes/00.md"
    monkeypatch.setattr(
        remote_limits,
        "MAX_REMOTE_REQUEST_BYTES",
        len(encode_fetch_request(snapshot, (identity,))),
    )
    store.requests.clear()
    plan = reconcile.build_reconcile_plan(right, store)
    assert {item.id for item in plan.changes} == {identity}
    assert {item.id for item in plan.deferred} == {
        "memory:notes/01.md",
        "memory:notes/02.md",
    }
    assert store.requests == [(identity,)]


def test_config_resolution_does_not_fetch_outside_its_budget(bounded, monkeypatch):
    left, right, store, home = bounded
    apply(left, store, home)
    apply(right, store, home)
    (right / "config.toml").write_text(
        '[feature]\nflag = "' + "x" * 2500 + '"\n', encoding="utf-8"
    )
    apply(right, store, home)
    (left / "config.toml").write_text('feature = "scalar"\n', encoding="utf-8")
    snapshot = store.read()
    manifest = len(encode_fetch_request(snapshot, ()))
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_REQUEST_BYTES", manifest + 1000)
    monkeypatch.setattr(remote_limits, "MAX_REMOTE_RESPONSE_BYTES", manifest + 1000)
    store.requests.clear()
    plan = reconcile.build_reconcile_plan(
        left, store, resolutions={"config:feature.flag": "local"}
    )
    assert not plan.blocked
    assert (
        next(item for item in plan.items if item.id == "config:feature.flag").action
        == "BLOCKED"
    )
    assert store.requests == []


def test_unpaired_id_disappearing_on_both_sides_is_confirmed(bounded):
    left, right, store, home = bounded
    notes(left)
    apply(left, store, home)
    plan = reconcile.build_reconcile_plan(right, store)
    reconcile.apply_reconcile_plan(plan, home, remote=store)
    identity = next(item.id for item in plan.deferred if item.id.startswith("memory:"))
    assert identity in load_replica_state(right)[0].unpaired_ids
    path = left / "memory" / identity.partition(":")[2]
    path.unlink()
    apply(left, store, home)
    apply(right, store, home)
    assert identity not in load_replica_state(right)[0].unpaired_ids
    assert not load_replica_state(right)[0].unpaired_ids


def test_upload_planning_releases_each_payload_before_next_capture(
    bounded, monkeypatch
):
    left, _, store, _ = bounded
    notes(left)
    previous = []
    original = reconcile.capture_resources

    def capture(*args, **kwargs):
        assert all(reference() is None for reference in previous)
        payloads = original(*args, **kwargs)
        previous.extend(weakref.ref(payload) for payload in payloads.values())
        return payloads

    monkeypatch.setattr(reconcile, "capture_resources", capture)
    plan = reconcile.build_reconcile_plan(left, store)
    assert all(reference() is None for reference in previous)
    assert not plan.payload_cache
    assert plan.deferred


def test_two_thousand_notes_have_linear_payload_encoding(tmp_path, monkeypatch):
    local = _workspace(tmp_path / "local")
    for index in range(2000):
        (local / f"memory/notes/{index:04}.md").write_bytes(b"x" * 1024)
    calls = []
    original = reconcile.encode_payload

    def encode(payload):
        calls.append(1)
        return original(payload)

    monkeypatch.setattr(reconcile, "encode_payload", encode)
    wire_calls = []
    original_request = remote_protocol.encode_request

    def encode_request(*args, **kwargs):
        wire_calls.append(1)
        return original_request(*args, **kwargs)

    monkeypatch.setattr(remote_protocol, "encode_request", encode_request)
    started = time.perf_counter()
    plan = reconcile.build_reconcile_plan(local, InMemoryRemote())
    elapsed = time.perf_counter() - started
    assert len(plan.changes) >= 2000 and not plan.deferred
    assert len(calls) == len(plan.changes)
    assert len(wire_calls) <= 10
    # Encoding count is the deterministic complexity guard. Leave filesystem
    # headroom for Windows and loaded CI hosts while rejecting the old minutes.
    assert elapsed < (10 if sys.platform == "win32" else 3), elapsed


@pytest.mark.parametrize(
    "payload",
    [
        FilePayload(b"x"),
        TomlPayload.from_value(TomlValue(('字"\\',), "value")),
        TreePayload((TreeEntry("SKILL.md", b"# Tree"),)),
        MemberPayload(),
    ],
)
@pytest.mark.parametrize("operation", ["create", "update", "delete"])
@pytest.mark.parametrize("count", [1, 3])
def test_additive_commit_sizes_match_exact_wire_encoding(payload, operation, count):
    after = ResourceDescriptor(
        "fingerprint",
        payload_hash(payload),
        len(encode_payload(payload)),
        ('opaque"\\字',),
    )
    identities = [f'字"\\资源{index}' for index in range(count)]
    before = after if operation != "create" else None
    cursor = ReceiptCursor('previous"字', "a" * 64)
    snapshot = RemoteSnapshot(
        'center"字', 9, {identity: before for identity in identities} if before else {}
    )
    mutations = tuple(
        ResourceMutation(
            identity,
            before,
            None if operation == "delete" else after,
            None if operation == "delete" else payload,
        )
        for identity in identities
    )
    req = build_commit_request(
        "0" * 32, "0" * 32, snapshot, mutations, previous_receipt=cursor
    )
    predicted = (
        commit_envelope_size(snapshot, cursor)
        + count
        - 1
        + sum(
            mutation_size(mutation.id, mutation.before, mutation.after)
            for mutation in mutations
        )
    )
    assert predicted == len(encode_commit_request(req))
