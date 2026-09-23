"""Shared structured diagnostics for commands and health reports."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FindingAction:
    """One explicit user action that can resolve or bypass a finding."""

    label: str
    command: str


@dataclass(frozen=True)
class Finding:
    """A diagnostic with stable identity and optional actionable context."""

    status: str
    message: str
    fix_hint: str = ""
    code: str = ""
    resource: str = ""
    source: str = ""
    reason: str = ""
    actions: tuple[FindingAction, ...] = ()


def normalized_finding_status(finding: Finding) -> str:
    """Normalize finding status strings to canonical lower-case categories."""
    status = finding.status.strip().upper()
    if status in {"ERROR", "FAIL"}:
        return "error"
    if status in {"WARNING", "WARN"}:
        return "warning"
    if status == "CONFLICT":
        return "conflict"
    return status.lower()


def is_error_finding(finding: Finding) -> bool:
    """True if the finding represents an error or failure condition."""
    return normalized_finding_status(finding) == "error"


def is_warning_finding(finding: Finding) -> bool:
    """True if the finding represents a warning condition."""
    return normalized_finding_status(finding) == "warning"


def is_conflict_finding(finding: Finding) -> bool:
    """True if the finding represents a resource conflict."""
    return normalized_finding_status(finding) == "conflict"
