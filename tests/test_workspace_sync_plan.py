"""Tests for WorkspaceSyncPlan and unified workspace coordinator (INV-APP-01..07)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

import pytest

from aikito.mcp import MCPExecutionResult
from aikito.subagent import SubagentExecutionResult
from aikito.workspace_sync import (
    BundledSkillRefreshError,
    GlobalSyncExecutionResult,
    WorkspaceSyncPlan,
    build_bundled_refresh_plan,
    build_workspace_sync_plan,
    execute_bundled_refresh_plan,
    execute_workspace_sync_plan,
    sync_global_resources,
)


def _setup_minimal_workspace(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True)
    skills_dir = root / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (root / "agents.toml").write_text("[agents]\n", encoding="utf-8")
    (root / "subagents.toml").write_text("[subagents]\n", encoding="utf-8")
    (root / "mcps").mkdir(parents=True, exist_ok=True)
    global_dir = root / "global"
    global_dir.mkdir(parents=True, exist_ok=True)
    (global_dir / "AGENTS.md").write_text("# Global Rules\n", encoding="utf-8")


def test_inv_app_01_read_only_plan_build(tmp_path: Path) -> None:
    """INV-APP-01: build_workspace_sync_plan is strictly read-only."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # Add a project
    proj_dir = ws / "projects" / "myproj"
    proj_dir.mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "myproj_checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (proj_dir / "agent.toml").write_text(
        f'path = "{checkout}"\nskills = []\n',
        encoding="utf-8",
    )

    # Snapshot workspace state before build
    before_files = {p: p.stat().st_mtime_ns for p in ws.rglob("*") if p.is_file()}

    plan = build_workspace_sync_plan(ws, home=home)

    # Verify plan structure
    assert isinstance(plan, WorkspaceSyncPlan)
    assert plan.can_apply is True
    assert len(plan.project_entries) == 1
    assert plan.project_entries[0].project_name == "myproj"
    assert plan.project_entries[0].binding_status == "active"

    # Verify zero mutations
    after_files = {p: p.stat().st_mtime_ns for p in ws.rglob("*") if p.is_file()}
    assert before_files == after_files

    # Verify no lock file or backup directories created
    assert not (ws / ".skill_lock").exists()
    assert not (home / ".local" / "state" / "aikito" / "backups").exists()


def test_inv_app_02_same_plan_execution_fidelity(tmp_path: Path) -> None:
    """INV-APP-02: execute_workspace_sync_plan executes the exact operations from the plan without re-planning."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    plan = build_workspace_sync_plan(ws, home=home)

    mock_exec_global = Mock(return_value=GlobalSyncExecutionResult(success=True))
    mock_exec_sub = Mock(
        return_value=SubagentExecutionResult(
            success=True,
            applied_count=0,
            noop_count=0,
            skipped_count=0,
            conflict_count=0,
            failed_count=0,
        )
    )
    mock_exec_mcp = Mock(
        return_value=MCPExecutionResult(
            success=True,
            applied_count=0,
            noop_count=0,
            skipped_count=0,
            conflict_count=0,
            failed_count=0,
        )
    )
    mock_apply_batch = Mock()

    # Pass the plan to executor
    res = execute_workspace_sync_plan(
        plan,
        ws,
        home,
        execute_global_fn=mock_exec_global,
        execute_subagent_fn=mock_exec_sub,
        execute_mcp_fn=mock_exec_mcp,
        apply_project_batch_fn=mock_apply_batch,
    )

    assert res.success is True
    # Verify exact plan was passed to executor without calling build functions again
    mock_exec_global.assert_called_once_with(plan.global_plan, ws, home, dry_run=False)
    if plan.subagent_plan:
        mock_exec_sub.assert_called_once_with(plan.subagent_plan, home=home)
    if plan.mcp_plan:
        mock_exec_mcp.assert_called_once_with(plan.mcp_plan, home=home)


def test_inv_app_04_partial_failure_segmented_results(tmp_path: Path) -> None:
    """INV-APP-04: Segmented failure accurately records prior committed segments as succeeded."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # Add a project
    proj_dir = ws / "projects" / "p1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "p1_checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (proj_dir / "agent.toml").write_text(
        f'path = "{checkout}"\nskills = []\n', encoding="utf-8"
    )

    plan = build_workspace_sync_plan(ws, home=home)

    # Simulate: Global succeeds, Subagents succeeds, MCP fails
    mock_global_res = GlobalSyncExecutionResult(success=True)
    mock_sub_res = SubagentExecutionResult(
        success=True,
        applied_count=2,
        noop_count=0,
        skipped_count=0,
        conflict_count=0,
        failed_count=0,
    )
    mock_mcp_res = MCPExecutionResult(
        success=False,
        applied_count=0,
        noop_count=0,
        skipped_count=0,
        conflict_count=0,
        failed_count=1,
        error_message="MCP config write locked",
    )
    mock_apply_proj = Mock()

    res = execute_workspace_sync_plan(
        plan,
        ws,
        home,
        execute_global_fn=lambda *a, **k: mock_global_res,
        execute_subagent_fn=lambda *a, **k: mock_sub_res,
        execute_mcp_fn=lambda *a, **k: mock_mcp_res,
        apply_project_batch_fn=mock_apply_proj,
    )

    # Overall execution failed
    assert res.success is False
    assert res.error_message == "MCP config write locked"

    # Prior committed segments MUST be recorded as succeeded
    assert res.global_result is not None
    assert res.global_result.success is True
    assert res.subagent_result is not None
    assert res.subagent_result.success is True

    # Failed segment accurately captured
    assert res.mcp_result is not None
    assert res.mcp_result.success is False

    # Projects were not executed due to MCP failure
    mock_apply_proj.assert_not_called()
    assert len(res.project_results) == 0


def test_inv_app_05_stale_precondition_plan_rejection(tmp_path: Path) -> None:
    """INV-APP-05: Stale precondition plan rejection (no silent re-plan)."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # Construct plan when can_apply is False (e.g. conflict/stale finding)
    plan = build_workspace_sync_plan(ws, home=home)
    object.__setattr__(plan, "can_apply", False)

    # Calling execute on an invalid/stale plan must reject without executing any segment
    mock_global = Mock()
    res = execute_workspace_sync_plan(
        plan,
        ws,
        home,
        execute_global_fn=mock_global,
    )

    assert res.success is False
    assert "cannot apply" in (res.error_message or "").lower()
    mock_global.assert_not_called()


def test_inv_app_06_structured_project_binding_and_offline(tmp_path: Path) -> None:
    """INV-APP-06: Configured projects are represented with structured binding states: active, offline, unbound."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # 1. Active project
    p_act = ws / "projects" / "active_proj"
    p_act.mkdir(parents=True, exist_ok=True)
    c_act = tmp_path / "active_checkout"
    c_act.mkdir(parents=True, exist_ok=True)
    (p_act / "agent.toml").write_text(
        f'path = "{c_act}"\nskills = []\n', encoding="utf-8"
    )

    # 2. Offline project (path does not exist on host)
    p_off = ws / "projects" / "offline_proj"
    p_off.mkdir(parents=True, exist_ok=True)
    c_off = tmp_path / "nonexistent_checkout"
    (p_off / "agent.toml").write_text(
        f'path = "{c_off}"\nskills = []\n', encoding="utf-8"
    )

    # 3. Unbound project (no paths defined)
    p_unb = ws / "projects" / "unbound_proj"
    p_unb.mkdir(parents=True, exist_ok=True)
    (p_unb / "agent.toml").write_text("# empty paths\n", encoding="utf-8")

    plan = build_workspace_sync_plan(ws, home=home)

    entries_by_name = {e.project_name: e for e in plan.project_entries}
    assert len(entries_by_name) == 3

    # Active
    assert entries_by_name["active_proj"].binding_status == "active"
    assert entries_by_name["active_proj"].batch is not None

    # Offline
    assert entries_by_name["offline_proj"].binding_status == "offline"
    assert entries_by_name["offline_proj"].batch is None
    assert str(c_off) in entries_by_name["offline_proj"].offline_paths

    # Unbound
    assert entries_by_name["unbound_proj"].binding_status == "unbound"
    assert entries_by_name["unbound_proj"].batch is None

    # Structured count
    assert plan.offline == 1
    # Offline project is NOT treated as an error or conflict
    assert len(plan.conflicts) == 0
    assert len(plan.errors) == 0


def test_inv_app_07_bundled_refresh_replan_boundary_in_workspace(
    tmp_path: Path,
) -> None:
    """INV-APP-07: Bundled skill refresh halts before project sync and requires replan."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # Add an active project
    proj_dir = ws / "projects" / "p1"
    proj_dir.mkdir(parents=True, exist_ok=True)
    checkout = tmp_path / "p1_checkout"
    checkout.mkdir(parents=True, exist_ok=True)
    (proj_dir / "agent.toml").write_text(
        f'path = "{checkout}"\nskills = []\n', encoding="utf-8"
    )

    plan = build_workspace_sync_plan(ws, home=home)

    # Simulate global sync where bundled skills were refreshed (replan_required=True)
    mock_global_res = GlobalSyncExecutionResult(
        success=True,
        refreshed_bundled=("aikito-memory",),
        replan_required=True,
    )
    mock_apply_proj = Mock()

    res = execute_workspace_sync_plan(
        plan,
        ws,
        home,
        dry_run=False,
        execute_global_fn=lambda *a, **k: mock_global_res,
        apply_project_batch_fn=mock_apply_proj,
    )

    # Replan required must be set, and project execution deferred
    assert res.success is False
    assert res.replan_required is True
    assert "re-run 'aikito sync'" in (res.error_message or "")
    mock_apply_proj.assert_not_called()


def test_bundled_skill_refresh_fingerprint_divergence_fails(tmp_path: Path) -> None:
    """Bundled skill refresh aborts when target fingerprint diverges between plan and apply."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # Put an outdated bundled skill in workspace
    aikito_skill = ws / "skills" / "aikito"
    aikito_skill.mkdir(parents=True, exist_ok=True)
    (aikito_skill / "SKILL.md").write_text("Old content\n", encoding="utf-8")

    plan = build_bundled_refresh_plan(ws, home)
    assert plan.can_apply is True
    assert "aikito" in plan.refreshed_names

    # Diverge target fingerprint before execution
    (aikito_skill / "SKILL.md").write_text("Mutated after plan\n", encoding="utf-8")

    with pytest.raises(
        BundledSkillRefreshError, match="state diverged from plan snapshot"
    ):
        execute_bundled_refresh_plan(plan, ws, home, dry_run=False)


def test_sync_global_resources_application_service_and_bundled_verification(
    tmp_path: Path,
) -> None:
    """sync_global_resources in workspace_sync functions as an application service and enforces fingerprint verification."""
    ws = tmp_path / "workspace"
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    _setup_minimal_workspace(ws)

    # 1. Successful execution through application service without CLI dependencies
    res = sync_global_resources(ws, home, dry_run=False)
    assert isinstance(res, GlobalSyncExecutionResult)
    assert res.success is True

    # 2. Bundled skill refresh divergence is enforced through sync_global_resources
    aikito_skill = ws / "skills" / "aikito"
    aikito_skill.mkdir(parents=True, exist_ok=True)
    (aikito_skill / "SKILL.md").write_text("Old content\n", encoding="utf-8")

    from unittest.mock import patch

    with patch(
        "aikito.workspace_sync.execute_bundled_refresh_plan",
        side_effect=BundledSkillRefreshError("Mock fingerprint divergence"),
    ):
        res_fail = sync_global_resources(ws, home, dry_run=False)
        assert isinstance(res_fail, GlobalSyncExecutionResult)
        assert res_fail.success is False
        assert "Mock fingerprint divergence" in (res_fail.error_message or "")
