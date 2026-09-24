"""Golden behavior tests locking in legacy WorkspaceSyncPlan and preview contracts (v1.50).

These tests explicitly assert the exact legacy counting and cross-tier diagnostic propagation
behaviors documented in the Legacy Duplication Inventory (Section 1.14 of the refactor plan).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from unittest.mock import patch

from aikito.agents import Target
from aikito.config_runtime import ConfigOperation, ConfigTarget
from aikito.diagnostics import Finding
from aikito.global_skills import GlobalSkillBatch, GlobalSkillBatchPlan
from aikito.instructions import InstructionBatch, InstructionPlan
from aikito.link import LinkOperation
from aikito.mcp import MCPConfigTarget, MCPOperation, MCPPlan
from aikito.memory_runtime import MemoryBatch, MemoryPlan
from aikito.plan_observation import PlanObservation, safe_observe_plan
from aikito.project_sync import ProjectSyncBatch
from aikito.skill_plan import SkillOperation, SkillPlan, SkillTarget
from aikito.subagent import SubagentPlan
from aikito.workspace import Workspace, WorkspaceFinding, WorkspaceSyncPreview
from aikito.workspace_sync import (
    BundledSkillRefreshOperation,
    BundledSkillRefreshPlan,
    GlobalSyncPlan,
    ProjectSyncEntry,
    WorkspaceSyncPlan,
    execute_workspace_sync_plan,
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
    # Workspace sync planning adds preflight findings as PREFLIGHT_ERROR.
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


def _legacy_plan_sync_simulation(
    ws_path: Path, plan: WorkspaceSyncPlan
) -> WorkspaceSyncPreview:
    operations: list[str] = []
    if plan.global_plan.bundled_refresh_plan:
        for op in plan.global_plan.bundled_refresh_plan.operations:
            if op.action == "REFRESH":
                operations.append(f"Bundled Skill {op.skill_name}: REFRESH")

    if plan.global_plan.skill_plan:
        for op in plan.global_plan.skill_plan.all_operations:
            if op.action in ("CREATE", "UNLINK", "MIGRATE_CONTAINER", "CONFLICT"):
                name = op.resource_name or op.target_path.name
                operations.append(f"Global Skill {op.action}: {name}")

    if plan.global_plan.instruction_plan:
        for op in plan.global_plan.instruction_plan.operations:
            if op.action in ("CREATE", "UNLINK", "CONFLICT"):
                name = op.resource_name or op.target_path.name
                operations.append(f"Global Instructions {op.action}: {name}")

    if plan.subagent_plan:
        for op in plan.subagent_plan.operations:
            if op.is_authorized:
                agent = getattr(getattr(op, "target", None), "agent", "")
                identity = getattr(getattr(op, "target", None), "logical_identity", "")
                operations.append(f"Subagent {op.action}: {agent}/{identity}")

    if plan.mcp_plan:
        for op in getattr(plan.mcp_plan, "operations", ()):
            if getattr(op, "is_authorized", True):
                agent = getattr(getattr(op, "target", None), "agent", "")
                identity = getattr(getattr(op, "target", None), "logical_identity", "")
                operations.append(f"MCP {op.action}: {agent}/{identity}")

    for entry in plan.project_entries:
        if entry.batch:
            b = entry.batch
            if b.active_checkouts:
                checkouts_str = ", ".join(str(c) for c in b.active_checkouts)
                operations.append(f"Project {entry.project_name}: {checkouts_str}")
            else:
                operations.append(f"Project {entry.project_name}: no active checkouts")
        elif entry.binding_status == "offline":
            operations.append(f"Project {entry.project_name}: offline")

    preview_findings = tuple(
        WorkspaceFinding(
            status=f.status,
            code=f.code,
            message=f.message,
            resource=f.resource,
            fix_hint=f.fix_hint,
        )
        for f in plan.findings
    )

    return WorkspaceSyncPreview(
        workspace_path=ws_path,
        changes=plan.changes,
        unchanged=plan.unchanged,
        offline=plan.offline,
        warnings=len(plan.warnings),
        conflicts=len(plan.conflicts),
        errors=len(plan.errors),
        can_apply=plan.can_apply,
        will_mutate=plan.changes > 0,
        findings=preview_findings,
        operations=tuple(operations),
    )


def test_golden_full_preview_equivalence_matrix(tmp_path: Path) -> None:
    """Full preview equivalence matrix test covering §45–46:

    Asserts exact strings, ordering, inclusion/exclusion, and summary metrics
    between legacy simulation and new observation-driven Workspace.plan_sync().
    """
    ws = Workspace(path=tmp_path, home=tmp_path)

    # 1. Bundled Refresh: REFRESH and NOOP
    bundled_plan = BundledSkillRefreshPlan(
        operations=(
            BundledSkillRefreshOperation(skill_name="bundle_core", action="REFRESH"),
            BundledSkillRefreshOperation(skill_name="bundle_noop", action="NOOP"),
        ),
        refreshed_names=("bundle_core",),
    )

    # 2. Global Skill: CREATE, UNLINK, MIGRATE_CONTAINER, CONFLICT, NOOP, SHARED_PATH
    global_target = Target(
        kind="managed_container",
        scope="global",
        path=tmp_path / "skills",
    )
    global_skill_plan = GlobalSkillBatchPlan(
        batch=GlobalSkillBatch(
            workspace_root=tmp_path,
            container=global_target,
            selected_entries=(),
            stale_entries=(),
            consumers=(),
        ),
        container_op=_make_dummy_link_op("NOOP", "container"),
        entry_ops=(
            _make_dummy_link_op(
                "CREATE", "gs_create", target_path=tmp_path / "skills" / "gs_create"
            ),
            _make_dummy_link_op(
                "UNLINK", "gs_unlink", target_path=tmp_path / "skills" / "gs_unlink"
            ),
            _make_dummy_link_op(
                "MIGRATE_CONTAINER",
                "gs_mig",
                target_path=tmp_path / "skills" / "gs_mig",
            ),
            LinkOperation(
                action="CONFLICT",
                rule_id="R4",
                target_path=tmp_path / "skills" / "gs_conf",
                reason="gs conflict reason",
                finding="gs conflict finding",
                resource_name="gs_conf",
                is_authorized=False,
            ),
            _make_dummy_link_op(
                "NOOP", "gs_noop", target_path=tmp_path / "skills" / "gs_noop"
            ),
            _make_dummy_link_op(
                "SHARED_PATH",
                "gs_shared",
                target_path=tmp_path / "skills" / "gs_shared",
            ),
        ),
        consumer_ops=(),
    )

    # 3. Global Instructions: CREATE, UNLINK, CONFLICT, NOOP
    global_inst_plan = InstructionPlan(
        batch=InstructionBatch(
            scope="global",
            canonical_source=tmp_path / "AGENTS.md",
        ),
        operations=(
            _make_dummy_link_op(
                "CREATE", "AGENTS.md", target_path=tmp_path / "AGENTS.md"
            ),
            _make_dummy_link_op(
                "UNLINK", "CLAUDE.md", target_path=tmp_path / "CLAUDE.md"
            ),
            LinkOperation(
                action="CONFLICT",
                rule_id="R_GI_C",
                target_path=tmp_path / "GEMINI.md",
                resource_name="GEMINI.md",
                reason="gi conflict",
                finding="gi conflict finding",
                is_authorized=False,
            ),
            _make_dummy_link_op(
                "NOOP", "CURSOR.md", target_path=tmp_path / "CURSOR.md"
            ),
        ),
    )

    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=bundled_plan,
        skill_plan=global_skill_plan,
        instruction_plan=global_inst_plan,
    )

    # 4. Subagent: CREATE, UPDATE, REMOVE, NOOP, SKIP, ORPHAN, CONFLICT, ERROR
    sub_ops = (
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_create",
                format="toml",
                agent="codex",
            ),
            action="CREATE",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_update",
                format="toml",
                agent="codex",
            ),
            action="UPDATE",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_remove",
                format="toml",
                agent="codex",
            ),
            action="REMOVE",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_noop",
                format="toml",
                agent="codex",
            ),
            action="NOOP",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_skip",
                format="toml",
                agent="codex",
            ),
            action="SKIP",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_orphan",
                format="toml",
                agent="codex",
            ),
            action="ORPHAN",
            is_authorized=True,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_conflict",
                format="toml",
                agent="codex",
            ),
            action="CONFLICT",
            is_authorized=False,
        ),
        ConfigOperation(
            target=ConfigTarget(
                path=tmp_path / "a.toml",
                logical_identity="sub_error",
                format="toml",
                agent="codex",
            ),
            action="ERROR",
            is_authorized=False,
        ),
    )
    subagent_plan = SubagentPlan(operations=sub_ops, file_plans=())

    # 5. MCP: CREATE, UPDATE, REMOVE, NOOP, SKIP, ERROR, CONFLICT
    mcp_ops = (
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_create",
                format="json",
                agent="claude",
            ),
            action="CREATE",
            is_authorized=True,
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_update",
                format="json",
                agent="claude",
            ),
            action="UPDATE",
            is_authorized=True,
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_remove",
                format="json",
                agent="claude",
            ),
            action="REMOVE",
            is_authorized=True,
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_noop",
                format="json",
                agent="claude",
            ),
            action="NOOP",
            is_authorized=True,
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_skip",
                format="json",
                agent="claude",
            ),
            action="SKIP",
            is_authorized=True,
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_error",
                format="json",
                agent="claude",
            ),
            action="ERROR",
            is_authorized=True,
            reason="MCP error",
        ),
        MCPOperation(
            target=MCPConfigTarget(
                path=tmp_path / "m.json",
                logical_identity="mcp_conflict",
                format="json",
                agent="claude",
            ),
            action="CONFLICT",
            is_authorized=False,
            reason="MCP conflict",
        ),
    )
    mcp_plan = MCPPlan(operations=mcp_ops, file_plans=(), state_snapshot_hash="h")

    # 6. Projects: active checkouts, no active checkouts, offline, unbound
    co1 = tmp_path / "co1"
    co2 = tmp_path / "co2"

    skill_target = SkillTarget(
        workspace_root=tmp_path,
        workspace_id="ws",
        project_name="p_active",
        physical_checkout=co1,
        skill_name="pskill",
        target_path=co1 / ".agents" / "skills" / "pskill",
    )
    project_skill_plan = SkillPlan(
        workspace_root=tmp_path,
        project_name="p_active",
        operations=(
            SkillOperation(
                action="CREATE",
                rule_id="R",
                target=skill_target,
                is_authorized=True,
                reason="create",
            ),
            SkillOperation(
                action="UPDATE",
                rule_id="R",
                target=skill_target,
                is_authorized=True,
                reason="update",
            ),
            SkillOperation(
                action="UNLINK",
                rule_id="R",
                target=skill_target,
                is_authorized=True,
                reason="unlink",
            ),
            SkillOperation(
                action="RECONCILE_STATE",
                rule_id="R",
                target=skill_target,
                reason="reconcile",
            ),
            SkillOperation(
                action="CLAIM_STATE",
                rule_id="R",
                target=skill_target,
                reason="claim",
            ),
            SkillOperation(
                action="REACTIVATE_STATE",
                rule_id="R",
                target=skill_target,
                reason="reactivate",
            ),
            SkillOperation(
                action="DEACTIVATE_STATE",
                rule_id="R",
                target=skill_target,
                reason="deactivate",
            ),
        ),
        findings=(),
        authorizations=(),
        can_apply=True,
    )

    project_inst_plan = InstructionPlan(
        batch=InstructionBatch(
            scope="project",
            canonical_source=co1 / "AGENTS.md",
            project_name="p_active",
        ),
        operations=(
            _make_dummy_link_op(
                "CREATE", "p_inst_create", target_path=co1 / "AGENTS.md"
            ),
            _make_dummy_link_op(
                "UNLINK", "p_inst_unlink", target_path=co1 / "CLAUDE.md"
            ),
            _make_dummy_link_op(
                "CONFLICT", "p_inst_conf", target_path=co1 / "GEMINI.md"
            ),
        ),
    )

    project_mem_plan = MemoryPlan(
        batch=MemoryBatch(
            project_name="p_active",
            workspace_root=tmp_path,
            active_checkouts=(co1,),
        ),
        operations=(
            _make_dummy_link_op("CREATE", "p_mem_create", target_path=co1 / "mem.md"),
            _make_dummy_link_op(
                "UNLINK", "p_mem_unlink", target_path=co1 / "mem_old.md"
            ),
            _make_dummy_link_op(
                "CONFLICT", "p_mem_conf", target_path=co1 / "mem_conf.md"
            ),
        ),
    )

    batch_active = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="p_active",
        active_checkouts=(co1, co2),
        offline_checkouts=(),
        skill_plan=project_skill_plan,
        instruction_plan=project_inst_plan,
        memory_plan=project_mem_plan,
        preflight_findings=(),
        can_apply=True,
    )
    entry_active = ProjectSyncEntry(
        project_name="p_active", binding_status="active", batch=batch_active
    )

    batch_empty = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="p_empty",
        active_checkouts=(),
        offline_checkouts=(),
        skill_plan=None,
        preflight_findings=(),
        can_apply=True,
    )
    entry_empty = ProjectSyncEntry(
        project_name="p_empty", binding_status="active", batch=batch_empty
    )
    entry_offline = ProjectSyncEntry(
        project_name="p_offline", binding_status="offline", batch=None
    )
    entry_unbound = ProjectSyncEntry(
        project_name="p_unbound", binding_status="error", batch=None
    )

    # 7. Workspace findings
    ws_finding = Finding(
        status="WARNING",
        code="WS_WARN",
        message="Workspace notice",
        resource="workspace",
    )

    workspace_plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=subagent_plan,
        mcp_plan=mcp_plan,
        project_entries=(entry_active, entry_empty, entry_offline, entry_unbound),
        findings=(ws_finding,),
    )

    # Compute legacy simulated preview and actual preview via Workspace.plan_sync
    legacy_preview = _legacy_plan_sync_simulation(tmp_path, workspace_plan)
    with patch(
        "aikito.workspace.build_workspace_sync_plan", return_value=workspace_plan
    ):
        actual_preview = ws.plan_sync()

    # Assert exact golden string sequence and ordering
    expected_operations = (
        "Bundled Skill bundle_core: REFRESH",
        "Global Skill CREATE: gs_create",
        "Global Skill UNLINK: gs_unlink",
        "Global Skill MIGRATE_CONTAINER: gs_mig",
        "Global Skill CONFLICT: gs_conf",
        "Global Instructions CREATE: AGENTS.md",
        "Global Instructions UNLINK: CLAUDE.md",
        "Global Instructions CONFLICT: GEMINI.md",
        "Subagent CREATE: codex/sub_create",
        "Subagent UPDATE: codex/sub_update",
        "Subagent REMOVE: codex/sub_remove",
        "Subagent NOOP: codex/sub_noop",
        "Subagent SKIP: codex/sub_skip",
        "Subagent ORPHAN: codex/sub_orphan",
        "MCP CREATE: claude/mcp_create",
        "MCP UPDATE: claude/mcp_update",
        "MCP REMOVE: claude/mcp_remove",
        "MCP NOOP: claude/mcp_noop",
        "MCP SKIP: claude/mcp_skip",
        "MCP ERROR: claude/mcp_error",
        f"Project p_active: {co1}, {co2}",
        "Project p_empty: no active checkouts",
        "Project p_offline: offline",
    )
    assert actual_preview.operations == expected_operations
    assert actual_preview.operations == legacy_preview.operations

    # Explicit assertions on excluded items (§45–46):
    ops_text = " ".join(actual_preview.operations)
    assert "gs_noop" not in ops_text
    assert "gs_shared" not in ops_text
    assert "CURSOR.md" not in ops_text
    assert "sub_conflict" not in ops_text
    assert "sub_error" not in ops_text
    assert "mcp_conflict" not in ops_text
    assert "RECONCILE_STATE" not in ops_text
    assert "CLAIM_STATE" not in ops_text
    assert "REACTIVATE_STATE" not in ops_text
    assert "DEACTIVATE_STATE" not in ops_text
    assert "p_unbound" not in ops_text
    assert "p_inst_create" not in ops_text
    assert "p_mem_create" not in ops_text

    # Fixed v1.50 counts independently guard the observation-backed properties.
    assert (
        actual_preview.changes,
        actual_preview.unchanged,
        actual_preview.offline,
        actual_preview.warnings,
        actual_preview.conflicts,
        actual_preview.errors,
        actual_preview.can_apply,
        actual_preview.will_mutate,
    ) == (19, 6, 1, 2, 6, 2, False, True)

    # Full summary metrics assertions
    assert actual_preview.changes == legacy_preview.changes
    assert actual_preview.unchanged == legacy_preview.unchanged
    assert actual_preview.offline == legacy_preview.offline == 1
    assert actual_preview.warnings == legacy_preview.warnings
    assert actual_preview.conflicts == legacy_preview.conflicts
    assert actual_preview.errors == legacy_preview.errors
    assert actual_preview.can_apply == legacy_preview.can_apply
    assert actual_preview.will_mutate == legacy_preview.will_mutate
    assert actual_preview.findings == legacy_preview.findings


def test_fail_closed_unknown_actions_across_all_domains(tmp_path: Path) -> None:
    """Item 1 / §27: Unknown plan action must fail-closed with ERROR finding and can_apply=False."""
    # 1. LinkOperation in GlobalSkillBatchPlan
    unknown_link_op = _make_dummy_link_op("MYSTERY_ACTION", "skill_unknown")
    global_skill_plan = GlobalSkillBatchPlan(
        batch=GlobalSkillBatch(
            workspace_root=tmp_path,
            container=Target(
                kind="managed_container", scope="global", path=tmp_path / "skills"
            ),
            selected_entries=(),
            stale_entries=(),
            consumers=(),
        ),
        container_op=_make_dummy_link_op("NOOP", "container"),
        entry_ops=(unknown_link_op,),
        consumer_ops=(),
    )
    obs_link = global_skill_plan.observe()
    assert obs_link.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_link.findings
    )

    # 2. BundledSkillRefreshPlan
    bundled_plan = BundledSkillRefreshPlan(
        operations=(
            BundledSkillRefreshOperation(skill_name="b1", action="BOGUS_REFRESH"),
        ),
    )
    obs_bundled = bundled_plan.observe()
    assert obs_bundled.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_bundled.findings
    )

    # 3. SkillPlan
    skill_target = SkillTarget(
        workspace_root=tmp_path,
        workspace_id="ws",
        project_name="proj",
        physical_checkout=tmp_path / "co",
        skill_name="s1",
        target_path=tmp_path / "co" / "s1",
    )
    skill_plan = SkillPlan(
        workspace_root=tmp_path,
        project_name="proj",
        operations=(
            SkillOperation(
                action="ALIEN_ACTION",
                rule_id="R",
                target=skill_target,
                reason="alien",
            ),
        ),
        findings=(),
        authorizations=(),
        can_apply=True,
    )
    obs_skill = skill_plan.observe()
    assert obs_skill.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_skill.findings
    )

    # 4. MCPOperation in MCPPlan
    mcp_target = MCPConfigTarget(
        path=tmp_path / "m.json", logical_identity="m1", format="json", agent="codex"
    )
    mcp_plan = MCPPlan(
        operations=(
            MCPOperation(target=mcp_target, action="WEIRD_ACTION", is_authorized=True),
        ),
        file_plans=(),
        state_snapshot_hash="h",
    )
    obs_mcp = mcp_plan.observe()
    assert obs_mcp.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_mcp.findings
    )

    # 5. ConfigOperation in SubagentPlan
    sub_target = ConfigTarget(
        path=tmp_path / "a.toml", logical_identity="a1", format="toml", agent="codex"
    )
    sub_plan = SubagentPlan(
        operations=(
            ConfigOperation(
                target=sub_target, action="STRANGE_ACTION", is_authorized=True
            ),
        ),
        file_plans=(),
    )
    obs_sub = sub_plan.observe()
    assert obs_sub.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_sub.findings
    )


def test_mcp_authorized_conflict_fails_closed(tmp_path: Path) -> None:
    """Item 2 / §23: MCP Authorized CONFLICT is an invalid state that must fail-closed."""
    mcp_target = MCPConfigTarget(
        path=tmp_path / "m.json", logical_identity="srv", format="json", agent="codex"
    )
    mcp_plan = MCPPlan(
        operations=(
            MCPOperation(target=mcp_target, action="CONFLICT", is_authorized=True),
        ),
        file_plans=(),
        state_snapshot_hash="h",
    )
    obs_mcp = mcp_plan.observe()
    assert obs_mcp.can_apply is False
    assert any(
        f.code == "UNKNOWN_PLAN_ACTION" and f.status == "ERROR"
        for f in obs_mcp.findings
    )


def test_composite_observation_preserves_error_when_child_has_same_message_warning(
    tmp_path: Path,
) -> None:
    """Item 3 / §54: Workspace deduplication must use status+message key so child WARNING

    does not suppress top-level ERROR with identical message.
    """
    shared_message = "Common diagnostic message across tiers"
    child_finding = Finding(
        status="WARNING",
        code="CHILD_WARN",
        message=shared_message,
        resource="child",
    )
    parent_finding = Finding(
        status="ERROR",
        code="PARENT_ERR",
        message=shared_message,
        resource="parent",
    )

    batch = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="p1",
        active_checkouts=(),
        offline_checkouts=(),
        skill_plan=None,
        preflight_findings=(shared_message,),
        can_apply=True,
    )
    entry = ProjectSyncEntry(project_name="p1", binding_status="active", batch=batch)

    # Global plan with child finding
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
        findings=(child_finding,),
    )

    workspace_plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(entry,),
        findings=(parent_finding,),
        can_apply=False,
    )

    obs = workspace_plan.observe()
    error_findings = [
        f for f in obs.findings if f.status == "ERROR" and f.message == shared_message
    ]
    warning_findings = [
        f for f in obs.findings if f.status == "WARNING" and f.message == shared_message
    ]

    assert len(error_findings) == 2
    assert len(warning_findings) == 1
    assert obs.summary.errors == 2
    assert obs.summary.warnings == 1


def test_safe_observe_plan_produces_error_finding_on_exception() -> None:
    """Item 3 / §37: safe_observe_plan catches exceptions and returns can_apply=False

    with PLAN_OBSERVATION_ERROR finding instead of swallowing into can_apply=True.
    """

    class ExplodingPlan:
        can_apply = True

        def observe(self) -> PlanObservation:
            raise RuntimeError("Unexpected failure during observation projection")

    obs = safe_observe_plan(ExplodingPlan())
    assert obs.can_apply is False
    assert len(obs.findings) == 1
    assert obs.findings[0].status == "ERROR"
    assert obs.findings[0].code == "PLAN_OBSERVATION_ERROR"
    assert "Unexpected failure during observation projection" in obs.findings[0].message


def test_execution_gate_and_preview_enforce_observation_can_apply(
    tmp_path: Path,
) -> None:
    """Item 1 / P1: Execution gate and public preview must block when observation can_apply=False."""
    from aikito.workspace_sync import execute_workspace_sync_plan

    bundled_plan = BundledSkillRefreshPlan(
        operations=(
            BundledSkillRefreshOperation(
                skill_name="b_unknown", action="UNKNOWN_MYSTERY_ACTION"
            ),
        ),
    )
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=bundled_plan,
        skill_plan=None,
        instruction_plan=None,
    )
    # Even if can_apply was passed True, unknown action in child must fail-closed
    workspace_plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
        can_apply=True,
    )

    # 1. Plan model can_apply is fail-closed
    assert workspace_plan.can_apply is False
    assert workspace_plan.observe().can_apply is False

    # 2. Public preview can_apply is False
    ws = Workspace(path=tmp_path, home=tmp_path)
    with patch(
        "aikito.workspace.build_workspace_sync_plan", return_value=workspace_plan
    ):
        preview = ws.plan_sync()
    assert preview.can_apply is False

    # 3. Execution entrypoint is blocked
    exec_res = execute_workspace_sync_plan(workspace_plan, tmp_path, tmp_path)
    assert exec_res.success is False
    assert "cannot apply" in (exec_res.error_message or "")


def test_composite_observation_preserves_distinct_errors_with_same_message(
    tmp_path: Path,
) -> None:
    """Item 2 / P1: Observation must be lossless; different code/resource ERRORs

    with identical message must not be deleted.
    """
    shared_message = "Database connection refused"
    child_f = Finding(
        status="ERROR",
        code="DB_CONN_ERR",
        message=shared_message,
        resource="postgres_service",
    )
    parent_f = Finding(
        status="ERROR",
        code="WS_CONN_ERR",
        message=shared_message,
        resource="workspace_coordinator",
    )

    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=BundledSkillRefreshPlan(),
        skill_plan=None,
        instruction_plan=None,
        findings=(child_f,),
    )
    workspace_plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
        findings=(parent_f,),
        can_apply=False,
    )

    obs = workspace_plan.observe()
    matching_findings = [f for f in obs.findings if f.message == shared_message]
    assert len(matching_findings) == 2
    assert child_f in obs.findings
    assert parent_f in obs.findings


def test_safe_observe_plan_fails_when_observe_returns_none() -> None:
    """Item 3 / P2: When observe() returns None, safe_observe_plan() must return

    can_apply=False with PLAN_OBSERVATION_ERROR instead of treating as success.
    """

    class NoneReturningPlan:
        can_apply = True

        def observe(self) -> Any:
            return None

    obs = safe_observe_plan(NoneReturningPlan())
    assert obs is not None
    assert obs.can_apply is False
    assert len(obs.findings) == 1
    assert obs.findings[0].status == "ERROR"
    assert obs.findings[0].code == "PLAN_OBSERVATION_ERROR"
    assert (
        "returned invalid non-PlanObservation result: None" in obs.findings[0].message
    )


def test_owned_preflight_error_survives_matching_child_conflict(tmp_path: Path) -> None:
    message = "same diagnostic text"
    instruction_plan = InstructionPlan(
        batch=InstructionBatch(
            scope="project", canonical_source=tmp_path / "AGENTS.md", project_name="p1"
        ),
        operations=(
            LinkOperation(
                action="CONFLICT",
                rule_id="R1",
                target_path=tmp_path / "AGENTS.md",
                finding=message,
                is_authorized=False,
            ),
        ),
    )
    batch = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="p1",
        active_checkouts=(),
        offline_checkouts=(),
        skill_plan=None,
        instruction_plan=instruction_plan,
        preflight_findings=(message,),
        owned_preflight_findings=(message,),
        can_apply=False,
    )

    observation = batch.observe()
    assert observation.summary.conflicts == 1
    assert observation.summary.errors == 1


def test_workspace_owned_error_survives_matching_child_warning(tmp_path: Path) -> None:
    message = "same diagnostic text"
    warning = Finding("WARNING", message, code="UNRELATED_WARNING", resource="other")
    error = Finding("ERROR", message, code="PREFLIGHT_ERROR", resource="project-a")
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=GlobalSyncPlan(
            bundled_refresh_plan=BundledSkillRefreshPlan(),
            skill_plan=None,
            instruction_plan=None,
            findings=(warning,),
        ),
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
        findings=(error,),
        owned_findings=(error,),
    )

    observation = plan.observe()
    assert observation.findings == (warning, error)
    assert observation.summary.warnings == 1
    assert observation.summary.errors == 1


def test_blocked_execution_includes_observation_error_with_legacy_warning(
    tmp_path: Path,
) -> None:
    warning = Finding("WARNING", "Legacy notice", code="PLAN_WARNING")
    plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=GlobalSyncPlan(
            bundled_refresh_plan=BundledSkillRefreshPlan(
                operations=(
                    BundledSkillRefreshOperation(
                        skill_name="bundle", action="UNKNOWN_ACTION"
                    ),
                ),
            ),
            skill_plan=None,
            instruction_plan=None,
        ),
        subagent_plan=None,
        mcp_plan=None,
        project_entries=(),
        findings=(warning,),
        owned_findings=(warning,),
    )

    result = execute_workspace_sync_plan(plan, tmp_path, tmp_path)
    assert result.success is False
    assert result.findings[0] == warning
    assert any(f.code == "UNKNOWN_PLAN_ACTION" for f in result.findings)
