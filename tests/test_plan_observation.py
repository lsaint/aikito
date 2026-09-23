"""Unit tests for plan observation protocol, summary models, and helpers."""

from __future__ import annotations

import pytest
from pathlib import Path

from aikito.diagnostics import (
    Finding,
    is_conflict_finding,
    is_error_finding,
    is_warning_finding,
    normalized_finding_status,
)
from aikito.plan_observation import (
    ObservablePlan,
    OperationEffect,
    PlanObservation,
    PlanOperationView,
    PlanSummary,
    combine_observations,
    summarize_plan,
)


def test_operation_effect_values() -> None:
    expected = {
        "create",
        "update",
        "remove",
        "state_only",
        "noop",
        "skip",
        "none",
    }
    assert {e.value for e in OperationEffect} == expected


def test_plan_operation_view_immutability() -> None:
    view = PlanOperationView(
        resource_type="skill",
        resource_name="test-skill",
        effect=OperationEffect.CREATE,
        scope="project",
        agent="codex",
        project="projA",
        domain_action="CREATE",
        authorized=True,
    )
    assert view.resource_type == "skill"
    assert view.resource_name == "test-skill"
    assert view.effect == OperationEffect.CREATE
    assert view.scope == "project"
    assert view.authorized is True

    with pytest.raises(AttributeError):
        view.effect = OperationEffect.UPDATE  # type: ignore[misc]


def test_plan_summary_state_only_excluded_from_changes() -> None:
    summary = PlanSummary(
        creates=1,
        updates=2,
        removes=3,
        state_only=4,
        unchanged=5,
        skipped=6,
        conflicts=1,
        warnings=2,
        errors=3,
    )
    # changes must be creates + updates + removes (state_only is excluded)
    assert summary.changes == 6
    assert summary.state_only == 4
    assert summary.blocked is True

    unblocked_summary = PlanSummary(conflicts=0, errors=0, warnings=5)
    assert unblocked_summary.blocked is False


def test_summarize_plan_mapping() -> None:
    ops = [
        PlanOperationView(
            resource_type="skill",
            resource_name="s1",
            effect=OperationEffect.CREATE,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s2",
            effect=OperationEffect.UPDATE,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s3",
            effect=OperationEffect.REMOVE,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s4",
            effect=OperationEffect.STATE_ONLY,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s5",
            effect=OperationEffect.NOOP,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s6",
            effect=OperationEffect.SKIP,
        ),
        PlanOperationView(
            resource_type="skill",
            resource_name="s7",
            effect=OperationEffect.NONE,
        ),
    ]
    findings = [
        Finding(status="CONFLICT", message="conflict 1"),
        Finding(status="conflict", message="conflict 2"),
        Finding(status="WARNING", message="warn 1"),
        Finding(status="warn", message="warn 2"),
        Finding(status="ERROR", message="err 1"),
        Finding(status="FAIL", message="err 2"),
        Finding(status="INFO", message="info 1"),
    ]

    summary = summarize_plan(ops, findings)
    assert summary.creates == 1
    assert summary.updates == 1
    assert summary.removes == 1
    assert summary.state_only == 1
    assert summary.unchanged == 1
    assert summary.skipped == 1
    assert summary.conflicts == 2
    assert summary.warnings == 2
    assert summary.errors == 2
    assert summary.changes == 3
    assert summary.blocked is True


def test_plan_observation_computed_summary() -> None:
    obs = PlanObservation(
        operations=(
            PlanOperationView(
                resource_type="mcp",
                resource_name="m1",
                effect=OperationEffect.UPDATE,
            ),
        ),
        findings=(Finding(status="WARN", message="something"),),
        can_apply=True,
    )
    assert obs.summary.updates == 1
    assert obs.summary.warnings == 1
    assert obs.summary.changes == 1
    assert obs.can_apply is True


def test_combine_observations_stable_ordering_and_no_dedup() -> None:
    f1 = Finding(status="CONFLICT", message="duplicate conflict")
    f2 = Finding(status="CONFLICT", message="duplicate conflict")
    f_extra = Finding(status="ERROR", message="extra error")

    op1 = PlanOperationView(
        resource_type="skill",
        resource_name="a",
        effect=OperationEffect.CREATE,
    )
    op2 = PlanOperationView(
        resource_type="skill",
        resource_name="b",
        effect=OperationEffect.REMOVE,
    )

    obs1 = PlanObservation(operations=(op1,), findings=(f1,), can_apply=True)
    obs2 = PlanOperationView(
        resource_type="skill",
        resource_name="b",
        effect=OperationEffect.REMOVE,
    )
    obs2 = PlanObservation(operations=(op2,), findings=(f2,), can_apply=True)

    combined = combine_observations(
        [obs1, obs2],
        additional_findings=(f_extra,),
    )

    assert combined.operations == (op1, op2)
    # Findings must NOT be deduplicated in observation layer
    assert combined.findings == (f1, f2, f_extra)
    assert combined.can_apply is True
    assert combined.summary.conflicts == 2
    assert combined.summary.errors == 1


def test_combine_observations_can_apply_propagation() -> None:
    obs_ok = PlanObservation(can_apply=True)
    obs_blocked = PlanObservation(can_apply=False)

    # Child blocked -> combined blocked
    res1 = combine_observations([obs_ok, obs_blocked])
    assert res1.can_apply is False

    # All children ok -> combined ok
    res2 = combine_observations([obs_ok, obs_ok])
    assert res2.can_apply is True

    # Parent explicitly False -> combined blocked
    res3 = combine_observations([obs_ok, obs_ok], can_apply=False)
    assert res3.can_apply is False

    # Parent True but child False -> combined blocked (parent cannot override child failure)
    res4 = combine_observations([obs_ok, obs_blocked], can_apply=True)
    assert res4.can_apply is False


def test_diagnostics_normalization_helpers() -> None:
    assert normalized_finding_status(Finding("ERROR", "m")) == "error"
    assert normalized_finding_status(Finding("fail", "m")) == "error"
    assert normalized_finding_status(Finding("WARNING", "m")) == "warning"
    assert normalized_finding_status(Finding("warn", "m")) == "warning"
    assert normalized_finding_status(Finding("CONFLICT", "m")) == "conflict"
    assert normalized_finding_status(Finding("unknown_status", "m")) == "unknown_status"

    assert is_error_finding(Finding("ERROR", "m")) is True
    assert is_error_finding(Finding("FAIL", "m")) is True
    assert is_error_finding(Finding("WARNING", "m")) is False

    assert is_warning_finding(Finding("WARNING", "m")) is True
    assert is_warning_finding(Finding("warn", "m")) is True
    assert is_warning_finding(Finding("ERROR", "m")) is False

    assert is_conflict_finding(Finding("CONFLICT", "m")) is True
    assert is_conflict_finding(Finding("conflict", "m")) is True
    assert is_conflict_finding(Finding("ERROR", "m")) is False


def test_observable_plan_protocol() -> None:
    class DummyPlan:
        def observe(self) -> PlanObservation:
            return PlanObservation()

    class NonObservable:
        pass

    assert isinstance(DummyPlan(), ObservablePlan)
    assert not isinstance(NonObservable(), ObservablePlan)


def test_link_operation_effect_mappings() -> None:
    
    from aikito.link import LinkOperation, link_operation_effect
    from aikito.plan_observation import UnknownPlanActionError

    target = Path("/tmp/t")

    def _op(action: str) -> LinkOperation:
        return LinkOperation(action=action, rule_id="R1", target_path=target)

    assert link_operation_effect(_op("CREATE")) == OperationEffect.CREATE
    assert link_operation_effect(_op("UNLINK")) == OperationEffect.REMOVE
    assert link_operation_effect(_op("MIGRATE_CONTAINER")) == OperationEffect.UPDATE
    assert link_operation_effect(_op("NOOP")) == OperationEffect.NOOP
    assert link_operation_effect(_op("SHARED_PATH")) == OperationEffect.SKIP
    assert link_operation_effect(_op("SKIP")) == OperationEffect.SKIP
    assert link_operation_effect(_op("CONFLICT")) == OperationEffect.NONE

    with pytest.raises(UnknownPlanActionError):
        link_operation_effect(_op("MYSTERY_ACTION"))


def test_link_operation_finding() -> None:
    
    from aikito.link import LinkOperation, link_operation_finding

    target = Path("/tmp/t")
    op_conflict = LinkOperation(
        action="CONFLICT",
        rule_id="RULE-42",
        target_path=target,
        finding="custom finding",
    )
    finding = link_operation_finding(op_conflict)
    assert finding is not None
    assert finding.status == "CONFLICT"
    assert finding.code == "RULE-42"
    assert finding.message == "custom finding"
    assert finding.resource == str(target)

    op_create = LinkOperation(action="CREATE", rule_id="R", target_path=target)
    assert link_operation_finding(op_create) is None


def test_observe_link_operation_fallback_on_unknown() -> None:
    
    from aikito.link import LinkOperation, observe_link_operation

    target = Path("/tmp/t")
    op_bad = LinkOperation(action="BAD_ACTION", rule_id="R", target_path=target)
    view, finding = observe_link_operation(op_bad, resource_type="global_skill")

    assert view.effect == OperationEffect.NONE
    assert view.domain_action == "BAD_ACTION"
    assert finding is not None
    assert finding.status == "ERROR"
    assert finding.code == "UNKNOWN_PLAN_ACTION"


def test_global_skill_batch_plan_observe(tmp_path: Path) -> None:
    from aikito.agents import Target
    from aikito.global_skills import GlobalSkillBatch, GlobalSkillBatchPlan
    from aikito.link import LinkOperation

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
    container_op = LinkOperation(
        action="NOOP",
        rule_id="C1",
        target_path=tmp_path / "skills",
    )
    entry_op = LinkOperation(
        action="CREATE",
        rule_id="E1",
        target_path=tmp_path / "skills" / "my-skill",
        resource_name="my-skill",
    )
    plan = GlobalSkillBatchPlan(
        batch=batch,
        container_op=container_op,
        entry_ops=(entry_op,),
        consumer_ops=(),
    )

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 2
    assert obs.operations[0].effect == OperationEffect.NOOP
    assert obs.operations[1].effect == OperationEffect.CREATE
    assert obs.operations[1].resource_name == "my-skill"
    assert obs.operations[1].resource_type == "global_skill"
    assert obs.summary.creates == 1
    assert obs.summary.unchanged == 1
    assert obs.can_apply is True


def test_instruction_plan_observe(tmp_path: Path) -> None:
    from aikito.instructions import InstructionBatch, InstructionPlan
    from aikito.link import LinkOperation

    batch = InstructionBatch(
        scope="project",
        canonical_source=tmp_path / "AGENTS.md",
        project_name="projA",
    )
    inst_op = LinkOperation(
        action="CONFLICT",
        rule_id="INST-1",
        target_path=tmp_path / "target.md",
        reason="Target diverged",
        resource_name="AGENTS.md",
    )
    plan = InstructionPlan(batch=batch, operations=(inst_op,))

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 1
    assert obs.operations[0].effect == OperationEffect.NONE
    assert obs.operations[0].resource_type == "instruction"
    assert obs.operations[0].project == "projA"
    assert len(obs.findings) == 1
    assert obs.findings[0].code == "INST-1"
    assert obs.findings[0].status == "CONFLICT"
    assert obs.summary.conflicts == 1
    assert obs.can_apply is False


def test_memory_plan_observe(tmp_path: Path) -> None:
    from aikito.link import LinkOperation
    from aikito.memory_runtime import MemoryBatch, MemoryPlan

    batch = MemoryBatch(
        workspace_root=tmp_path,
        project_name="projB",
        active_checkouts=(),
    )
    mem_op = LinkOperation(
        action="UNLINK",
        rule_id="MEM-1",
        target_path=tmp_path / "mem_symlink",
        resource_name="memory-ref",
    )
    plan = MemoryPlan(batch=batch, operations=(mem_op,))

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 1
    assert obs.operations[0].effect == OperationEffect.REMOVE
    assert obs.operations[0].resource_type == "memory"
    assert obs.operations[0].project == "projB"
    assert obs.summary.removes == 1
    assert obs.summary.changes == 1
    assert obs.can_apply is True


def test_bundled_skill_refresh_plan_observe() -> None:
    from aikito.workspace_sync import (
        BundledSkillRefreshOperation,
        BundledSkillRefreshPlan,
    )

    op_refresh = BundledSkillRefreshOperation(
        skill_name="skill1",
        action="REFRESH",
        reason="Diverged",
    )
    op_noop = BundledSkillRefreshOperation(
        skill_name="skill2",
        action="NOOP",
        reason="Up to date",
    )
    plan = BundledSkillRefreshPlan(
        operations=(op_refresh, op_noop),
        refreshed_names=("skill1",),
        can_apply=True,
    )

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 2
    assert obs.operations[0].effect == OperationEffect.UPDATE
    assert obs.operations[0].domain_action == "REFRESH"
    assert obs.operations[0].resource_type == "bundled_skill"
    assert obs.operations[1].effect == OperationEffect.NOOP
    assert obs.summary.updates == 1
    assert obs.summary.unchanged == 1
    assert obs.summary.changes == 1
    assert obs.can_apply is True


def test_skill_plan_observe(tmp_path: Path) -> None:
    from aikito.skill_plan import SkillOperation, SkillPlan, SkillTarget

    target = SkillTarget(
        workspace_root=tmp_path,
        workspace_id="ws",
        project_name="projA",
        physical_checkout=tmp_path / "co",
        skill_name="s1",
        target_path=tmp_path / "co" / "s1",
    )

    ops = (
        SkillOperation(
            action="CREATE",
            rule_id="TR-1",
            target=target,
            reason="create",
            is_authorized=True,
        ),
        SkillOperation(
            action="UPDATE",
            rule_id="TR-2",
            target=target,
            reason="update",
            is_authorized=True,
        ),
        SkillOperation(
            action="UNLINK",
            rule_id="TR-3",
            target=target,
            reason="unlink",
            is_authorized=True,
        ),
        SkillOperation(
            action="NOOP",
            rule_id="TR-4",
            target=target,
            reason="noop",
            is_authorized=True,
        ),
        SkillOperation(
            action="RECONCILE_STATE",
            rule_id="TR-5",
            target=target,
            reason="reconcile",
            is_authorized=True,
        ),
        SkillOperation(
            action="CLAIM_STATE",
            rule_id="TR-6",
            target=target,
            reason="claim",
            is_authorized=True,
        ),
        SkillOperation(
            action="REACTIVATE_STATE",
            rule_id="TR-7",
            target=target,
            reason="reactivate",
            is_authorized=True,
        ),
        SkillOperation(
            action="DEACTIVATE_STATE",
            rule_id="TR-8",
            target=target,
            reason="deactivate",
            is_authorized=True,
        ),
        SkillOperation(
            action="CONFLICT",
            rule_id="TR-9",
            target=target,
            reason="conflict",
            is_authorized=False,
        ),
    )

    plan = SkillPlan(
        workspace_root=tmp_path,
        project_name="projA",
        operations=ops,
        findings=(),
        authorizations=(),
        can_apply=False,
    )

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 9
    assert obs.summary.creates == 1
    assert obs.summary.updates == 1
    assert obs.summary.removes == 1
    assert obs.summary.state_only == 4
    assert obs.summary.unchanged == 1
    assert obs.summary.conflicts == 1
    # changes excludes state_only
    assert obs.summary.changes == 3
    assert obs.can_apply is False


def test_mcp_plan_observe(tmp_path: Path) -> None:
    from aikito.mcp import MCPConfigTarget, MCPOperation, MCPPlan

    def _mcp_op(action: str, name: str, auth: bool = True) -> MCPOperation:
        target = MCPConfigTarget(
            path=tmp_path / "mcp.json",
            format="json",
            agent="codex",
            logical_identity=name,
        )
        return MCPOperation(
            target=target,
            action=action,
            reason=f"reason_{name}",
            is_authorized=auth,
        )

    ops = (
        _mcp_op("CREATE", "m1"),
        _mcp_op("UPDATE", "m2"),
        _mcp_op("REMOVE", "m3"),
        _mcp_op("NOOP", "m4"),
        _mcp_op("SKIP", "m5"),
        _mcp_op("CONFLICT", "m6", auth=False),
        _mcp_op("ERROR", "m7"),
        _mcp_op("CREATE", "m8_unauth", auth=False),
    )

    plan = MCPPlan(
        operations=ops,
        file_plans=(),
        state_snapshot_hash="hash",
    )

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 8
    assert obs.summary.creates == 1  # m8_unauth does not count as create
    assert obs.summary.updates == 1
    assert obs.summary.removes == 1
    assert obs.summary.unchanged == 1
    assert obs.summary.skipped == 1
    assert obs.summary.conflicts == 2  # m6 + m8_unauth
    assert obs.summary.errors == 1  # m7
    assert obs.summary.changes == 3
    assert obs.can_apply is False


def test_subagent_plan_observe(tmp_path: Path) -> None:
    from aikito.config_runtime import ConfigOperation, ConfigTarget
    from aikito.subagent import SubagentPlan

    def _sub_op(action: str, name: str, auth: bool = True) -> ConfigOperation:
        target = ConfigTarget(
            path=tmp_path / "subagents.toml",
            format="codex_toml",
            agent="codex",
            logical_identity=name,
        )
        return ConfigOperation(
            target=target,
            action=action,
            reason=f"reason_{name}",
            is_authorized=auth,
        )

    ops = (
        _sub_op("CREATE", "s1"),
        _sub_op("UPDATE", "s2"),
        _sub_op("REMOVE", "s3"),
        _sub_op("NOOP", "s4"),
        _sub_op("SKIP", "s5"),
        _sub_op("ORPHAN", "s6"),
        _sub_op("CONFLICT", "s7", auth=False),
        _sub_op("ERROR", "s8", auth=False),
    )

    plan = SubagentPlan(operations=ops, file_plans=())

    obs = plan.observe()
    assert isinstance(obs, PlanObservation)
    assert len(obs.operations) == 8
    assert obs.summary.creates == 1
    assert obs.summary.updates == 1
    assert obs.summary.removes == 1
    assert obs.summary.unchanged == 1
    assert obs.summary.skipped == 2  # SKIP (s5) + ORPHAN (s6)
    assert obs.summary.warnings == 1  # ORPHAN (s6)
    assert obs.summary.conflicts == 1  # CONFLICT (s7)
    assert obs.summary.errors == 1  # ERROR (s8)
    assert obs.summary.changes == 3
    assert obs.can_apply is False


