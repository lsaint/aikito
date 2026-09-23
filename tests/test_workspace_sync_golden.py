"""Golden behavior tests locking in legacy WorkspaceSyncPlan and preview contracts (v1.50).

These tests explicitly assert the exact legacy counting and cross-tier diagnostic propagation
behaviors documented in the Legacy Duplication Inventory (Section 1.14 of the refactor plan).
"""

from __future__ import annotations

from pathlib import Path

from aikito.agents import Target
from aikito.config_runtime import ConfigOperation, ConfigTarget
from aikito.diagnostics import Finding
from aikito.global_skills import GlobalSkillBatch, GlobalSkillBatchPlan
from aikito.instructions import InstructionBatch, InstructionPlan
from aikito.link import LinkOperation
from aikito.mcp import MCPConfigTarget, MCPOperation, MCPPlan
from aikito.project_sync import ProjectSyncBatch
from aikito.skill_plan import SkillOperation, SkillPlan, SkillTarget
from aikito.subagent import SubagentPlan
from aikito.workspace_sync import (
    BundledSkillRefreshPlan,
    GlobalSyncPlan,
    ProjectSyncEntry,
    WorkspaceSyncPlan,
)


def _make_dummy_link_op(
    action: str,
    name: str = "item",
    reason: str = "reason",
    finding: str | None = None,
    rule_id: str = "INV-TR-01",
    target_path: Path = Path("/tmp/target"),
) -> LinkOperation:
    return LinkOperation(
        action=action,
        rule_id=rule_id,
        target_path=target_path,
        reason=reason,
        finding=finding,
        resource_name=name,
    )


def test_golden_global_conflict_duplicate_and_error_message(tmp_path: Path) -> None:
    """1 & 2: Global skill conflict with differing finding/reason counts twice in conflicts,

    and GlobalSyncPlan.error_message causes errors +1.
    """
    op = _make_dummy_link_op(
        action="CONFLICT",
        name="skillA",
        reason="reason text",
        finding="distinct finding text",
        rule_id="RULE-1",
    )
    dummy_target = Target(
        kind="managed_container",
        scope="global",
        path=tmp_path / "skills",
    )
    batch = GlobalSkillBatch(
        workspace_root=tmp_path,
        container=dummy_target,
        selected_entries=(),
        stale_entries=(),
        consumers=(),
    )
    skill_plan = GlobalSkillBatchPlan(
        batch=batch,
        container_op=_make_dummy_link_op("NOOP", "container"),
        entry_ops=(op,),
        consumer_ops=(),
    )
    # GlobalSyncPlan duplicates op.reason in findings and sets error_message
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=skill_plan,
        instruction_plan=None,
        findings=(
            Finding(
                status="CONFLICT",
                code="SKILL_CONFLICT",
                message="reason text",
                resource=str(op.target_path),
            ),
        ),
        can_apply=False,
        error_message="Conflicts detected in global plan.",
    )

    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
        findings=global_plan.findings,
    )

    # Legacy conflicts collects skill_plan.conflicts ("distinct finding text") AND
    # plan.findings ("reason text") -> count is 2!
    assert len(plan.conflicts) == 2
    assert "distinct finding text" in plan.conflicts
    assert "reason text" in plan.conflicts

    # Legacy errors includes global_plan.error_message
    assert len(plan.errors) == 1
    assert "Conflicts detected in global plan." in plan.errors


def test_golden_project_instruction_and_memory_conflict_in_both_conflicts_and_errors(
    tmp_path: Path,
) -> None:
    """3: Project instruction/memory conflicts count in conflicts AND in errors via preflight_findings."""
    inst_op = _make_dummy_link_op(
        action="CONFLICT",
        name="AGENTS.md",
        reason="instruction conflict",
        finding="instruction finding",
    )
    inst_plan = InstructionPlan(
        batch=InstructionBatch(
            scope="project",
            canonical_source=tmp_path / "AGENTS.md",
            project_name="proj1",
        ),
        operations=(inst_op,),
    )

    # In legacy build_project_sync_batch, conflict finding is added to extra_findings -> preflight_findings
    batch = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="proj1",
        active_checkouts=(tmp_path / "co",),
        offline_checkouts=(),
        skill_plan=SkillPlan(
            workspace_root=tmp_path,
            project_name="proj1",
            operations=(),
            findings=(),
            authorizations=(),
            can_apply=True,
        ),
        preflight_findings=("instruction finding",),
        can_apply=False,
        instruction_plan=inst_plan,
    )
    entry = ProjectSyncEntry(
        project_name="proj1",
        binding_status="active",
        batch=batch,
    )

    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
    )
    # plan_workspace_sync adds preflight_findings as Finding(code="PREFLIGHT_ERROR")
    findings = (
        Finding(
            status="error",
            code="PREFLIGHT_ERROR",
            message="instruction finding",
            resource="proj1",
        ),
    )
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(entry,),
        findings=findings,
    )

    # Appears in conflicts (via b.instruction_plan.conflicts)
    assert len(plan.conflicts) == 1
    assert "instruction finding" in plan.conflicts

    # Appears in errors (via entry.batch.preflight_findings and plan.findings)
    assert len(plan.errors) == 1
    assert "instruction finding" in plan.errors


def test_golden_mcp_error_counts_in_conflicts_not_errors(tmp_path: Path) -> None:
    """6: MCPOperation with action='ERROR' counts in conflicts, NOT in errors."""
    mcp_target = MCPConfigTarget(
        path=tmp_path / "mcp.json",
        format="json",
        agent="codex",
        logical_identity="server1",
    )
    mcp_op = MCPOperation(
        target=mcp_target,
        action="ERROR",
        reason="Server initialization failed",
        is_authorized=True,
    )
    mcp_plan = MCPPlan(
        operations=(mcp_op,),
        file_plans=(),
        state_snapshot_hash="dummy_hash",
    )

    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
    )
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=mcp_plan,
        project_entries=(),
    )

    # Legacy conflicts includes MCP ERROR
    assert len(plan.conflicts) == 1
    assert "codex/server1: Server initialization failed" in plan.conflicts

    # Legacy errors does NOT check MCP ERROR
    assert len(plan.errors) == 0


def test_golden_shared_path_does_not_inflate_unchanged(tmp_path: Path) -> None:
    """Legacy unchanged uses noop_count only, ignoring same_object_count (SHARED_PATH)."""
    noop_op = _make_dummy_link_op("NOOP", "item1")
    shared_op = _make_dummy_link_op("SHARED_PATH", "item2")

    dummy_target = Target(
        kind="managed_container",
        scope="global",
        path=tmp_path / "skills",
    )
    batch = GlobalSkillBatch(
        workspace_root=tmp_path,
        container=dummy_target,
        selected_entries=(),
        stale_entries=(),
        consumers=(),
    )
    skill_plan = GlobalSkillBatchPlan(
        batch=batch,
        container_op=_make_dummy_link_op("NOOP", "container"),
        entry_ops=(noop_op, shared_op),
        consumer_ops=(),
    )
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=skill_plan,
        instruction_plan=None,
    )
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
    )

    # In legacy logic: container NOOP (1) + entry NOOP (1) = 2.
    # SHARED_PATH is same_object_count and is NOT added to unchanged!
    assert plan.unchanged == 2


def test_golden_state_only_project_skill_does_not_increment_changes(
    tmp_path: Path,
) -> None:
    """Project skill state-only actions do NOT count toward WorkspaceSyncPlan.changes."""
    target = SkillTarget(
        workspace_root=tmp_path,
        workspace_id="ws",
        project_name="proj",
        physical_checkout=tmp_path / "co",
        skill_name="s1",
        target_path=tmp_path / "co" / ".agents" / "skills" / "s1",
    )
    state_ops = (
        SkillOperation(
            action="RECONCILE_STATE",
            rule_id="INV-TR-06",
            target=target,
            reason="reconcile",
        ),
        SkillOperation(
            action="CLAIM_STATE",
            rule_id="INV-TR-07",
            target=target,
            reason="claim",
        ),
        SkillOperation(
            action="REACTIVATE_STATE",
            rule_id="INV-TR-08",
            target=target,
            reason="reactivate",
        ),
        SkillOperation(
            action="DEACTIVATE_STATE",
            rule_id="INV-TR-16",
            target=target,
            reason="deactivate",
        ),
    )
    skill_plan = SkillPlan(
        workspace_root=tmp_path,
        project_name="proj",
        operations=state_ops,
        findings=(),
        authorizations=(),
        can_apply=True,
    )
    batch = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="proj",
        active_checkouts=(tmp_path / "co",),
        offline_checkouts=(),
        skill_plan=skill_plan,
        preflight_findings=(),
        can_apply=True,
    )
    entry = ProjectSyncEntry(
        project_name="proj",
        binding_status="active",
        batch=batch,
    )
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
    )
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(entry,),
    )

    # changes MUST be 0!
    assert plan.changes == 0


def test_golden_subagent_skip_and_orphan(tmp_path: Path) -> None:
    """Subagent SKIP is ignored in counts; ORPHAN counts in warnings."""
    target = ConfigTarget(
        path=tmp_path / "agents.toml",
        logical_identity="sub1",
        format="codex_toml",
        agent="codex",
    )
    skip_op = ConfigOperation(
        target=target,
        action="SKIP",
        reason="Agent not installed",
        is_authorized=True,
    )
    orphan_op = ConfigOperation(
        target=target,
        action="ORPHAN",
        reason="Pruning disabled",
        is_authorized=True,
    )
    sub_plan = SubagentPlan(
        operations=(skip_op, orphan_op),
        file_plans=(),
    )
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
    )
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=sub_plan,
        mcp_plan=None,
        project_entries=(),
    )

    assert plan.changes == 0
    assert plan.unchanged == 0
    assert len(plan.warnings) == 1
    assert "codex/sub1: Pruning disabled" in plan.warnings
