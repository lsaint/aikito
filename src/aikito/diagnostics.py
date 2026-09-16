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
