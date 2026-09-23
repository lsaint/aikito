"""Universal observation protocol, summary metrics, and view models for synchronization plans."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from enum import Enum
from typing import Protocol, runtime_checkable

from .diagnostics import (
    Finding,
    is_conflict_finding,
    is_error_finding,
    is_warning_finding,
)


class OperationEffect(str, Enum):
    """Categorized mutation effect of a single planned operation."""

    CREATE = "create"
    UPDATE = "update"
    REMOVE = "remove"
    STATE_ONLY = "state_only"
    NOOP = "noop"
    SKIP = "skip"
    NONE = "none"


class UnknownPlanActionError(RuntimeError):
    """Raised when an unrecognized domain operation action is encountered."""


@dataclass(frozen=True)
class PlanOperationView:
    """Read-only projection of a single domain operation for cross-resource aggregation."""

    resource_type: str
    resource_name: str
    effect: OperationEffect

    scope: str = ""
    agent: str = ""
    project: str = ""
    target: str = ""
    source: str = ""

    reason: str = ""
    domain_action: str = ""
    authorized: bool = True


@dataclass(frozen=True)
class PlanSummary:
    """Aggregated quantitative summary of planned operations and diagnostics."""

    creates: int = 0
    updates: int = 0
    removes: int = 0
    state_only: int = 0
    unchanged: int = 0
    skipped: int = 0
    conflicts: int = 0
    warnings: int = 0
    errors: int = 0

    @property
    def changes(self) -> int:
        """Count of mutations affecting user-visible resources (state_only is excluded)."""
        return self.creates + self.updates + self.removes

    @property
    def blocked(self) -> bool:
        """True if the plan contains blocking conflict or error diagnostics."""
        return self.conflicts > 0 or self.errors > 0


def summarize_plan(
    operations: Sequence[PlanOperationView],
    findings: Sequence[Finding],
) -> PlanSummary:
    """Compute a deterministic summary from operations and findings."""
    creates = 0
    updates = 0
    removes = 0
    state_only = 0
    unchanged = 0
    skipped = 0

    for op in operations:
        match op.effect:
            case OperationEffect.CREATE:
                creates += 1
            case OperationEffect.UPDATE:
                updates += 1
            case OperationEffect.REMOVE:
                removes += 1
            case OperationEffect.STATE_ONLY:
                state_only += 1
            case OperationEffect.NOOP:
                unchanged += 1
            case OperationEffect.SKIP:
                skipped += 1
            case OperationEffect.NONE:
                pass

    conflicts = sum(1 for f in findings if is_conflict_finding(f))
    warnings = sum(1 for f in findings if is_warning_finding(f))
    errors = sum(1 for f in findings if is_error_finding(f))

    return PlanSummary(
        creates=creates,
        updates=updates,
        removes=removes,
        state_only=state_only,
        unchanged=unchanged,
        skipped=skipped,
        conflicts=conflicts,
        warnings=warnings,
        errors=errors,
    )


@dataclass(frozen=True)
class PlanObservation:
    """Immutable, pure observation projection of any resource or composite plan."""

    operations: tuple[PlanOperationView, ...] = ()
    findings: tuple[Finding, ...] = ()
    can_apply: bool = True

    @property
    def summary(self) -> PlanSummary:
        return summarize_plan(self.operations, self.findings)


@runtime_checkable
class ObservablePlan(Protocol):
    """Structural protocol for any plan capable of projecting a PlanObservation."""

    def observe(self) -> PlanObservation: ...


def combine_observations(
    observations: Iterable[PlanObservation],
    *,
    additional_findings: Iterable[Finding] = (),
    can_apply: bool | None = None,
) -> PlanObservation:
    """Combine multiple child observations into a composite observation without deduplication."""
    obs_list = list(observations)
    combined_ops: list[PlanOperationView] = []
    combined_findings: list[Finding] = []

    for obs in obs_list:
        combined_ops.extend(obs.operations)
        combined_findings.extend(obs.findings)

    combined_findings.extend(additional_findings)

    if can_apply is False:
        effective_can_apply = False
    else:
        effective_can_apply = all(obs.can_apply for obs in obs_list)

    return PlanObservation(
        operations=tuple(combined_ops),
        findings=tuple(combined_findings),
        can_apply=effective_can_apply,
    )
