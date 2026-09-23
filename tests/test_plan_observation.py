"""Unit tests for plan observation protocol, summary models, and helpers."""

from __future__ import annotations

import pytest

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
