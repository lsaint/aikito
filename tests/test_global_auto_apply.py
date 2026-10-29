"""Host approval and ownership boundaries for global auto apply."""

import shutil
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from layout_helpers import write_agents
from workspace_reconcile_smoke import _workspace

from aikito.cli_parser import build_parser
from aikito.directory_identity import directory_identity
from aikito.global_approval import approve_global_container, global_approval_error
from aikito.global_auto_apply import (
    AutoGlobalResult,
    GlobalAutoApply,
    reconcile_local_global,
    render_auto_global_result,
)
from aikito.local_state import clean_local_state, inspect_local_state
from aikito.project_auto_apply import AutoProjectResult
from aikito.remote_cli import cmd_remote_sync
from aikito.workspace.auto_sync import AutoSyncScheduler
from aikito.workspace.sync import sync_global_resources


def test_approval_reconcile_and_external_links(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "global").mkdir()
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(workspace, "")
    skill = workspace / "skills" / "example"
    skill.mkdir(parents=True)
    (workspace / "skills.toml").write_text('skills = ["example"]\n')
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    external = tmp_path / "external"
    external.mkdir()
    (container / "twg-x").symlink_to(external, target_is_directory=True)
    assert reconcile_local_global(workspace, home).status == "unapproved"
    assert not (container / "example").exists()
    approve_global_container(workspace, home, container)
    assert reconcile_local_global(workspace, home, dry_run=True).status == "applied"
    assert not (container / "example").exists()
    assert reconcile_local_global(workspace, home).status == "applied"
    assert (container / "example").is_symlink()
    (skill / "SKILL.md").write_text("updated content")
    assert reconcile_local_global(workspace, home).status == "noop"
    (workspace / "skills.toml").write_text("skills = []\n")
    assert reconcile_local_global(workspace, home).status == "applied"
    assert not (container / "example").is_symlink()
    assert (container / "twg-x").resolve() == external


def test_replaced_container_is_unapproved(tmp_path, isolated_windows_state_permissions):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    container.rename(container.with_name("old"))
    container.mkdir()
    assert reconcile_local_global(workspace, home).status == "unapproved"


def test_manual_sync_approves_only_after_success(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "global").mkdir()
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    (workspace / "skills").mkdir()
    (workspace / "skills.toml").write_text("skills = []\n")
    write_agents(workspace, "")
    container = home / ".agents" / "skills"
    assert sync_global_resources(workspace, home, dry_run=True).success
    assert global_approval_error(workspace, home, container)
    assert sync_global_resources(workspace, home).success
    assert global_approval_error(workspace, home, container) is None


def test_manual_sync_explains_approval_identity_failure(
    tmp_path, isolated_windows_state_permissions, monkeypatch, capsys
):
    home = tmp_path / "home"
    home.mkdir()
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(workspace, "")

    def deny(_path):
        raise ValueError("redirected ancestor")

    monkeypatch.setattr("aikito.workspace.sync.directory_identity", deny)
    assert sync_global_resources(workspace, home).success
    assert "cannot be approved: redirected ancestor" in capsys.readouterr().err


def test_conflicting_entry_does_not_block_other_entries(
    tmp_path, isolated_windows_state_permissions
):
    home, workspace = tmp_path / "home", tmp_path / "workspace"
    home.mkdir()
    workspace.mkdir()
    (workspace / "global").mkdir()
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(workspace, "")
    for name in ("alpha", "beta"):
        (workspace / "skills" / name).mkdir(parents=True)
    (workspace / "skills.toml").write_text('skills = ["alpha", "beta"]\n')
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    (container / "alpha").mkdir()
    approve_global_container(workspace, home, container)
    result = reconcile_local_global(workspace, home)
    assert result.status == "blocked"
    assert result.changes == 1
    assert (container / "alpha").is_dir()
    assert (container / "beta").is_symlink()


def test_doctor_removes_replaced_global_approval(
    tmp_path, isolated_windows_state_permissions, monkeypatch
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    assert not inspect_local_state(home)
    container.rename(container.with_name("old"))
    container.mkdir()
    with monkeypatch.context() as pending:
        pending.setattr("aikito.local_state._pending_workspace", lambda _: True)
        assert not any(f.actions for f in inspect_local_state(home))
        assert not clean_local_state(home)
    findings = inspect_local_state(home)
    assert any(f.code == "local-state.stale" and f.actions for f in findings)
    assert clean_local_state(home)
    assert not inspect_local_state(home)


def test_doctor_removes_deleted_temporary_workspace_approval(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    shutil.rmtree(workspace)
    assert any(f.code == "local-state.stale" for f in inspect_local_state(home))
    assert any("global approval" in message for message in clean_local_state(home))


def test_invalid_remote_selection_never_deletes_links(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    write_agents(workspace, "")
    skill = workspace / "skills" / "example"
    skill.mkdir()
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    link = container / "example"
    link.symlink_to(skill, target_is_directory=True)
    approve_global_container(workspace, home, container)
    for config in ('skills = ["../escape"]\n', "other = []\n"):
        (workspace / "skills.toml").write_text(config)
        assert reconcile_local_global(workspace, home).status == "blocked"
        assert link.is_symlink()


def test_runtime_drift_is_read_only_and_does_not_block_skill(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(
        workspace,
        '[agents.claude]\nskills_path = ".claude/skills"\ninstruction_path = ".claude/CLAUDE.md"\n',
    )
    (home / ".claude").mkdir(parents=True)
    (workspace / "skills" / "example").mkdir()
    (workspace / "skills.toml").write_text('skills = ["example"]\n')
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    assert reconcile_local_global(workspace, home).runtime == "drift"
    approve_global_container(workspace, home, container)
    result = reconcile_local_global(workspace, home)
    assert result.status == "applied"
    assert result.runtime == "drift"
    assert (container / "example").is_symlink()
    assert not (home / ".claude" / "skills").exists()
    assert not (home / ".claude" / "CLAUDE.md").exists()


def test_pending_workspace_blocks_writes(
    tmp_path, isolated_windows_state_permissions, monkeypatch
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    monkeypatch.setattr("aikito.global_auto_apply._pending_workspace", lambda _: True)
    assert reconcile_local_global(workspace, home).status == "blocked"


def test_worker_retries_and_deduplicates_unchanged_results(tmp_path, monkeypatch):
    now = [0.0]
    results = [AutoGlobalResult("failed", reason="temporary"), AutoGlobalResult("noop")]
    logs = []
    monkeypatch.setattr("aikito.global_auto_apply.time.monotonic", lambda: now[0])
    monkeypatch.setattr(
        "aikito.global_auto_apply.reconcile_local_global", lambda *_: results[0]
    )
    worker = GlobalAutoApply(tmp_path, tmp_path, logs.append)
    worker.run()
    assert worker._next == 30
    now[0] = 30
    worker.run()
    assert worker._next == 90
    assert len(logs) == 1
    results.pop(0)
    now[0] = 90
    assert worker.run()["status"] == "noop"
    assert len(logs) == 1
    now[0] = 120
    worker.run()
    assert worker._next == 420


def test_worker_exception_records_failed_status(tmp_path):
    def fail(**_kwargs):
        raise RuntimeError("failed to reconcile")

    scheduler = AutoSyncScheduler(tmp_path, None, tmp_path, on_global_reconcile=fail)
    scheduler._apply_local_projects()
    assert scheduler._status["global_apply"]["status"] == "failed"


def test_approval_moves_between_workspaces(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    left, right = _workspace(tmp_path / "left"), _workspace(tmp_path / "right")
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(left, home, container)
    assert global_approval_error(left, home, container) is None
    approve_global_container(right, home, container)
    assert global_approval_error(left, home, container)
    assert global_approval_error(right, home, container) is None
    assert reconcile_local_global(left, home).status == "unapproved"


def test_add_skill_sync_approves_container(
    tmp_path, isolated_windows_state_permissions, monkeypatch
):
    home = tmp_path / "home"
    home.mkdir()
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(workspace, "")
    monkeypatch.setenv("AIKITO_DIR", str(workspace))
    monkeypatch.setattr("pathlib.Path.home", lambda: home)
    args = build_parser().parse_args(["add", "skill", "example", "--global", "--sync"])
    args.func(args)
    assert global_approval_error(workspace, home, home / ".agents" / "skills") is None


def test_preview_and_noop_messages():
    assert "would apply" in render_auto_global_result(
        AutoGlobalResult("applied", 2), dry_run=True
    )
    assert not render_auto_global_result(AutoGlobalResult("noop"))
    assert "[GLOBAL_APPLY_NOTICE]" in render_auto_global_result(
        AutoGlobalResult("unapproved")
    )


def test_system_root_alias_is_allowed_but_redirected_ancestor_is_not(
    tmp_path, monkeypatch
):
    path = tmp_path / "home" / ".agents" / "skills"
    path.mkdir(parents=True)
    system_alias = list(path.parents)[-2]
    monkeypatch.setattr(
        "aikito.directory_identity.is_reparse_point", lambda p: p == system_alias
    )
    assert directory_identity(path)["inode"]
    monkeypatch.setattr(
        "aikito.directory_identity.is_reparse_point", lambda p: p == path.parent
    )
    with pytest.raises(ValueError):
        directory_identity(path)


def test_changed_agent_paths_are_only_inspected(
    tmp_path, isolated_windows_state_permissions
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    (home / ".new-agent").mkdir(parents=True)
    write_agents(
        workspace,
        '[agents.new]\nskills_path = ".new-agent/skills"\ninstruction_path = ".new-agent/RULES.md"\n',
    )
    (workspace / "skills" / "example").mkdir()
    (workspace / "skills.toml").write_text('skills = ["example"]\n')
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    result = reconcile_local_global(workspace, home)
    assert result.status == "applied" and result.runtime == "drift"
    assert not (home / ".new-agent" / "skills").exists()
    assert not (home / ".new-agent" / "RULES.md").exists()


def test_remote_dry_run_previews_without_writes(
    tmp_path, isolated_windows_state_permissions, capsys
):
    home = tmp_path / "home"
    workspace = _workspace(tmp_path / "workspace")
    (workspace / "global" / "AGENTS.md").write_text("instructions")
    write_agents(workspace, "")
    (workspace / "skills" / "example").mkdir()
    (workspace / "skills.toml").write_text('skills = ["example"]\n')
    container = home / ".agents" / "skills"
    container.mkdir(parents=True)
    approve_global_container(workspace, home, container)
    before = {p: p.read_bytes() for p in (home / ".local").rglob("*.approval")}
    plan = SimpleNamespace(
        blocked=False, conflicts=(), blocked_items=(), stop_reason=None, deferred=()
    )
    with (
        patch("pathlib.Path.home", return_value=home),
        patch("aikito.remote_cli._resolve_cli_workspace", return_value=workspace),
        patch("aikito.remote_cli.is_recognized_workspace", return_value=True),
        patch(
            "aikito.remote_cli.get_workspace_remote_binding",
            return_value=SimpleNamespace(
                endpoint="local", auth=SimpleNamespace(env="TOKEN")
            ),
        ),
        patch("aikito.remote_cli.sync_workspace_remote", return_value=plan),
        patch("aikito.remote_cli.render_remote_sync_plan", return_value="preview"),
        patch(
            "aikito.remote_cli.reconcile_local_projects",
            return_value=AutoProjectResult(),
        ),
    ):
        cmd_remote_sync(build_parser().parse_args(["remote", "sync", "--dry-run"]))
    assert "would apply (1 changes planned)" in capsys.readouterr().out
    assert not (container / "example").exists()
    assert before == {p: p.read_bytes() for p in before}
