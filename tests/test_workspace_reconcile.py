"""Resource-center reconciliation, per-replica history, and crash recovery."""

from __future__ import annotations

import json
import os
import random
import shutil
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

import pytest

import aikito.workspace.transactions as transactions
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
    apply_reconcile_plan,
    build_reconcile_plan,
    run_reconciliation,
)
from aikito.workspace.remote import (
    FilesystemRemote,
    REMOTE_STATE,
    REPLICA_STATE,
    resource_for_id,
)
from aikito.workspace.resource_write import ResourceContent, ResourceWrite
from aikito.workspace.resources import fingerprint_resource, snapshot_workspace

from aikito.workspace.payload import (
    ResourceDescriptor,
    ResourceMutation,
    PayloadError,
    payload_hash,
)
from aikito.workspace.payload_io import capture_resources
from aikito.workspace.remote_store import SnapshotExpired, InvalidContent


def _commit(remote, expected, content, writes):
    payloads = capture_resources(
        content,
        [w.id for w in writes if w.fingerprint is not None],
        check_credentials=False,
    )
    mutations = tuple(
        ResourceMutation(
            w.id,
            expected.resources.get(w.id),
            ResourceDescriptor(
                w.fingerprint,
                payload_hash(payloads[w.id]),
                content.resources[w.id].references,
            )
            if w.fingerprint is not None
            else None,
            payloads.get(w.id),
        )
        for w in writes
    )
    return remote.commit(expected, mutations)


def _workspace(root: Path) -> Path:
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects",
        "skills",
        "global",
    ):
        (root / directory).mkdir(parents=True, exist_ok=True)
    (root / "layout.toml").write_text("version = 2\n")
    (root / "skills.toml").write_text("skills = []\n")
    return root


def _round(local, remote, home, **kwargs):
    return run_reconciliation(local, remote, home, dry_run=False, **kwargs)


def _pair(tmp_path):
    a = _workspace(tmp_path / "a")
    remote = FilesystemRemote.create(tmp_path / "center")
    home = tmp_path / "home"
    _round(a, remote, home)
    return a, remote, home


def _state(local):
    return json.loads((local / REPLICA_STATE).read_text())


def _write(local, value, name="a"):
    (local / f"memory/notes/{name}.md").write_text(value)


def test_first_pairing_unions_resources_without_workspace_center(tmp_path):
    a, remote, home = _pair(tmp_path)
    _write(a, "a", "from-a")
    _round(a, remote, home)
    b = _workspace(tmp_path / "b")
    _write(b, "b", "from-b")
    before = {p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()}
    preview = run_reconciliation(b, remote, home, dry_run=True)
    assert {item.target for item in preview.changes} == {"local", "remote"}
    assert before == {
        p.relative_to(b): p.read_bytes() for p in b.rglob("*") if p.is_file()
    }
    _round(b, remote, home)
    _round(a, remote, home)
    assert (a / "memory/notes/from-b.md").read_text() == "b"
    assert (b / "memory/notes/from-a.md").read_text() == "a"
    assert not (remote.root / "layout.toml").exists()
    assert _state(a)["sync_id"] == _state(b)["sync_id"]
    assert _state(a)["replica_id"] != _state(b)["replica_id"]
    assert str(tmp_path) not in (a / REPLICA_STATE).read_text()


def test_add_update_delete_and_repeat_across_two_replicas(tmp_path):
    a, remote, home = _pair(tmp_path)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    note = Path("memory/notes/a.md")
    _write(a, "first")
    assert [
        (i.id, i.action, i.target) for i in build_reconcile_plan(a, remote).changes
    ] == [("memory:notes/a.md", "CREATE", "remote")]
    _round(a, remote, home)
    _round(b, remote, home)
    generation, state = remote.read().generation, _state(b)
    _round(b, remote, home)
    assert remote.read().generation == generation and _state(b) == state
    _write(b, "second")
    assert build_reconcile_plan(b, remote).changes[0].action == "UPDATE"
    _round(b, remote, home)
    _round(a, remote, home)
    assert (a / note).read_text() == "second"
    (a / note).unlink()
    assert build_reconcile_plan(a, remote).changes[0].action == "DELETE"
    _round(a, remote, home)
    _round(b, remote, home)
    assert not (remote.root / note).exists() and not (b / note).exists()
    assert "memory:notes/a.md" not in _state(b)["base"]


def test_skill_directory_is_one_resource_including_empty_directories(tmp_path):
    a, remote, home = _pair(tmp_path)
    skill = a / "skills/example"
    (skill / "empty").mkdir(parents=True)
    (skill / "SKILL.md").write_text("# Example\n")
    _round(a, remote, home)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    assert (b / "skills/example/empty").is_dir()
    (b / "skills/example/SKILL.md").write_text("# Updated\n")
    _round(b, remote, home)
    _round(a, remote, home)
    assert (skill / "SKILL.md").read_text() == "# Updated\n"
    shutil.rmtree(skill)
    _round(a, remote, home)
    _round(b, remote, home)
    assert not (b / "skills/example").exists()


@pytest.mark.parametrize("side", ("local", "remote"))
@pytest.mark.parametrize("delete", (False, True))
def test_conflicts_preserve_base_while_other_resources_advance(tmp_path, side, delete):
    a, remote, home = _pair(tmp_path)
    _write(a, "base")
    _round(a, remote, home)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    old_base = _state(a)["base"]["memory:notes/a.md"]
    if delete:
        (a / "memory/notes/a.md").unlink()
    else:
        _write(a, "local")
    _write(b, "remote")
    _round(b, remote, home)
    _write(a, "safe", "safe")
    result = _round(a, remote, home)
    assert [i.id for i in result.conflicts] == ["memory:notes/a.md"]
    assert (remote.root / "memory/notes/safe.md").read_text() == "safe"
    assert _state(a)["base"]["memory:notes/a.md"] == old_base
    assert "memory:notes/safe.md" in _state(a)["base"]
    assert build_reconcile_plan(a, remote).conflicts
    _round(a, remote, home, resolutions={"memory:notes/a.md": side})
    expected = None if delete and side == "local" else side
    for root in (a, remote.root):
        assert (root / "memory/notes/a.md").exists() == (expected is not None)
        if expected:
            assert (root / "memory/notes/a.md").read_text() == expected
    assert not build_reconcile_plan(a, remote).conflicts


def test_matching_concurrent_edits_advance_base_without_center_write(tmp_path):
    a, remote, home = _pair(tmp_path)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    for root in (a, b):
        _write(root, "same")
    _round(b, remote, home)
    generation = remote.read().generation
    assert build_reconcile_plan(a, remote).items[0].action == "NOOP"
    _round(a, remote, home)
    assert remote.read().generation == generation
    assert "memory:notes/a.md" in _state(a)["base"]
    (a / "memory/notes/a.md").unlink()
    _round(a, remote, home)
    assert not (remote.root / "memory/notes/a.md").exists()


def test_paths_do_not_define_replica_or_center_identity(tmp_path):
    a, remote, home = _pair(tmp_path)
    original = _state(a)
    moved, center = tmp_path / "moved-replica", tmp_path / "moved-center"
    shutil.move(a, moved)
    shutil.move(remote.root, center)
    remote = FilesystemRemote(center)
    _write(moved, "moved")
    _round(moved, remote, home)
    assert _state(moved)["replica_id"] == original["replica_id"]
    assert _state(moved)["sync_id"] == original["sync_id"]
    assert (center / "memory/notes/a.md").read_text() == "moved"


def test_project_memory_creates_local_project_without_workspace_center(tmp_path):
    a, remote, home = _pair(tmp_path)
    notes = a / "projects/demo/memory/notes"
    notes.mkdir(parents=True)
    (a / "projects/demo/agent.toml").write_text('name = "demo"\n')
    (notes / "a.md").write_text("project")
    _write(a, "safe", "safe")
    _round(a, remote, home)
    assert not (remote.root / "projects/demo/agent.toml").exists()
    b = _workspace(tmp_path / "b")
    plan = _round(b, remote, home)
    assert not plan.conflicts
    assert (b / "projects/demo/agent.toml").is_file()
    assert (b / "memory/notes/safe.md").read_text() == "safe"
    assert (b / "projects/demo/memory/notes/a.md").read_text() == "project"
    (b / "projects/demo/memory/notes/a.md").write_text("updated")
    _round(b, remote, home)
    _round(a, remote, home)
    assert (notes / "a.md").read_text() == "updated"


@pytest.mark.parametrize("kind", ("project-memory", "skill"))
@pytest.mark.parametrize("side", ("local", "remote"))
def test_conflict_choices_apply_to_all_supported_resource_kinds(tmp_path, kind, side):
    a, remote, home = _pair(tmp_path)
    b = _workspace(tmp_path / "b")
    if kind == "project-memory":
        for local in (a, b):
            (local / "projects/demo/memory/notes").mkdir(parents=True)
            (local / "projects/demo/agent.toml").write_text('name = "demo"\n')
        relative, identity = (
            Path("projects/demo/memory/notes/a.md"),
            "project-memory:demo/notes/a.md",
        )
    else:
        (a / "skills/review").mkdir()
        relative, identity = Path("skills/review/SKILL.md"), "skill:review"
    (a / relative).write_text("base")
    _round(a, remote, home)
    _round(b, remote, home)
    (a / relative).write_text("local")
    (b / relative).write_text("remote")
    _round(b, remote, home)
    assert [item.id for item in _round(a, remote, home).conflicts] == [identity]
    _round(a, remote, home, resolutions={identity: side})
    assert (a / relative).read_text() == side
    assert (remote.root / relative).read_text() == side


def test_skill_deletion_cannot_break_local_selection(tmp_path):
    a, remote, home = _pair(tmp_path)
    (a / "skills/review").mkdir()
    (a / "skills/review/SKILL.md").write_text("# Review")
    _round(a, remote, home)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    (b / "skills.toml").write_text('skills = ["review"]\n')
    shutil.rmtree(a / "skills/review")
    _round(a, remote, home)
    _write(b, "safe", "safe")
    plan = _round(b, remote, home)
    assert {i.id for i in plan.conflicts} == {"skill:review", "skill-selection:review"}
    assert (b / "skills/review/SKILL.md").exists()
    assert "skill:review" in _state(b)["base"]
    assert (remote.root / "memory/notes/safe.md").exists()
    (b / "skills.toml").write_text("skills = []\n")
    _round(b, remote, home)
    assert not (b / "skills/review").exists()


def test_credentials_block_only_upload_and_do_not_advance_base(tmp_path):
    a, remote, home = _pair(tmp_path)
    secret = a / "memory/notes/private.md"
    secret.write_text("api_key = 'abcdefghijklmnopqrstuv'\n")
    _write(a, "safe", "safe")
    plan = _round(a, remote, home)
    assert [i.id for i in plan.items if i.action == "BLOCKED"] == [
        "memory:notes/private.md"
    ]
    assert "abcdefghijklmnopqrstuv" not in repr(plan.items)
    assert not (remote.root / "memory/notes/private.md").exists()
    assert "memory:notes/private.md" not in _state(a)["base"]
    assert (remote.root / "memory/notes/safe.md").exists()
    secret.write_text("no secret")
    _round(a, remote, home)
    assert (remote.root / "memory/notes/private.md").read_text() == "no secret"


def test_conditional_commit_rejects_stale_generation_before_writes(tmp_path):
    a, remote, home = _pair(tmp_path)
    stale = remote.read()
    _write(a, "new")
    _round(a, remote, home)
    snapshot = snapshot_workspace(a)
    resource = snapshot.resources["memory:notes/a.md"]
    write = ResourceWrite(
        Path(resource.parts[0].path), resource.kind, resource.fingerprint, resource.name
    )
    with pytest.raises(SnapshotExpired, match="generation changed"):
        _commit(remote, stale, ResourceContent.from_workspace(snapshot), (write,))
    assert remote.read().generation == 1


def test_competing_center_writers_cannot_commit_same_generation(tmp_path):
    a, remote, _ = _pair(tmp_path)
    b = _workspace(tmp_path / "b")
    expected = remote.read()
    batches = []
    for local, name in ((a, "a"), (b, "b")):
        _write(local, name, name)
        snapshot = snapshot_workspace(local)
        resource = snapshot.resources[f"memory:notes/{name}.md"]
        batches.append(
            (
                ResourceContent.from_workspace(snapshot),
                ResourceWrite(
                    Path(resource.parts[0].path),
                    resource.kind,
                    resource.fingerprint,
                    resource.name,
                ),
            )
        )

    def commit(batch):
        content, write = batch
        try:
            return _commit(
                FilesystemRemote(remote.root), expected, content, (write,)
            ).generation
        except SnapshotExpired as exc:
            assert "generation changed" in str(exc)
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(commit, batches))
    assert results.count(1) == 1 and results.count(None) == 1
    assert remote.read().generation == 1
    assert len(remote.read().resources) == 1


def test_plan_rejects_stale_local_or_center_changes(tmp_path):
    a, remote, home = _pair(tmp_path)
    _write(a, "planned")
    plan = build_reconcile_plan(a, remote)
    _write(a, "later", "b")
    with pytest.raises(WorkspaceReconcileError, match="changed after planning"):
        apply_reconcile_plan(plan, home, remote=remote)
    assert remote.read().generation == 0
    plan = build_reconcile_plan(a, remote)
    b = _workspace(tmp_path / "b")
    _write(b, "other replica", "c")
    _round(b, remote, home)
    with pytest.raises(WorkspaceReconcileError, match="changed after planning"):
        apply_reconcile_plan(plan, home, remote=remote)
    assert not (remote.root / "memory/notes/a.md").exists()


@pytest.mark.parametrize("failure", ("second-resource", "manifest", "replica-state"))
def test_failure_does_not_advance_replica_base(tmp_path, failure):
    a, remote, home = _pair(tmp_path)
    before = _state(a)
    for name in ("a", "b"):
        _write(a, name, name)
    original_replace, original_text = os.replace, transactions.atomic_text
    failed = False

    def fail_resource(src, dst):
        if (
            "stage" in Path(src).parts
            and Path(dst) == remote.root / "memory/notes/b.md"
        ):
            raise OSError("second write failed")
        original_replace(src, dst)

    def fail_state(path, content):
        nonlocal failed
        target = (
            remote.root / REMOTE_STATE if failure == "manifest" else a / REPLICA_STATE
        )
        if Path(path) == target and not failed:
            failed = True
            raise OSError("state write failed")
        original_text(path, content)

    replacement = (
        patch("aikito.workspace.transactions.os.replace", side_effect=fail_resource)
        if failure == "second-resource"
        else patch("aikito.workspace.transactions.atomic_text", side_effect=fail_state)
    )
    with replacement:
        with pytest.raises(
            OSError if failure == "replica-state" else WorkspaceReconcileError
        ):
            _round(a, remote, home)
    assert _state(a) == before
    if failure == "replica-state":
        assert remote.read().generation == 1
        assert (remote.root / "memory/notes/a.md").read_text() == "a"
    else:
        assert remote.read().generation == 0
        assert not (remote.root / "memory/notes/a.md").exists()
        assert not (remote.root / "memory/notes/b.md").exists()
    _round(a, remote, home)
    assert _state(a)["generation"] == remote.read().generation


@pytest.mark.parametrize("target", ("local", "remote"))
def test_interrupted_round_recovers_and_repeats(tmp_path, target):
    a, remote, home = _pair(tmp_path)
    _write(a, "base")
    _round(a, remote, home)
    note = Path("memory/notes/a.md")
    if target == "local":
        b = _workspace(tmp_path / "b")
        _round(b, remote, home)
        _write(b, "changed")
        _round(b, remote, home)
        destination = a / note
    else:
        _write(a, "changed")
        destination = remote.root / note
    original = os.replace

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == destination:
            raise KeyboardInterrupt
        original(src, dst)

    with patch("aikito.workspace.transactions.os.replace", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            _round(a, remote, home)
    assert not destination.exists()
    with pytest.raises(WorkspaceReconcileError, match="Pending"):
        build_reconcile_plan(a, remote)
    with pytest.raises(WorkspaceReconcileError, match="Recovered an interrupted"):
        _round(a, remote, home)
    assert destination.read_text() == "base"
    _round(a, remote, home)
    assert destination.read_text() == "changed"


def test_recovery_preserves_external_change(tmp_path):
    a, remote, home = _pair(tmp_path)
    _write(a, "new")
    original = os.replace
    destination = remote.root / "memory/notes/a.md"

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == destination:
            raise KeyboardInterrupt
        original(src, dst)

    with patch("aikito.workspace.transactions.os.replace", side_effect=interrupt):
        with pytest.raises(KeyboardInterrupt):
            _round(a, remote, home)
    destination.write_text("external")
    with pytest.raises(WorkspaceReconcileError, match="externally changed"):
        _round(a, remote, home)
    assert destination.read_text() == "external"


@pytest.mark.parametrize("secret", (False, True))
def test_center_accepts_layout_independent_content_and_scans_credentials(
    tmp_path, secret
):
    a, remote, home = _pair(tmp_path)
    text = "password = 'abcdefghijklmnopqrstuv'" if secret else "portable"
    _write(a, text)
    resource = snapshot_workspace(a).resources["memory:notes/a.md"]
    blob = tmp_path / "arbitrary-content.bin"
    blob.write_text(text)
    content = ResourceContent({resource.id: resource}, {resource.id: blob})
    write = ResourceWrite(
        Path(resource.parts[0].path), resource.kind, resource.fingerprint, resource.name
    )
    if secret:
        with pytest.raises(InvalidContent, match="credential"):
            _commit(remote, remote.read(), content, (write,))
        assert remote.read().generation == 0
    else:
        _commit(remote, remote.read(), content, (write,))
        b = _workspace(tmp_path / "b")
        _round(b, remote, home)
        assert (b / "memory/notes/a.md").read_text() == "portable"


def test_seeded_random_replay_through_center(tmp_path):
    a, remote, home = _pair(tmp_path)
    b = _workspace(tmp_path / "b")
    _round(b, remote, home)
    randomizer = random.Random(27)
    names = [Path(f"memory/notes/n{index}.md") for index in range(5)]
    for iteration in range(20):
        side = randomizer.choice((a, b))
        note = randomizer.choice(names)
        if randomizer.choice((True, False)):
            (side / note).write_text(f"round {iteration}\n")
        else:
            (side / note).unlink(missing_ok=True)
        _round(side, remote, home)
        _round(b if side == a else a, remote, home)
        for name in names:
            assert (a / name).exists() == (b / name).exists()
            if (a / name).exists():
                assert (a / name).read_bytes() == (b / name).read_bytes()


@pytest.mark.parametrize("case", ("legacy", "version", "identity", "fingerprint"))
def test_invalid_replica_state_is_rejected(tmp_path, case):
    a, remote, _ = _pair(tmp_path)
    state = _state(a)
    if case == "legacy":
        (a / ".local/state/aikito/workspace-reconcile/baseline.json").write_text(
            '{"version": 1}'
        )
    else:
        if case == "version":
            state["version"] = 0
        elif case == "identity":
            state["sync_id"] = "0" * 32
        else:
            state["base"] = {"memory:../../escape.md": "0" * 64}
        (a / REPLICA_STATE).write_text(json.dumps(state))
    with pytest.raises(WorkspaceReconcileError):
        build_reconcile_plan(a, remote)


def test_managed_area_findings_block_entire_round(tmp_path):
    a, remote, home = _pair(tmp_path)
    _write(a, "safe")
    (a / "agents/broken.toml").write_text("invalid = [")
    plan = _round(a, remote, home)
    assert plan.blocked and remote.read().generation == 0
    assert not (remote.root / "memory/notes/a.md").exists()


@pytest.mark.parametrize("failure", ("staging", "tamper", "verification"))
def test_center_rejects_failed_or_changed_staging_before_confirmation(
    tmp_path, failure
):
    a, remote, home = _pair(tmp_path)
    _write(a, "expected")
    original = transactions._fingerprint_private
    reads = 0

    def tamper(path, kind):
        nonlocal reads
        if "stage" in path.parts and path.name == "a.md":
            reads += 1
            if reads == 2:
                path.write_text("tampered")
        return original(path, kind)

    if failure == "staging":
        replacement = patch(
            "aikito.workspace.transactions._copy_resource",
            side_effect=OSError("stage failed"),
        )
    elif failure == "tamper":
        replacement = patch(
            "aikito.workspace.transactions._fingerprint_private", side_effect=tamper
        )
    else:
        replacement = patch(
            "aikito.workspace.remote.verify_resource_snapshot",
            side_effect=transactions.WorkspaceCoreError("verification failed"),
        )
    with replacement:
        with pytest.raises((OSError, WorkspaceReconcileError)):
            _round(a, remote, home)
    assert remote.read().generation == 0
    assert not (remote.root / "memory/notes/a.md").exists()
    assert not (
        remote.root / ".local/state/aikito/workspace-transactions/pending.json"
    ).exists()
    assert _state(a)["base"] == {}


def test_unmanaged_center_content_is_rejected_during_preview(tmp_path):
    a, remote, _ = _pair(tmp_path)
    (remote.root / "memory/notes").mkdir(parents=True)
    (remote.root / "memory/notes/a.md").write_text("unmanaged")
    _write(a, "source")
    with pytest.raises(WorkspaceReconcileError, match="Unmanaged"):
        build_reconcile_plan(a, remote)
    assert (remote.root / "memory/notes/a.md").read_text() == "unmanaged"


@pytest.mark.parametrize(
    "case",
    ("boolean-version", "boolean-generation", "escape", "bundled", "wrong-fingerprint"),
)
def test_invalid_center_manifest_is_rejected(tmp_path, case):
    a, remote, _ = _pair(tmp_path)
    path = remote.root / REMOTE_STATE
    state = json.loads(path.read_text())
    if case == "boolean-version":
        state["version"] = True
    elif case == "boolean-generation":
        state["generation"] = True
    elif case == "escape":
        state["resources"] = {"memory:../../escape.md": "0" * 64}
    elif case == "bundled":
        state["resources"] = {"skill:aikito": "0" * 64}
    else:
        state["resources"] = {"memory:notes/a.md": "0" * 64}
    path.write_text(json.dumps(state))
    with pytest.raises(WorkspaceReconcileError):
        build_reconcile_plan(a, remote)


def test_center_rejects_skill_payload_without_skill_definition(tmp_path):
    _, remote, _ = _pair(tmp_path)
    payload = tmp_path / "invalid-payload"
    payload.mkdir()
    resource = resource_for_id("skill:invalid", fingerprint_resource(payload, "skill"))
    content = ResourceContent({resource.id: resource}, {resource.id: payload})
    write = ResourceWrite(
        Path("skills/invalid"), "skill", resource.fingerprint, "invalid"
    )
    with pytest.raises(PayloadError, match="SKILL.md"):
        _commit(remote, remote.read(), content, (write,))
    assert remote.read().generation == 0
    assert not (remote.root / "skills/invalid").exists()


@pytest.mark.parametrize("existing", ("local", "remote"))
def test_first_pairing_preserves_resources_matching_template(tmp_path, existing):
    a = _workspace(tmp_path / "a")
    remote = FilesystemRemote.create(tmp_path / "center")
    home = tmp_path / "home"
    _write(a, "template")
    resource = snapshot_workspace(a).resources["memory:notes/a.md"]
    if existing == "remote":
        _round(a, remote, home)
        a = _workspace(tmp_path / "b")
    with patch(
        "aikito.workspace.reconcile.template_fingerprints",
        return_value=frozenset({resource.fingerprint}),
    ):
        plan = _round(a, remote, home)
    assert [item.action for item in plan.changes] == ["CREATE"]
    assert (a / "memory/notes/a.md").read_text() == "template"
    assert (remote.root / "memory/notes/a.md").read_text() == "template"
