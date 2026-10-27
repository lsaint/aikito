"""Local approval, checkout isolation and remote-independent application boundaries."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest
from layout_helpers import write_agents

from aikito.project import collect_project_summaries, evaluate_project_health
from aikito.render import render_project_detail, render_projects_table
from aikito.doctor import check_projects
from aikito.cli_show import print_project_approval_notice
from aikito.cli_parser import build_parser
from aikito.project_approval import checkout_approval_error
from aikito.project_auto_apply import (
    AutoProjectResult,
    CheckoutResult,
    ProjectAutoApply,
    reconcile_local_projects,
)
from aikito.project_sync import (
    apply_project_sync_batch,
    build_project_sync_batch,
    sync_project,
)
from aikito.skill_state import get_skill_state_dir
from aikito.local_state import inspect_local_state, clean_local_state
from aikito.workspace.auto_sync import AutoSyncScheduler, AutoSyncState
from aikito.workspace.resources import snapshot_workspace


@pytest.fixture
def project(tmp_path, isolated_windows_state_permissions):
    root = tmp_path.resolve()
    home, ws, checkout = root / "home", root / "workspace", root / "checkout"
    home.mkdir()
    checkout.mkdir()
    ws.mkdir()
    for directory in ("mcps", "memory", "global"):
        (ws / directory).mkdir()
    (ws / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    write_agents(ws, '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n')
    definition = ws / "projects" / "demo"
    (definition / "memory" / "notes").mkdir(parents=True)
    (definition / "AGENTS.md").write_text("# Project\n", encoding="utf-8")
    skill = ws / "skills" / "example"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("v1", encoding="utf-8")
    config = definition / "agent.toml"
    config.write_text(
        f'path = {json.dumps(str(checkout))}\nsync_mode = "copy"\nskills = ["example"]\n',
        encoding="utf-8",
    )
    return SimpleNamespace(
        home=home,
        ws=ws,
        checkout=checkout,
        config=config,
        source=skill / "SKILL.md",
        target=checkout / ".agents/skills/example/SKILL.md",
    )


def approve(p):
    assert sync_project(p.ws, p.home, "demo")
    assert checkout_approval_error(p.ws, p.home, "demo", p.checkout) is None


def test_copy_update_status_apply_and_idempotence(project):
    p = project
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    summary = collect_project_summaries(p.ws, p.home)[0]
    assert summary.runtime_status == "UPDATE"
    assert summary.is_sync_fixable
    assert evaluate_project_health(summary) == "! update"
    assert summary.fix_action == "aikito sync project demo"
    result = reconcile_local_projects(p.ws, p.home)
    assert result.checkouts[0].status == "applied"
    assert p.target.read_text(encoding="utf-8") == "v2"
    info = p.target.stat()
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "noop"
    assert p.target.stat().st_mtime_ns == info.st_mtime_ns
    assert evaluate_project_health(collect_project_summaries(p.ws, p.home)[0]) == "OK"


def test_unapproved_path_and_dry_run_never_write(project):
    p = project
    config = p.config.read_bytes()
    result = reconcile_local_projects(p.ws, p.home, dry_run=True)
    assert result.checkouts[0].status == "unapproved"
    assert not (p.checkout / ".agents").exists()
    assert not get_skill_state_dir(p.home).exists()
    assert p.config.read_bytes() == config
    assert sync_project(p.ws, p.home, "demo", dry_run=True)
    assert not list(get_skill_state_dir(p.home).glob("*.approval"))


def test_approved_dry_run_previews_without_mutation(project):
    p = project
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    before = {
        path: path.read_bytes() for path in get_skill_state_dir(p.home).glob("*.json")
    }
    assert reconcile_local_projects(p.ws, p.home, dry_run=True).checkouts[0].changes > 0
    assert p.target.read_text(encoding="utf-8") == "v1"
    assert before == {path: path.read_bytes() for path in before}


def test_new_remote_path_does_not_inherit_approval(project, tmp_path):
    p = project
    approve(p)
    stranger = tmp_path / "stranger"
    stranger.mkdir()
    p.config.write_text(
        p.config.read_text()
        .replace("path = ", "paths = [")
        .replace("\nsync_mode", f", {json.dumps(str(stranger))}]\nsync_mode"),
        encoding="utf-8",
    )
    before = p.config.read_bytes()
    p.source.write_text("v2", encoding="utf-8")
    result = reconcile_local_projects(p.ws, p.home)
    assert [item.status for item in result.checkouts] == ["applied", "unapproved"]
    assert not (stranger / ".agents").exists()
    assert p.config.read_bytes() == before


def test_one_drifted_checkout_does_not_block_another(project, tmp_path):
    p = project
    second = tmp_path / "second"
    second.mkdir()
    assert sync_project(p.ws, p.home, "demo", project_path=second)
    approve(p)
    p.target.write_text("user edit", encoding="utf-8")
    p.source.write_text("v2", encoding="utf-8")
    result = reconcile_local_projects(p.ws, p.home)
    assert {item.status for item in result.checkouts} == {"blocked", "applied"}
    assert result.failed
    assert p.target.read_text(encoding="utf-8") == "user edit"
    assert (second / ".agents/skills/example/SKILL.md").read_text(
        encoding="utf-8"
    ) == "v2"


@pytest.mark.parametrize("replacement", ["directory", "symlink"])
def test_replaced_checkout_is_not_approved(project, tmp_path, replacement):
    p = project
    approve(p)
    p.checkout.rename(tmp_path / "old-checkout")
    if replacement == "directory":
        p.checkout.mkdir()
    else:
        target = tmp_path / "redirect"
        target.mkdir()
        p.checkout.symlink_to(target, target_is_directory=True)
    result = reconcile_local_projects(p.ws, p.home)
    assert result.checkouts[0].status == "unapproved"
    assert not (p.checkout / ".agents").exists()


def test_instruction_only_project_keeps_approval(project):
    p = project
    p.config.write_text(
        p.config.read_text().replace('["example"]', "[]"), encoding="utf-8"
    )
    approve(p)
    assert list(get_skill_state_dir(p.home).glob("*.approval"))
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "noop"


@pytest.mark.parametrize("value", ['"manual"', '"typo"', "true", "[]"])
def test_trigger_policy_and_invalid_config(project, value):
    p = project
    approve(p)
    p.config.write_text(
        p.config.read_text() + f"sync_trigger = {value}\n", encoding="utf-8"
    )
    p.source.write_text("v2", encoding="utf-8")
    item = reconcile_local_projects(p.ws, p.home).checkouts[0]
    assert item.status == ("manual" if value == '"manual"' else "blocked")
    assert p.target.read_text(encoding="utf-8") == "v1"
    if value != '"manual"':
        assert (
            collect_project_summaries(p.ws, p.home)[0].runtime_status
            == "INVALID CONFIG"
        )
        assert any(
            "sync_trigger" in finding.message
            for finding in snapshot_workspace(p.ws).findings
        )
    else:
        assert sync_project(p.ws, p.home, "demo")
        assert p.target.read_text(encoding="utf-8") == "v2"


def test_workspace_pending_blocks_application(project):
    p = project
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    with patch("aikito.project_auto_apply._pending_workspace", return_value=True):
        assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "blocked"
    assert p.target.read_text(encoding="utf-8") == "v1"


def test_offline_checkout_is_not_created(project, tmp_path):
    p = project
    approve(p)
    p.checkout.rename(tmp_path / "offline")
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "offline"
    assert not p.checkout.exists()


def test_worker_throttle_workspace_edits_retry_and_log_dedup(project):
    p = project
    approve(p)
    logger = Mock()
    worker = ProjectAutoApply(p.ws, p.home, logger)
    with patch("aikito.project_auto_apply.time.monotonic", return_value=10):
        worker.run()
        first = worker.status
        worker.run()
        assert worker.status is first
        assert logger.call_count == 1
        p.source.write_text("v2", encoding="utf-8")
        worker.run(force=True)
        assert p.target.read_text(encoding="utf-8") == "v2"
    # Checkout-only edits are checked on the documented bounded fallback.
    p.target.write_text("user edit", encoding="utf-8")
    with patch("aikito.project_auto_apply.time.monotonic", return_value=400):
        worker.run()
        assert worker.status["checkouts"][0]["status"] == "blocked"
    calls = logger.call_count
    with patch("aikito.project_auto_apply.time.monotonic", return_value=440):
        worker.run()
        assert logger.call_count == calls


def test_scheduler_local_error_cannot_change_remote_state(project):
    p = project
    scheduler = AutoSyncScheduler(
        p.ws,
        None,
        p.home,
        on_local_reconcile=Mock(side_effect=PermissionError("denied")),
    )
    scheduler.state = AutoSyncState.IDLE
    scheduler._apply_local_projects(force=True)
    assert scheduler.state == AutoSyncState.IDLE
    assert scheduler._status["project_apply"]["last_error"] == "denied"


def test_scheduler_offline_start_repairs_local_update(project):
    p = project
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    worker = ProjectAutoApply(p.ws, p.home, Mock())
    scheduler = AutoSyncScheduler(p.ws, None, p.home, on_local_reconcile=worker.run)
    with patch.object(scheduler, "_step", side_effect=ConnectionError("offline")):
        with pytest.raises(ConnectionError):
            scheduler.run(once=True)
    assert p.target.read_text(encoding="utf-8") == "v2"


def test_scheduler_local_apply_runs_after_failed_remote_step(project):
    p = project
    local = Mock(return_value={})
    scheduler = AutoSyncScheduler(p.ws, None, p.home, on_local_reconcile=local)
    with patch.object(scheduler, "_step", side_effect=ConnectionError("offline")):
        with pytest.raises(ConnectionError):
            scheduler.step()
    local.assert_called_once_with(force=False)


def test_failed_checkout_is_isolated_from_other_projects(project):
    p = project
    approve(p)
    bad = p.ws / "projects" / "bad"
    bad.mkdir()
    (bad / "agent.toml").write_text('sync_trigger = "invalid"\n', encoding="utf-8")
    p.source.write_text("v2", encoding="utf-8")
    result = reconcile_local_projects(p.ws, p.home)
    assert result.failed
    assert p.target.read_text(encoding="utf-8") == "v2"


@pytest.mark.parametrize(
    "status,code",
    [
        ("blocked", 2),
        ("failed", 2),
        ("unbound", 0),
        ("unapproved", 0),
        ("offline", 0),
        ("manual", 0),
    ],
)
def test_remote_sync_project_exit_codes(project, status, code):
    from aikito.cli_parser import build_parser
    from aikito.remote_cli import cmd_remote_sync

    p = project
    plan = SimpleNamespace(
        blocked=False, conflicts=(), blocked_items=(), stop_reason=None, deferred=()
    )
    result = AutoProjectResult((CheckoutResult("demo", str(p.checkout), status),))
    with (
        patch("aikito.remote_cli._resolve_cli_workspace", return_value=p.ws),
        patch("aikito.remote_cli.is_recognized_workspace", return_value=True),
        patch(
            "aikito.remote_cli.get_workspace_remote_binding",
            return_value=SimpleNamespace(
                endpoint="local", auth=SimpleNamespace(env="TOKEN")
            ),
        ),
        patch("aikito.remote_cli.sync_workspace_remote", return_value=plan),
        patch("aikito.remote_cli.render_remote_sync_plan", return_value="ok"),
        patch("aikito.remote_cli.reconcile_local_projects", return_value=result),
    ):
        args = build_parser().parse_args(["remote", "sync", "--apply"])
        if code:
            with pytest.raises(SystemExit) as exc:
                cmd_remote_sync(args)
            assert exc.value.code == code
        else:
            cmd_remote_sync(args)


def test_legacy_copy_state_cannot_authorize_directory_identity(project):
    p = project
    data = {"path": str(p.checkout), "skills": ["example"], "sync_mode": "copy"}
    batch = build_project_sync_batch(p.ws, p.home, "demo", data)
    assert apply_project_sync_batch(batch, data, p.home).is_success
    assert list(get_skill_state_dir(p.home).glob("*.json"))
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "unapproved"
    assert evaluate_project_health(collect_project_summaries(p.ws, p.home)[0]) == "OK"


def test_approval_is_not_reported_as_invalid_copy_state(project):
    p = project
    approve(p)
    assert not inspect_local_state(p.home)


def test_failed_checkout_does_not_stop_other_checkouts(project, tmp_path):
    p = project
    other = tmp_path / "other"
    other.mkdir()
    assert sync_project(p.ws, p.home, "demo", project_path=other)
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    original = build_project_sync_batch

    def build(*args, **kwargs):
        if kwargs["explicit_path"] == p.checkout:
            raise PermissionError("filesystem access denied")
        return original(*args, **kwargs)

    with patch("aikito.project_auto_apply.build_project_sync_batch", side_effect=build):
        result = reconcile_local_projects(p.ws, p.home)
    assert {item.status for item in result.checkouts} == {"failed", "applied"}
    assert p.target.read_text(encoding="utf-8") == "v1"
    assert (other / ".agents/skills/example/SKILL.md").read_text(
        encoding="utf-8"
    ) == "v2"


def test_skill_selection_memory_and_deselection_follow_existing_ownership(project):
    p = project
    approve(p)
    skill = p.ws / "skills/second"
    skill.mkdir()
    (skill / "SKILL.md").write_text("second", encoding="utf-8")
    shared = p.ws / "memory/shared.md"
    shared.write_text("shared", encoding="utf-8")
    p.config.write_text(
        p.config.read_text().replace('["example"]', '["example", "second"]')
        + 'memory = ["shared.md"]\n',
        encoding="utf-8",
    )
    assert not reconcile_local_projects(p.ws, p.home).failed
    assert (p.checkout / ".agents/skills/second/SKILL.md").read_text(
        encoding="utf-8"
    ) == "second"
    assert (p.checkout / ".agents/memory/shared.md").read_text(
        encoding="utf-8"
    ) == "shared"
    p.config.write_text(
        p.config.read_text().replace('["example", "second"]', "[]"), encoding="utf-8"
    )
    assert not reconcile_local_projects(p.ws, p.home).failed
    # Existing Copy semantics deactivate ownership and preserve the directory.
    assert p.target.exists()
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "noop"


def test_link_deselection_removes_only_managed_link(project):
    p = project
    p.config.write_text(
        p.config.read_text().replace('"copy"', '"link"'), encoding="utf-8"
    )
    approve(p)
    assert p.target.parent.is_symlink()
    p.config.write_text(
        p.config.read_text().replace('["example"]', "[]"), encoding="utf-8"
    )
    assert not reconcile_local_projects(p.ws, p.home).failed
    assert not p.target.parent.is_symlink()


def test_manual_and_background_sync_serialize(project):
    p = project
    approve(p)
    p.source.write_text("v2", encoding="utf-8")
    barrier = threading.Barrier(2)

    def manual():
        barrier.wait(timeout=5)
        return sync_project(p.ws, p.home, "demo")

    def automatic():
        barrier.wait(timeout=5)
        return reconcile_local_projects(p.ws, p.home)

    with ThreadPoolExecutor(max_workers=2) as executor:
        manual_job = executor.submit(manual)
        auto_job = executor.submit(automatic)
        assert manual_job.result(timeout=15)
        assert not auto_job.result(timeout=15).failed
    assert p.target.read_text(encoding="utf-8") == "v2"
    assert reconcile_local_projects(p.ws, p.home).checkouts[0].status == "noop"


def test_multiple_copy_projects_remain_noop(project):
    p = project
    second = p.ws / "projects" / "second"
    second.mkdir()
    checkout = p.ws.parent / "second-checkout"
    checkout.mkdir()
    (second / "agent.toml").write_text(
        p.config.read_text().replace(
            json.dumps(str(p.checkout)), json.dumps(str(checkout))
        ),
        encoding="utf-8",
    )
    assert sync_project(p.ws, p.home, "second")
    approve(p)
    targets = (p.target, checkout / ".agents/skills/example/SKILL.md")
    before = [(path.read_bytes(), path.stat().st_mtime_ns) for path in targets]
    result = reconcile_local_projects(p.ws, p.home)
    assert {item.project for item in result.checkouts} == {"demo", "second"}
    assert all(item.status == "noop" for item in result.checkouts)
    assert [(path.read_bytes(), path.stat().st_mtime_ns) for path in targets] == before


@pytest.mark.benchmark
def test_multiple_copy_projects_scan_baseline(project, record_property):
    p = project
    names = ["example"]
    for number in range(4):
        skill = p.ws / "skills" / f"skill-{number}"
        skill.mkdir()
        (skill / "SKILL.md").write_text("content" * 200, encoding="utf-8")
        names.append(skill.name)
    p.config.write_text(
        p.config.read_text().replace('["example"]', json.dumps(names)), encoding="utf-8"
    )
    for number in range(3):
        definition = p.ws / "projects" / f"project-{number}"
        definition.mkdir()
        checkout = p.ws.parent / f"checkout-{number}"
        checkout.mkdir()
        (definition / "agent.toml").write_text(
            p.config.read_text().replace(
                json.dumps(str(p.checkout)), json.dumps(str(checkout))
            ),
            encoding="utf-8",
        )
        assert sync_project(p.ws, p.home, definition.name)
    approve(p)
    start = time.monotonic()
    result = reconcile_local_projects(p.ws, p.home)
    record_property(
        "four_projects_twenty_copy_skills_seconds", time.monotonic() - start
    )
    assert len(result.checkouts) == 4
    assert all(item.status == "noop" for item in result.checkouts)


def test_unregistered_project_cannot_be_fixed_by_sync(project):
    p = project
    p.config.write_text("skills = []\n", encoding="utf-8")
    summary = collect_project_summaries(p.ws, p.home)[0]
    assert summary.runtime_status == "UNBOUND"
    assert not summary.is_sync_fixable
    assert summary.fix_action == ""
    assert summary.fix_hint == ""
    section = check_projects(p.ws, p.home)
    assert all("aikito sync" not in finding.fix_hint for finding in section.findings)


def test_unapproved_runtime_stays_healthy_and_has_one_migration_notice(project, capsys):
    p = project
    data = {"path": str(p.checkout), "skills": ["example"], "sync_mode": "copy"}
    batch = build_project_sync_batch(p.ws, p.home, "demo", data)
    assert apply_project_sync_batch(batch, data, p.home).is_success
    summary = collect_project_summaries(p.ws, p.home)[0]
    assert summary.runtime_status == "OK"
    assert summary.auto_apply_status == "UNAPPROVED"
    assert summary.is_sync_fixable
    assert evaluate_project_health(summary) == "OK"
    detail = render_project_detail(summary, False, False)
    assert "UNAPPROVED" in detail and "Notice:" in detail
    assert "Issue:" not in detail
    table = render_projects_table([summary, summary], False, False)
    assert "! unbound" not in table.lower()
    assert "Run 'aikito sync' once" not in table
    print_project_approval_notice([summary, summary])
    notices = capsys.readouterr().err
    assert notices.count("[NOTICE] Automatic application") == 1
    assert notices.count("Run 'aikito sync' once") == 1
    assert not any(
        f.status in ("WARN", "ERROR", "FAIL")
        for f in check_projects(p.ws, p.home).findings
    )


@pytest.mark.parametrize(
    "status", ["manual", "unapproved", "offline", "blocked", "failed", "noop"]
)
def test_stable_results_back_off_without_rebuilding_batches(project, status):
    p = project
    result = AutoProjectResult((CheckoutResult("demo", str(p.checkout), status),))
    worker = ProjectAutoApply(p.ws, p.home, Mock())
    with (
        patch(
            "aikito.project_auto_apply.reconcile_local_projects", return_value=result
        ) as reconcile,
        patch("aikito.project_auto_apply.time.monotonic", return_value=0),
    ):
        worker.run()
    with (
        patch(
            "aikito.project_auto_apply.reconcile_local_projects", return_value=result
        ) as reconcile,
        patch("aikito.project_auto_apply.time.monotonic", return_value=30),
    ):
        worker.run()
        reconcile.assert_called_once()
    assert worker._next_scan == 330
    with (
        patch(
            "aikito.project_auto_apply.reconcile_local_projects", return_value=result
        ) as reconcile,
        patch("aikito.project_auto_apply.time.monotonic", return_value=60),
    ):
        worker.run()
        reconcile.assert_not_called()
    with (
        patch(
            "aikito.project_auto_apply.reconcile_local_projects", return_value=result
        ) as reconcile,
        patch("aikito.project_auto_apply.time.monotonic", return_value=330),
    ):
        worker.run()
        reconcile.assert_called_once()


def test_periodic_wakeups_do_not_scan_metadata_every_five_seconds(project):
    p = project
    worker = ProjectAutoApply(p.ws, p.home, Mock())
    with (
        patch.object(worker, "_workspace_signature", return_value=()) as signature,
        patch(
            "aikito.project_auto_apply.reconcile_local_projects",
            return_value=AutoProjectResult(),
        ),
    ):
        for tick in (0, 5, 10, 15, 20, 25, 30):
            with patch("aikito.project_auto_apply.time.monotonic", return_value=tick):
                worker.run()
        assert signature.call_count == 2
        with patch("aikito.project_auto_apply.time.monotonic", return_value=31):
            worker.run(force=True)
        assert signature.call_count == 3


def test_signature_ignores_files_deleted_during_scan(project):
    p = project
    worker = ProjectAutoApply(p.ws, p.home, Mock())
    with patch(
        "aikito.project_auto_apply.os.walk",
        return_value=[(str(p.source.parent), [], ["SKILL.md", "vanished.md"])],
    ):
        signature = worker._workspace_signature()
    assert any(entry[0] == str(p.source) for entry in signature)
    assert not any("vanished.md" in entry[0] for entry in signature)


def test_global_memory_edit_bypasses_stable_result_backoff(project):
    p = project
    shared = p.ws / "memory/shared.md"
    shared.write_text("v1", encoding="utf-8")
    p.config.write_text(
        p.config.read_text() + 'memory = ["shared.md"]\n', encoding="utf-8"
    )
    approve(p)
    worker = ProjectAutoApply(p.ws, p.home, Mock())
    with patch("aikito.project_auto_apply.time.monotonic", return_value=0):
        worker.run()
    with patch("aikito.project_auto_apply.time.monotonic", return_value=30):
        worker.run()
    assert worker._next_scan == 330
    shared.write_text("v2 changed", encoding="utf-8")
    with (
        patch("aikito.project_auto_apply.time.monotonic", return_value=31),
        patch(
            "aikito.project_auto_apply.reconcile_local_projects",
            wraps=reconcile_local_projects,
        ) as reconcile,
    ):
        worker.run(force=True)
        reconcile.assert_called_once()
    assert (p.checkout / ".agents/memory/shared.md").read_text(
        encoding="utf-8"
    ) == "v2 changed"


def test_remote_download_probes_projects_immediately(project):
    p = project
    local = Mock(return_value={})
    scheduler = AutoSyncScheduler(p.ws, None, p.home, on_local_reconcile=local)
    with patch.object(
        scheduler, "_step", return_value=SimpleNamespace(applied_items=(object(),))
    ):
        scheduler.step()
    local.assert_called_once_with(force=True)


def test_unchanged_local_status_is_not_rewritten_on_poll(project):
    p = project
    status = {"last_checked": "stable"}
    scheduler = AutoSyncScheduler(
        p.ws, None, p.home, on_local_reconcile=Mock(return_value=status)
    )
    with patch("aikito.workspace.auto_sync.save_worker_status") as save:
        scheduler._apply_local_projects()
        scheduler._apply_local_projects()
    save.assert_called_once()


@pytest.mark.parametrize("obsolete", ["project", "candidate", "identity", "moved"])
def test_doctor_cleans_proven_obsolete_approval(project, tmp_path, obsolete):
    p = project
    approve(p)
    approval = next(get_skill_state_dir(p.home).glob("*.approval"))
    if obsolete == "project":
        p.config.unlink()
    elif obsolete == "candidate":
        p.config.write_text("skills = []\n", encoding="utf-8")
    elif obsolete == "identity":
        p.checkout.rename(tmp_path / "old-checkout")
        p.checkout.mkdir()
    else:
        p.checkout.rename(tmp_path / "moved-checkout")
        p.config.write_text(
            p.config.read_text().replace(
                json.dumps(str(p.checkout)),
                json.dumps(str(tmp_path / "moved-checkout")),
            ),
            encoding="utf-8",
        )
    before = approval.read_bytes()
    finding = next(f for f in inspect_local_state(p.home) if f.source == str(approval))
    assert finding.code == "local-state.stale" and finding.actions
    assert approval.read_bytes() == before
    assert any(str(approval) in message for message in clean_local_state(p.home))
    assert not approval.exists()


def test_doctor_preserves_offline_approval_and_defers_cleanup_for_journal(
    project, tmp_path
):
    p = project
    approve(p)
    approval = next(get_skill_state_dir(p.home).glob("*.approval"))
    p.checkout.rename(tmp_path / "offline")
    finding = next(f for f in inspect_local_state(p.home) if f.source == str(approval))
    assert finding.code == "local-state.unavailable" and not finding.actions
    assert clean_local_state(p.home) == []
    p.config.unlink()
    journal = approval.parent / "transactions/pending/journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}", encoding="utf-8")
    finding = next(f for f in inspect_local_state(p.home) if f.source == str(approval))
    assert finding.code == "local-state.stale" and not finding.actions
    assert clean_local_state(p.home) == []
    assert approval.exists()


def test_doctor_preserves_malformed_and_symlink_approvals(project):
    p = project
    approve(p)
    approval = next(get_skill_state_dir(p.home).glob("*.approval"))
    wrong = approval.with_name("1" * 64 + ".approval")
    wrong.write_bytes(approval.read_bytes())
    alias = approval.with_name("2" * 64 + ".approval")
    alias.symlink_to(approval)
    findings = inspect_local_state(p.home)
    assert len([f for f in findings if f.code == "local-state.invalid"]) == 2
    assert clean_local_state(p.home) == []
    assert wrong.exists() and alias.is_symlink()


@pytest.mark.parametrize(
    "config", ["paths = 42", 'path = {invalid = "value"}', "paths = [true]"]
)
def test_doctor_cannot_infer_removed_checkout_from_invalid_config(project, config):
    p = project
    approve(p)
    approval = next(get_skill_state_dir(p.home).glob("*.approval"))
    p.config.write_text(config, encoding="utf-8")
    finding = next(f for f in inspect_local_state(p.home) if f.source == str(approval))
    assert finding.code == "local-state.invalid" and not finding.actions
    assert clean_local_state(p.home) == []
    assert approval.exists()


@pytest.mark.parametrize(
    "command", [["status"], ["show", "projects"], ["show", "project", "demo"]]
)
def test_approval_notice_shares_bundled_notice_area(
    project, monkeypatch, capsys, command
):
    p = project
    monkeypatch.setenv("AIKITO_DIR", str(p.ws))
    monkeypatch.setattr("pathlib.Path.home", lambda: p.home)
    args = build_parser().parse_args(command)
    args.func(args)
    output = capsys.readouterr()
    assert "Automatic application" not in output.out
    notices = [line for line in output.err.splitlines() if line.startswith("[NOTICE]")]
    assert len(notices) == 2
    assert notices[0].startswith("[NOTICE] Bundled skill snapshot")
    assert notices[1].startswith("[NOTICE] Automatic application")
    assert output.err.count("Run 'aikito sync' once") == 1
