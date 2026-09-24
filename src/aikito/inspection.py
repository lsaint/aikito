"""Resource inspection contract and models for workspace diagnosis and status reporting."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Mapping

from .diagnostics import Finding


class InspectionStatus(str, Enum):
    """Canonical inspection state of a managed workspace resource."""

    OK = "OK"
    MISSING = "MISSING"
    UPDATE = "UPDATE"
    REMOVE = "REMOVE"
    DRIFT = "DRIFT"
    CONFLICT = "CONFLICT"
    SKIP = "SKIP"
    ERROR = "ERROR"
    ORPHAN = "ORPHAN"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ResourceInspectionView:
    """A pure, read-only inspection fact for a single workspace resource."""

    resource_type: str
    resource_name: str
    status: InspectionStatus

    scope: str = ""
    agent: str = ""
    project: str = ""
    target_path: Path | None = None
    source_path: Path | None = None
    reason: str = ""
    finding: Finding | None = None
    details: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WorkspaceResourceInspection:
    """Aggregated, typed inspection facts across all workspace resources."""

    resources: tuple[ResourceInspectionView, ...] = ()
    findings: tuple[Finding, ...] = ()

    def for_agent(self, agent_name: str) -> tuple[ResourceInspectionView, ...]:
        """Return all inspections relevant to a specific target agent."""
        return tuple(r for r in self.resources if r.agent == agent_name)

    def for_resource_type(
        self, resource_type: str
    ) -> tuple[ResourceInspectionView, ...]:
        """Return all inspections for a given resource type."""
        return tuple(r for r in self.resources if r.resource_type == resource_type)

    def for_project(self, project_name: str) -> tuple[ResourceInspectionView, ...]:
        """Return all inspections scoped to a given project."""
        return tuple(r for r in self.resources if r.project == project_name)

    def status_counts(self) -> dict[InspectionStatus, int]:
        """Return a count of resources per inspection status."""
        counts: dict[InspectionStatus, int] = {s: 0 for s in InspectionStatus}
        for r in self.resources:
            counts[r.status] = counts.get(r.status, 0) + 1
        return counts

    @property
    def has_failures(self) -> bool:
        """True if any resource is in CONFLICT or ERROR, or any finding is FAIL/ERROR/CONFLICT."""
        return any(
            r.status in (InspectionStatus.ERROR, InspectionStatus.CONFLICT)
            for r in self.resources
        ) or any(
            f.status.upper() in ("FAIL", "ERROR", "CONFLICT") for f in self.findings
        )
