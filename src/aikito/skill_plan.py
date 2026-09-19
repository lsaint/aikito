"""Pure skill planning, state transitions, granular authorization, and CAS verification.

This module is completely free of filesystem side-effects and subprocess calls.
It evaluates observed filesystem/state facts against desired configuration to produce
a deterministic, stably-sorted SkillPlan governed by INV-TR-01..20 and INV-AUTH-01..05.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .skill_state import SkillStateRecord


@dataclass(frozen=True)
class SkillTarget:
    """Target identifier and directory entry path for one project skill."""

    workspace_root: Path
    workspace_id: str
    project_name: str
    physical_checkout: Path
    skill_name: str
    target_path: Path


@dataclass(frozen=True)
class ObservedSkill:
    """Observed runtime directory entry facts and state snapshots for a skill."""

    target: SkillTarget
    entry_type: str  # "missing", "dir", "symlink", "unsupported"
    raw_link_target: Path | None = None
    resolved_link_target: Path | None = None
    link_points_to_canonical: bool = False
    link_points_within_canonical: bool = False
    canonical_valid: bool = True
    canonical_error: str | None = None
    runtime_fingerprint: str | None = None  # R
    canonical_fingerprint: str | None = None  # C
    state_record: SkillStateRecord | None = None
    state_error: str | None = None
    target_lstat: Any = None
    state_generation: int = 0  # Generation of the state doc at plan time


@dataclass(frozen=True)
class DesiredSkill:
    """Desired configuration state for one skill."""

    skill_name: str
    mode: str  # "link", "copy", or "absent"
    canonical_path: Path
    canonical_fingerprint: str | None = None


@dataclass(frozen=True)
class CandidatePathCAS:
    """Compare-And-Swap plan for registering an explicit candidate path in agent.toml."""

    config_path: Path
    pre_image_bytes: bytes
    pre_image_hash: str
    post_image_bytes: bytes
    post_image_hash: str
    is_noop: bool = False


@dataclass(frozen=True)
class SkillOperation:
    """A single atomic operation planned for one skill target."""

    action: str  # "CREATE", "UPDATE", "UNLINK", "NOOP", "CONFLICT", "RECONCILE_STATE", "DEACTIVATE_STATE", "CLAIM_STATE", "REACTIVATE_STATE"
    rule_id: str  # e.g. "INV-TR-01" .. "INV-TR-20"
    target: SkillTarget
    reason: str
    finding: str | None = None
    requires_force: bool = False
    force_type: str | None = (
        None  # "overwrite_drift", "overwrite_unmanaged", "claim_state", "reactivate_state"
    )
    is_authorized: bool = True
    expected_representation: str = "missing"  # "missing", "link", "copy", "unsupported"
    desired_representation: str = "link"  # "link", "copy", "absent"
    expected_fingerprint: str | None = None
    desired_fingerprint: str | None = None
    expected_generation: int | None = None
    next_state_lifecycle: str | None = None  # "active", "inactive", or None
    next_baseline_origin: str | None = (
        None  # "write", "reconcile", "claim", "reactivate"
    )


@dataclass(frozen=True)
class SkillPlan:
    """Immutable, fully-evaluated synchronization plan for project skills."""

    workspace_root: Path
    project_name: str
    operations: tuple[SkillOperation, ...]
    findings: tuple[str, ...]
    authorizations: tuple[str, ...]
    can_apply: bool
    config_cas: CandidatePathCAS | None = None

    @property
    def has_conflicts(self) -> bool:
        return any(
            op.action == "CONFLICT" or not op.is_authorized for op in self.operations
        )


def plan_single_skill(
    target: SkillTarget,
    desired: DesiredSkill,
    observed: ObservedSkill,
    *,
    force: bool = False,
    is_offline: bool = False,
) -> SkillOperation:
    """Evaluate observed evidence and return the exact state transition operation."""
    if is_offline:
        return SkillOperation(
            action="NOOP",
            rule_id="INV-AUTH-02",
            target=target,
            reason=f"Skill '{target.skill_name}' on offline checkout is not synchronized",
            is_authorized=True,
            expected_representation=observed.entry_type,
            desired_representation=desired.mode,
        )

    exp_gen = observed.state_generation if observed.state_generation is not None else 0

    # 1. Selected skills: Link or Copy
    if desired.mode in ("link", "copy"):
        # Check canonical validity first
        if not observed.canonical_valid:
            if observed.entry_type == "symlink" and observed.link_points_to_canonical:
                return SkillOperation(
                    action="CONFLICT",
                    rule_id="INV-TR-04",
                    target=target,
                    reason=f"Broken symlink pointing to missing canonical source for skill '{target.skill_name}'",
                    finding=f"Broken symlink pointing to missing canonical skill: {target.target_path}",
                    is_authorized=False,
                    expected_representation="symlink",
                    desired_representation=desired.mode,
                )
            err = (
                observed.canonical_error
                or f"Canonical skill '{target.skill_name}' is missing or unreadable"
            )
            return SkillOperation(
                action="CONFLICT",
                rule_id="INV-TR-02",
                target=target,
                reason=f"Canonical skill source missing or unreadable: {err}",
                finding=f"Canonical skill source missing or unreadable: {err}",
                is_authorized=False,
                expected_representation=observed.entry_type,
                desired_representation=desired.mode,
            )

        # Mode: Link
        if desired.mode == "link":
            if observed.entry_type == "missing":
                return SkillOperation(
                    action="CREATE",
                    rule_id="INV-TR-01",
                    target=target,
                    reason=f"Create symbolic link for skill '{target.skill_name}'",
                    expected_representation="missing",
                    desired_representation="link",
                    expected_generation=exp_gen,
                    is_authorized=True,
                )
            if observed.entry_type == "symlink":
                if observed.link_points_to_canonical:
                    return SkillOperation(
                        action="NOOP",
                        rule_id="INV-TR-03",
                        target=target,
                        reason=f"Symbolic link for skill '{target.skill_name}' already points to canonical resource",
                        expected_representation="link",
                        desired_representation="link",
                        is_authorized=True,
                    )
                rule = "INV-TR-06" if observed.state_record else "INV-TR-05"
                dest = (
                    observed.raw_link_target
                    or observed.resolved_link_target
                    or "unknown"
                )
                return SkillOperation(
                    action="CONFLICT",
                    rule_id=rule,
                    target=target,
                    reason=f"Symbolic link points to unauthorized destination: {dest}",
                    finding=f"Symbolic link points to unauthorized destination: {target.target_path} -> {dest}",
                    expected_representation="symlink",
                    desired_representation="link",
                    is_authorized=False,
                )
            if observed.entry_type == "dir":
                state = observed.state_record
                if (
                    state
                    and state.lifecycle == "active"
                    and observed.runtime_fingerprint == state.baseline_fingerprint
                ):
                    return SkillOperation(
                        action="UPDATE",
                        rule_id="INV-TR-20",
                        target=target,
                        reason=f"Switch mode from copy to link for unchanged active skill '{target.skill_name}'",
                        expected_representation="copy",
                        desired_representation="link",
                        expected_fingerprint=observed.runtime_fingerprint,
                        expected_generation=exp_gen,
                        next_state_lifecycle="inactive",
                        is_authorized=True,
                    )
                return SkillOperation(
                    action="CONFLICT",
                    rule_id="INV-TR-20",
                    target=target,
                    reason=f"Cannot switch copy to link for skill '{target.skill_name}': directory is drifted, unmanaged, or inactive",
                    finding=f"Cannot switch copy to link for skill '{target.skill_name}': {target.target_path} is not an unchanged active copy",
                    expected_representation="copy",
                    desired_representation="link",
                    is_authorized=False,
                )
            return SkillOperation(
                action="CONFLICT",
                rule_id="INV-TR-13",
                target=target,
                reason=f"Unsupported target filesystem entry for skill '{target.skill_name}'",
                finding=f"Unsupported target filesystem entry: {target.target_path}",
                expected_representation="unsupported",
                desired_representation="link",
                is_authorized=False,
            )

        # Mode: Copy
        if observed.state_error:
            return SkillOperation(
                action="CONFLICT",
                rule_id="INV-TR-13",
                target=target,
                reason=f"State record is corrupt or unreadable: {observed.state_error}",
                finding=f"Corrupt state record for skill '{target.skill_name}': {observed.state_error}",
                expected_representation=observed.entry_type,
                desired_representation="copy",
                is_authorized=False,
            )
        if observed.entry_type == "missing":
            return SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason=f"Create copy of skill '{target.skill_name}'",
                expected_representation="missing",
                desired_representation="copy",
                desired_fingerprint=observed.canonical_fingerprint,
                expected_generation=exp_gen,
                next_state_lifecycle="active",
                next_baseline_origin="write",
                is_authorized=True,
            )
        if observed.entry_type == "symlink":
            if observed.link_points_to_canonical:
                return SkillOperation(
                    action="UPDATE",
                    rule_id="INV-TR-20",
                    target=target,
                    reason=f"Switch mode from link to copy for skill '{target.skill_name}'",
                    expected_representation="link",
                    desired_representation="copy",
                    desired_fingerprint=observed.canonical_fingerprint,
                    expected_generation=exp_gen,
                    next_state_lifecycle="active",
                    next_baseline_origin="write",
                    is_authorized=True,
                )
            return SkillOperation(
                action="CONFLICT",
                rule_id="INV-TR-20",
                target=target,
                reason="Cannot switch link to copy: symlink points to unauthorized destination",
                finding=f"Cannot switch link to copy: {target.target_path} points elsewhere",
                expected_representation="symlink",
                desired_representation="copy",
                is_authorized=False,
            )
        if observed.entry_type == "dir":
            r_fp = observed.runtime_fingerprint
            c_fp = observed.canonical_fingerprint
            state = observed.state_record
            # Capture current state document generation for executor pre-condition check.
            exp_gen = observed.state_generation

            if state and state.lifecycle == "active":
                b_fp = state.baseline_fingerprint
                if r_fp == b_fp == c_fp:
                    return SkillOperation(
                        action="NOOP",
                        rule_id="INV-TR-07",
                        target=target,
                        reason=f"Copied skill '{target.skill_name}' matches canonical and baseline",
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                        is_authorized=True,
                    )
                if r_fp == b_fp and r_fp != c_fp:
                    return SkillOperation(
                        action="UPDATE",
                        rule_id="INV-TR-08",
                        target=target,
                        reason=f"Update copied skill '{target.skill_name}' to match upstream canonical changes",
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                        next_state_lifecycle="active",
                        next_baseline_origin="write",
                        is_authorized=True,
                    )
                if r_fp != b_fp and r_fp != c_fp:
                    auth = force
                    return SkillOperation(
                        action="UPDATE" if auth else "CONFLICT",
                        rule_id="INV-TR-09",
                        target=target,
                        reason=f"Copied project skill '{target.skill_name}' drifted from workspace skill",
                        finding=(
                            None
                            if auth
                            else f"Project skill '{target.skill_name}' drifted at {target.target_path}"
                        ),
                        requires_force=True,
                        force_type="overwrite_drift",
                        is_authorized=auth,
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                        next_state_lifecycle="active" if auth else None,
                        next_baseline_origin="write" if auth else None,
                    )
                if r_fp != b_fp and r_fp == c_fp:
                    return SkillOperation(
                        action="RECONCILE_STATE",
                        rule_id="INV-TR-10",
                        target=target,
                        reason=f"Reconcile management state for skill '{target.skill_name}' to upstream canonical",
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                        next_state_lifecycle="active",
                        next_baseline_origin="reconcile",
                        is_authorized=True,
                    )

            if state and state.lifecycle == "inactive":
                if r_fp == c_fp:
                    if force:
                        return SkillOperation(
                            action="REACTIVATE_STATE",
                            rule_id="INV-TR-18",
                            target=target,
                            reason=f"Reactivate state for inactive copied skill '{target.skill_name}'",
                            requires_force=True,
                            force_type="reactivate_state",
                            is_authorized=True,
                            expected_representation="copy",
                            desired_representation="copy",
                            expected_fingerprint=r_fp,
                            desired_fingerprint=c_fp,
                            expected_generation=exp_gen,
                            next_state_lifecycle="active",
                            next_baseline_origin="reactivate",
                        )
                    return SkillOperation(
                        action="NOOP",
                        rule_id="INV-TR-18",
                        target=target,
                        reason=f"Inactive copied skill '{target.skill_name}' matches canonical (unmanaged)",
                        finding=None,
                        requires_force=False,
                        is_authorized=True,
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                    )
                auth = force
                return SkillOperation(
                    action="UPDATE" if auth else "CONFLICT",
                    rule_id="INV-TR-19",
                    target=target,
                    reason=f"Inactive copied skill '{target.skill_name}' differs from canonical",
                    finding=(
                        None
                        if auth
                        else f"Inactive copied skill '{target.skill_name}' differs from canonical; review diff or use --force to overwrite"
                    ),
                    requires_force=True,
                    force_type="overwrite_unmanaged",
                    is_authorized=auth,
                    expected_representation="copy",
                    desired_representation="copy",
                    expected_fingerprint=r_fp,
                    desired_fingerprint=c_fp,
                    expected_generation=exp_gen,
                    next_state_lifecycle="active" if auth else None,
                    next_baseline_origin="write" if auth else None,
                )

            # No state record
            if r_fp == c_fp:
                if force:
                    return SkillOperation(
                        action="CLAIM_STATE",
                        rule_id="INV-TR-11",
                        target=target,
                        reason=f"Claim management state for skill '{target.skill_name}'",
                        requires_force=True,
                        force_type="claim_state",
                        is_authorized=True,
                        expected_representation="copy",
                        desired_representation="copy",
                        expected_fingerprint=r_fp,
                        desired_fingerprint=c_fp,
                        expected_generation=exp_gen,
                        next_state_lifecycle="active",
                        next_baseline_origin="claim",
                    )
                return SkillOperation(
                    action="NOOP",
                    rule_id="INV-TR-11",
                    target=target,
                    reason=f"Unmanaged skill directory '{target.skill_name}' matches canonical",
                    finding=None,
                    requires_force=False,
                    is_authorized=True,
                    expected_representation="copy",
                    desired_representation="copy",
                    expected_fingerprint=r_fp,
                    desired_fingerprint=c_fp,
                    expected_generation=exp_gen,
                )
            auth = force
            return SkillOperation(
                action="UPDATE" if auth else "CONFLICT",
                rule_id="INV-TR-12",
                target=target,
                reason=f"Unmanaged skill directory '{target.skill_name}' conflicts with canonical skill",
                finding=(
                    None
                    if auth
                    else f"Unmanaged directory for skill '{target.skill_name}' conflicts with canonical; use --force to overwrite"
                ),
                requires_force=True,
                force_type="overwrite_unmanaged",
                is_authorized=auth,
                expected_representation="copy",
                desired_representation="copy",
                expected_fingerprint=r_fp,
                desired_fingerprint=c_fp,
                expected_generation=exp_gen,
                next_state_lifecycle="active" if auth else None,
                next_baseline_origin="write" if auth else None,
            )

        return SkillOperation(
            action="CONFLICT",
            rule_id="INV-TR-13",
            target=target,
            reason=f"Target path {target.target_path} is an unsupported filesystem entry",
            finding=f"Target path is unsupported filesystem entry: {target.target_path}",
            expected_representation="unsupported",
            desired_representation="copy",
            is_authorized=False,
        )

    # 2. Deselected skills: mode == "absent"
    if observed.entry_type == "symlink":
        if observed.link_points_to_canonical or observed.link_points_within_canonical:
            return SkillOperation(
                action="UNLINK",
                rule_id="INV-TR-14",
                target=target,
                reason=f"Remove deselected symbolic link for skill '{target.skill_name}'",
                expected_representation="link",
                desired_representation="absent",
                expected_generation=exp_gen,
                is_authorized=True,
            )
        return SkillOperation(
            action="NOOP",
            rule_id="INV-TR-15",
            target=target,
            reason=f"Preserve unmanaged symbolic link for deselected skill '{target.skill_name}'",
            expected_representation="symlink",
            desired_representation="absent",
            is_authorized=True,
        )
    if observed.entry_type == "dir":
        state = observed.state_record
        if state and state.lifecycle == "active":
            return SkillOperation(
                action="DEACTIVATE_STATE",
                rule_id="INV-TR-16",
                target=target,
                reason=f"Deactivate state record for deselected copy skill '{target.skill_name}'; preserve directory",
                expected_representation="copy",
                desired_representation="absent",
                expected_fingerprint=observed.runtime_fingerprint,
                expected_generation=exp_gen,
                next_state_lifecycle="inactive",
                is_authorized=True,
            )
        return SkillOperation(
            action="NOOP",
            rule_id="INV-TR-17",
            target=target,
            reason=f"Preserve unmanaged or inactive copy directory for deselected skill '{target.skill_name}'",
            expected_representation="copy",
            desired_representation="absent",
            is_authorized=True,
        )

    state = observed.state_record
    if state and state.lifecycle == "active":
        return SkillOperation(
            action="DEACTIVATE_STATE",
            rule_id="INV-TR-16",
            target=target,
            reason=f"Deactivate state record for absent deselected skill '{target.skill_name}'",
            expected_representation="missing",
            desired_representation="absent",
            expected_generation=exp_gen,
            next_state_lifecycle="inactive",
            is_authorized=True,
        )
    return SkillOperation(
        action="NOOP",
        rule_id="INV-TR-17",
        target=target,
        reason=f"Deselected skill '{target.skill_name}' is already absent",
        expected_representation="missing",
        desired_representation="absent",
        is_authorized=True,
    )


def format_authorization_item(op: SkillOperation) -> str:
    """Format an authorization token bound to seven attributes per INV-AUTH-03."""
    t = op.target
    exp_fp = op.expected_fingerprint or "-"
    des_fp = op.desired_fingerprint or "-"
    exp_rep = op.expected_representation
    des_rep = op.desired_representation
    gen = op.expected_generation if op.expected_generation is not None else "-"
    next_lc = op.next_state_lifecycle or "-"
    return (
        f"AUTH: [{op.action}/{op.force_type or 'default'}] "
        f"project='{t.project_name}' skill='{t.skill_name}' "
        f"path='{t.target_path.as_posix()}' "
        f"rep='{exp_rep}->{des_rep}' "
        f"fp='{exp_fp}->{des_fp}' "
        f"state_gen='{gen}->{next_lc}'"
    )


def build_skill_plan(
    workspace_root: Path,
    project_name: str,
    operations: Sequence[SkillOperation],
    *,
    config_cas: CandidatePathCAS | None = None,
    extra_findings: Sequence[str] | None = None,
) -> SkillPlan:
    """Sort operations stably, compile findings and authorizations into an immutable SkillPlan."""
    # Stably sort operations by physical checkout path, then skill name
    sorted_ops = tuple(
        sorted(
            operations,
            key=lambda op: (
                op.target.physical_checkout.as_posix(),
                op.target.skill_name,
            ),
        )
    )

    findings_list: list[str] = list(extra_findings) if extra_findings else []
    auth_list: list[str] = []

    for op in sorted_ops:
        if op.finding and op.finding not in findings_list:
            findings_list.append(op.finding)
        if op.requires_force and op.is_authorized:
            auth_list.append(format_authorization_item(op))

    can_apply = not any(
        op.action == "CONFLICT" or not op.is_authorized for op in sorted_ops
    ) and not any(
        "conflict" in f.lower() or "collision" in f.lower() for f in findings_list
    )

    return SkillPlan(
        workspace_root=workspace_root,
        project_name=project_name,
        operations=sorted_ops,
        findings=tuple(findings_list),
        authorizations=tuple(auth_list),
        can_apply=can_apply,
        config_cas=config_cas,
    )
