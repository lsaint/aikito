"""Skill inspection, verification, staging, atomic execution, and rollback.

Hides platform directory replacement differences, manages checkout-local staging,
verifies staging fingerprints against canonical secondary checks, and commits
guarded transitions under writer lock.
"""

from __future__ import annotations

import base64
import copy
import os
import shutil
import sys
import uuid

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .compat import (
    _resolve_symlink_target,
    get_physical_path,
    is_reparse_point,
    normalize_file_bytes,
    require_symlink_support,
    safe_symlink,
)
from .skill_plan import (
    DesiredSkill,
    ObservedSkill,
    SkillOperation,
    SkillPlan,
    SkillTarget,
)
from .skill_state import (
    ProjectSkillStateDocument,
    SkillStateRecord,
    SkillTransactionJournal,
    SkillWriterLock,
    calculate_directory_fingerprint,
    delete_transaction_journal,
    get_binding_hash,
    load_project_skill_state,
    run_recovery_pass,
    save_project_skill_state,
    write_transaction_journal,
)


@dataclass(frozen=True)
class SkillExecutionResult:
    """Execution outcomes reported to caller without stdout scraping."""

    applied_ops: tuple[SkillOperation, ...]
    skipped_ops: tuple[SkillOperation, ...]
    failed_ops: tuple[SkillOperation, ...]
    rolled_back_ops: tuple[SkillOperation, ...]
    recovery_required: bool
    content_changes: int
    state_only_changes: int
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return (
            not self.failed_ops
            and not self.recovery_required
            and self.error_message is None
        )


def validate_canonical_skill_source(
    workspace_root: Path, skill_name: str
) -> tuple[Path | None, str | None]:
    """Verify that skills/<skill_name> is a valid, non-escaping directory item.

    Returns (canonical_path, error).
    """
    skills_dir = workspace_root / "skills"
    if not skills_dir.exists():
        return None, f"Workspace skills directory missing: {skills_dir}"
    if is_reparse_point(skills_dir):
        return (
            None,
            f"Workspace skills root cannot be a symlink or reparse point: {skills_dir}",
        )

    canonical = skills_dir / skill_name
    if not canonical.exists():
        return canonical, f"Canonical skill source does not exist: {canonical}"
    if is_reparse_point(canonical):
        return (
            canonical,
            f"Canonical skill directory cannot be a symlink or reparse point: {canonical}",
        )
    if not canonical.is_dir():
        return canonical, f"Canonical skill source is not a directory: {canonical}"

    # Verify no parent directory escapes workspace boundary
    try:
        resolved_ws = workspace_root.resolve(strict=True)
        resolved_canon = canonical.resolve(strict=True)
        if not resolved_canon.is_relative_to(resolved_ws):
            return (
                canonical,
                f"Canonical skill path escapes workspace boundary: {canonical} -> {resolved_canon}",
            )
    except (ValueError, OSError) as exc:
        return canonical, f"Boundary resolution failed for {canonical}: {exc}"

    return canonical, None


def inspect_skill_target(
    target: SkillTarget,
    desired_mode: str,
    home: Path,
) -> tuple[ObservedSkill, DesiredSkill]:
    """Inspect live filesystem entry and state record for a skill target."""
    canonical_path, canonical_err = validate_canonical_skill_source(
        target.workspace_root, target.skill_name
    )
    canonical_fp: str | None = None
    canonical_valid = canonical_err is None

    if canonical_valid and canonical_path and canonical_path.is_dir():
        c_fp, c_fp_err = calculate_directory_fingerprint(canonical_path)
        if c_fp_err:
            canonical_valid = False
            canonical_err = c_fp_err
        else:
            canonical_fp = c_fp

    runtime_path = target.target_path
    entry_type = "missing"
    raw_link_target: Path | None = None
    resolved_link_target: Path | None = None
    link_points_to_canonical = False
    link_points_within_canonical = False
    runtime_fp: str | None = None
    target_lstat: Any = None

    if runtime_path.is_symlink():
        entry_type = "symlink"
        try:
            target_lstat = runtime_path.lstat()
            resolved_link_target = _resolve_symlink_target(runtime_path)
            raw_val = os.readlink(runtime_path)
            raw_link_target = (
                runtime_path.parent / raw_val
                if not os.path.isabs(raw_val)
                else Path(raw_val)
            )
        except OSError:
            pass

        canonical_skills_root = (target.workspace_root / "skills").resolve(strict=False)
        link_points_within_canonical = False
        for cand in (resolved_link_target, raw_link_target):
            if cand:
                try:
                    if cand.resolve(strict=False).is_relative_to(canonical_skills_root):
                        link_points_within_canonical = True
                        break
                except Exception:
                    pass

        if canonical_path:
            expected_canon = canonical_path.resolve(strict=False)
            if (
                resolved_link_target
                and resolved_link_target.resolve(strict=False) == expected_canon
            ):
                link_points_to_canonical = True
            elif (
                raw_link_target
                and raw_link_target.resolve(strict=False) == expected_canon
            ):
                link_points_to_canonical = True
    elif runtime_path.exists():
        if runtime_path.is_dir():
            entry_type = "dir"
            try:
                target_lstat = runtime_path.lstat()
            except OSError:
                pass
            r_fp, _ = calculate_directory_fingerprint(runtime_path)
            runtime_fp = r_fp
        else:
            entry_type = "unsupported"
            try:
                target_lstat = runtime_path.lstat()
            except OSError:
                pass

    # Load host state record
    state_doc, state_err = load_project_skill_state(
        home, target.workspace_root, target.project_name, target.physical_checkout
    )
    state_record: SkillStateRecord | None = None
    state_generation: int = 0
    if state_doc:
        state_generation = state_doc.generation
        if target.skill_name in state_doc.records:
            state_record = state_doc.records[target.skill_name]

    observed = ObservedSkill(
        target=target,
        entry_type=entry_type,
        raw_link_target=raw_link_target,
        resolved_link_target=resolved_link_target,
        link_points_to_canonical=link_points_to_canonical,
        link_points_within_canonical=link_points_within_canonical,
        canonical_valid=canonical_valid,
        canonical_error=canonical_err,
        runtime_fingerprint=runtime_fp,
        canonical_fingerprint=canonical_fp,
        state_record=state_record,
        state_error=state_err,
        target_lstat=target_lstat,
        state_generation=state_generation,
    )

    desired = DesiredSkill(
        skill_name=target.skill_name,
        mode=desired_mode,
        canonical_path=canonical_path
        or (target.workspace_root / "skills" / target.skill_name),
        canonical_fingerprint=canonical_fp,
    )

    return observed, desired


def get_live_representation(path: Path) -> str:
    """Return the current filesystem entry representation: 'missing', 'link', 'copy', or 'unsupported'."""
    if path.is_symlink():
        return "link"
    if not path.exists():
        return "missing"
    if path.is_dir():
        return "copy"
    return "unsupported"


def execute_skill_plan(
    plan: SkillPlan,
    home: Path,
    *,
    dry_run: bool = False,
) -> SkillExecutionResult:
    """Apply a SkillPlan under the global SkillWriterLock."""
    if not plan.can_apply:
        return SkillExecutionResult(
            applied_ops=(),
            skipped_ops=(),
            failed_ops=plan.operations,
            rolled_back_ops=(),
            recovery_required=False,
            content_changes=0,
            state_only_changes=0,
            error_message="Plan contains authorization or preflight errors; cannot apply.",
        )

    # Dry-run does not acquire locks or modify state
    if dry_run:
        content_changes = sum(
            1 for op in plan.operations if op.action in ("CREATE", "UPDATE", "UNLINK")
        )
        state_only_changes = sum(
            1
            for op in plan.operations
            if op.action in ("RECONCILE_STATE", "CLAIM_STATE", "REACTIVATE_STATE")
        )
        return SkillExecutionResult(
            applied_ops=plan.operations,
            skipped_ops=(),
            failed_ops=(),
            rolled_back_ops=(),
            recovery_required=False,
            content_changes=content_changes,
            state_only_changes=state_only_changes,
        )

    # Mutating execution under SkillWriterLock
    with SkillWriterLock(home):
        # 1. Recovery pass
        rec_needed, rec_msg = run_recovery_pass(
            home,
            plan.workspace_root,
            affected_projects=[plan.project_name],
            affected_skills=[op.target.skill_name for op in plan.operations],
        )
        if rec_needed:
            return SkillExecutionResult(
                applied_ops=(),
                skipped_ops=(),
                failed_ops=(),
                rolled_back_ops=(),
                recovery_required=True,
                content_changes=0,
                state_only_changes=0,
                error_message=f"Pending transaction recovered: {rec_msg}. Please re-run command.",
            )
        if rec_msg is not None:
            # Hard recovery error (corrupted journal or security sandbox escape)
            return SkillExecutionResult(
                applied_ops=(),
                skipped_ops=(),
                failed_ops=(),
                rolled_back_ops=(),
                recovery_required=True,
                content_changes=0,
                state_only_changes=0,
                error_message=f"Recovery failed: {rec_msg}",
            )

        # 2. Group operations by physical checkout and pre-load state documents
        ops_by_checkout: dict[Path, list[SkillOperation]] = {}
        for op in plan.operations:
            ops_by_checkout.setdefault(op.target.physical_checkout, []).append(op)

        initial_raw_docs: dict[Path, ProjectSkillStateDocument | None] = {}
        initial_docs: dict[Path, ProjectSkillStateDocument] = {}
        for checkout_path in ops_by_checkout:
            doc, load_err = load_project_skill_state(
                home, plan.workspace_root, plan.project_name, checkout_path
            )
            if load_err:
                return SkillExecutionResult(
                    applied_ops=(),
                    skipped_ops=(),
                    failed_ops=plan.operations,
                    rolled_back_ops=(),
                    recovery_required=False,
                    content_changes=0,
                    state_only_changes=0,
                    error_message=f"State loading error: {load_err}",
                )
            initial_raw_docs[checkout_path] = doc
            if doc is None:
                doc = ProjectSkillStateDocument(
                    version=1,
                    generation=0,
                    workspace_root=plan.workspace_root.as_posix(),
                    project_name=plan.project_name,
                    physical_checkout=checkout_path.as_posix(),
                    records={},
                )
            initial_docs[checkout_path] = doc

        # 3. CAS verification of candidate path
        if plan.config_cas is not None and not plan.config_cas.is_noop:
            cas = plan.config_cas
            try:
                live_bytes = cas.config_path.read_bytes()
            except OSError as exc:
                return SkillExecutionResult(
                    applied_ops=(),
                    skipped_ops=(),
                    failed_ops=plan.operations,
                    rolled_back_ops=(),
                    recovery_required=False,
                    content_changes=0,
                    state_only_changes=0,
                    error_message=f"Failed to read project configuration for CAS verification: {exc}",
                )

            if normalize_file_bytes(live_bytes) != normalize_file_bytes(
                cas.pre_image_bytes
            ):
                return SkillExecutionResult(
                    applied_ops=(),
                    skipped_ops=(),
                    failed_ops=plan.operations,
                    rolled_back_ops=(),
                    recovery_required=False,
                    content_changes=0,
                    state_only_changes=0,
                    error_message=(
                        f"Concurrent configuration modification detected in {cas.config_path}. "
                        "Plan invalidated; please re-run."
                    ),
                )

        # 4. Pre-validate all operations across all checkouts before mutating anything
        for checkout_path, ops in ops_by_checkout.items():
            doc = initial_docs[checkout_path]
            initial_checkout_generation = doc.generation

            for op in ops:
                if op.action == "NOOP":
                    continue

                if (
                    op.expected_generation is not None
                    and initial_checkout_generation != op.expected_generation
                ):
                    return SkillExecutionResult(
                        applied_ops=(),
                        skipped_ops=(),
                        failed_ops=(op,),
                        rolled_back_ops=(),
                        recovery_required=False,
                        content_changes=0,
                        state_only_changes=0,
                        error_message=(
                            f"State document changed since plan for skill '{op.target.skill_name}': "
                            f"expected generation {op.expected_generation}, "
                            f"found {initial_checkout_generation}. Plan invalidated; please re-run."
                        ),
                    )

                target = op.target
                canonical_source = plan.workspace_root / "skills" / target.skill_name

                live_rep = get_live_representation(target.target_path)
                expected_rep = op.expected_representation
                rep_matches = False
                if expected_rep == "missing" and live_rep == "missing":
                    rep_matches = True
                elif expected_rep in ("link", "symlink") and live_rep in (
                    "link",
                    "symlink",
                ):
                    rep_matches = True
                elif expected_rep in ("copy", "dir") and live_rep in ("copy", "dir"):
                    rep_matches = True
                elif (
                    expected_rep in ("unsupported", "file")
                    and live_rep == "unsupported"
                ):
                    rep_matches = True

                if not rep_matches:
                    return SkillExecutionResult(
                        applied_ops=(),
                        skipped_ops=(),
                        failed_ops=(op,),
                        rolled_back_ops=(),
                        recovery_required=False,
                        content_changes=0,
                        state_only_changes=0,
                        error_message=(
                            f"Target representation changed since plan for skill '{target.skill_name}': "
                            f"expected {expected_rep!r}, found {live_rep!r} at {target.target_path}. "
                            "Plan invalidated; please re-run to regenerate plan."
                        ),
                    )

                if expected_rep in ("link", "symlink"):
                    canon_dest = get_physical_path(canonical_source)
                    canon_root = get_physical_path(plan.workspace_root / "skills")
                    link_dest_valid = False
                    try:
                        resolved_target = get_physical_path(
                            _resolve_symlink_target(target.target_path)
                        )
                        if (
                            resolved_target == canon_dest
                            or resolved_target.is_relative_to(canon_root)
                        ):
                            link_dest_valid = True
                    except Exception:
                        pass
                    if not link_dest_valid:
                        return SkillExecutionResult(
                            applied_ops=(),
                            skipped_ops=(),
                            failed_ops=(op,),
                            rolled_back_ops=(),
                            recovery_required=False,
                            content_changes=0,
                            state_only_changes=0,
                            error_message=(
                                f"Symbolic link destination changed since plan for skill '{target.skill_name}': "
                                f"{target.target_path} no longer points to canonical source. "
                                "Plan invalidated; please re-run to regenerate plan."
                            ),
                        )

                if op.expected_fingerprint is not None:
                    if live_rep != "copy":
                        return SkillExecutionResult(
                            applied_ops=(),
                            skipped_ops=(),
                            failed_ops=(op,),
                            rolled_back_ops=(),
                            recovery_required=False,
                            content_changes=0,
                            state_only_changes=0,
                            error_message=(
                                f"Target entry for skill '{target.skill_name}' is no longer a directory. "
                                "Plan invalidated; please re-run."
                            ),
                        )
                    live_r_fp, live_r_err = calculate_directory_fingerprint(
                        target.target_path
                    )
                    if live_r_err or live_r_fp != op.expected_fingerprint:
                        return SkillExecutionResult(
                            applied_ops=(),
                            skipped_ops=(),
                            failed_ops=(op,),
                            rolled_back_ops=(),
                            recovery_required=False,
                            content_changes=0,
                            state_only_changes=0,
                            error_message=(
                                f"Runtime content changed since plan for skill '{target.skill_name}': "
                                f"expected fingerprint {op.expected_fingerprint!r}, "
                                f"found {live_r_fp!r}. Plan invalidated; please re-run."
                            ),
                        )

                if op.action == "CREATE" and op.desired_representation == "link":
                    canon_path, canon_err = validate_canonical_skill_source(
                        plan.workspace_root, target.skill_name
                    )
                    if canon_err is not None:
                        return SkillExecutionResult(
                            applied_ops=(),
                            skipped_ops=(),
                            failed_ops=(op,),
                            rolled_back_ops=(),
                            recovery_required=False,
                            content_changes=0,
                            state_only_changes=0,
                            error_message=f"Canonical skill source invalid for '{target.skill_name}': {canon_err}",
                        )

        if all(op.action == "NOOP" for op in plan.operations) and (
            plan.config_cas is None or plan.config_cas.is_noop
        ):
            return SkillExecutionResult(
                applied_ops=(),
                skipped_ops=plan.operations,
                failed_ops=(),
                rolled_back_ops=(),
                recovery_required=False,
                content_changes=0,
                state_only_changes=0,
            )

        # 5. Mutating execution under transactional journal and rollback protection
        applied_ops: list[SkillOperation] = []
        skipped_ops: list[SkillOperation] = []
        content_changes = 0
        state_only_changes = 0

        plan_tx_id = uuid.uuid4().hex
        active_tx_roots: set[Path] = set()
        config_applied: bool = False

        # Pre-construct transaction journal covering all planned operations
        journal_files: list[dict[str, Any]] = []
        if plan.config_cas is not None and not plan.config_cas.is_noop:
            cas = plan.config_cas
            journal_files.append(
                {
                    "path": str(cas.config_path),
                    "pre_image_base64": base64.b64encode(cas.pre_image_bytes).decode(
                        "ascii"
                    ),
                    "post_image_base64": base64.b64encode(cas.post_image_bytes).decode(
                        "ascii"
                    ),
                }
            )

        journal_symlinks: list[dict[str, Any]] = []
        journal_recovery_dirs: list[dict[str, Any]] = []
        op_staging_dirs: dict[tuple[Path, str], tuple[Path, Path]] = {}

        for checkout_path, ops in ops_by_checkout.items():
            tx_checkout_root = checkout_path / ".agents" / ".aikito-tx" / plan_tx_id
            for op in ops:
                target = op.target
                canon_source = plan.workspace_root / "skills" / target.skill_name

                if op.action == "UNLINK":
                    orig_link = (
                        str(_resolve_symlink_target(target.target_path))
                        if target.target_path.is_symlink()
                        else None
                    )
                    journal_symlinks.append(
                        {
                            "target_path": str(target.target_path),
                            "pre_link": orig_link,
                            "post_link": None,
                        }
                    )
                elif op.action == "CREATE" and op.desired_representation == "link":
                    orig_link = (
                        str(_resolve_symlink_target(target.target_path))
                        if target.target_path.is_symlink()
                        else None
                    )
                    journal_symlinks.append(
                        {
                            "target_path": str(target.target_path),
                            "pre_link": orig_link,
                            "post_link": str(canon_source),
                        }
                    )
                elif (
                    op.action in ("CREATE", "UPDATE")
                    and op.desired_representation == "copy"
                ):
                    staging_dir = tx_checkout_root / f"stage-{target.skill_name}"
                    recovery_dir = tx_checkout_root / f"prev-{target.skill_name}"
                    op_staging_dirs[(checkout_path, target.skill_name)] = (
                        staging_dir,
                        recovery_dir,
                    )
                    active_tx_roots.add(tx_checkout_root)
                    target_had_entry = (
                        target.target_path.is_symlink() or target.target_path.exists()
                    )
                    journal_recovery_dirs.append(
                        {
                            "target_path": str(target.target_path),
                            "recovery_dir": str(recovery_dir)
                            if target_had_entry
                            else "",
                            "staging_dir": str(staging_dir),
                            "created": not target_had_entry,
                            "pre_fingerprint": op.expected_fingerprint,
                            "post_fingerprint": op.desired_fingerprint,
                        }
                    )
                elif op.action == "UPDATE" and op.desired_representation == "link":
                    recovery_dir = tx_checkout_root / f"prev-{target.skill_name}"
                    op_staging_dirs[(checkout_path, target.skill_name)] = (
                        tx_checkout_root,
                        recovery_dir,
                    )
                    active_tx_roots.add(tx_checkout_root)
                    target_had_entry = (
                        target.target_path.is_symlink() or target.target_path.exists()
                    )
                    journal_recovery_dirs.append(
                        {
                            "target_path": str(target.target_path),
                            "recovery_dir": str(recovery_dir)
                            if target_had_entry
                            else "",
                            "staging_dir": "",
                            "created": False,
                            "pre_fingerprint": op.expected_fingerprint,
                            "post_fingerprint": None,
                            "expected_symlink_target": str(canon_source),
                        }
                    )

        journal_state_transitions: list[dict[str, Any]] = []
        for checkout_path in ops_by_checkout:
            raw_orig = initial_raw_docs[checkout_path]
            pre_gen = raw_orig.generation if raw_orig is not None else 0
            journal_state_transitions.append(
                {
                    "binding_hash": get_binding_hash(
                        plan.workspace_root, plan.project_name, checkout_path
                    ),
                    "workspace_root": plan.workspace_root.as_posix(),
                    "project_name": plan.project_name,
                    "physical_checkout": checkout_path.as_posix(),
                    "pre_doc": raw_orig.to_dict() if raw_orig is not None else None,
                    "pre_generation": pre_gen,
                    "post_docs": [],
                }
            )

        journal = SkillTransactionJournal(
            tx_id=plan_tx_id,
            kind="runtime_apply",
            phase="pending",
            workspace_root=plan.workspace_root.as_posix(),
            project_names=[plan.project_name],
            checkout_paths=[cp.as_posix() for cp in ops_by_checkout.keys()],
            affected_skills=list(
                {op.target.skill_name for op in plan.operations if op.target.skill_name}
            ),
            files=journal_files,
            recovery_dirs=journal_recovery_dirs,
            state_transitions=journal_state_transitions,
            symlinks=journal_symlinks,
        )

        _, j_err = write_transaction_journal(home, journal)
        if j_err:
            return SkillExecutionResult(
                applied_ops=(),
                skipped_ops=(),
                failed_ops=plan.operations,
                rolled_back_ops=(),
                recovery_required=False,
                content_changes=0,
                state_only_changes=0,
                error_message=f"Cannot persist transaction journal: {j_err}",
            )

        def persist_state_post_image(
            checkout_path: Path, doc: ProjectSkillStateDocument
        ) -> str | None:
            anticipated = doc.to_dict()
            anticipated["generation"] = doc.generation + 1
            binding_hash = get_binding_hash(
                plan.workspace_root, plan.project_name, checkout_path
            )
            for transition in journal.state_transitions:
                if transition.get("binding_hash") == binding_hash:
                    transition["post_docs"].append(anticipated)
                    break
            _, error = write_transaction_journal(home, journal)
            return error

        def checkpoint_operation(
            op: SkillOperation,
            checkout_path: Path,
            doc: ProjectSkillStateDocument,
        ) -> str | None:
            target_path = str(op.target.target_path)
            journal.recovery_dirs = [
                entry
                for entry in journal.recovery_dirs
                if entry.get("target_path") != target_path
            ]
            journal.symlinks = [
                entry
                for entry in journal.symlinks
                if entry.get("target_path") != target_path
            ]
            binding_hash = get_binding_hash(
                plan.workspace_root, plan.project_name, checkout_path
            )
            for transition in journal.state_transitions:
                if transition.get("binding_hash") == binding_hash:
                    transition["pre_doc"] = doc.to_dict() if doc.records else None
                    transition["pre_generation"] = doc.generation
                    transition["post_docs"] = []
                    break
            if config_applied:
                for file_entry in journal.files:
                    file_entry["pre_image_base64"] = file_entry.get("post_image_base64")
            _, error = write_transaction_journal(home, journal)
            return error

        def rollback_current(
            failed_op: SkillOperation, err_msg: str
        ) -> SkillExecutionResult:
            recovered, recovery_error = run_recovery_pass(
                home,
                affected_workspace=plan.workspace_root,
                affected_projects=[plan.project_name],
                affected_skills=[failed_op.target.skill_name],
                authorized_checkouts=list(ops_by_checkout),
            )
            recovery_required = not recovered
            if recovery_error:
                recovery_required = True
                err_msg = f"{err_msg}; recovery failed: {recovery_error}"
            return SkillExecutionResult(
                applied_ops=tuple(applied_ops),
                skipped_ops=tuple(skipped_ops),
                failed_ops=() if failed_op in applied_ops else (failed_op,),
                rolled_back_ops=(),
                recovery_required=recovery_required,
                content_changes=content_changes,
                state_only_changes=state_only_changes,
                error_message=err_msg,
            )

        if plan.config_cas is not None and not plan.config_cas.is_noop:
            cas = plan.config_cas
            tmp_config = cas.config_path.with_name(f".{cas.config_path.name}.cas_tmp")
            try:
                tmp_config.write_bytes(cas.post_image_bytes)
                os.replace(tmp_config, cas.config_path)
                config_applied = True
            except OSError as exc:
                if tmp_config.exists():
                    tmp_config.unlink()
                delete_transaction_journal(home, plan_tx_id)
                return SkillExecutionResult(
                    applied_ops=(),
                    skipped_ops=(),
                    failed_ops=plan.operations,
                    rolled_back_ops=(),
                    recovery_required=False,
                    content_changes=0,
                    state_only_changes=0,
                    error_message=f"Failed to atomically register candidate path: {exc}",
                )

        working_docs = {cp: copy.deepcopy(d) for cp, d in initial_docs.items()}

        for checkout_path, ops in ops_by_checkout.items():
            doc = working_docs[checkout_path]

            for op in ops:
                target = op.target
                canonical_source = plan.workspace_root / "skills" / target.skill_name

                if op.action == "NOOP":
                    skipped_ops.append(op)
                    continue

                if op.action == "UNLINK":
                    try:
                        if target.target_path.is_symlink():
                            target.target_path.unlink()
                    except OSError as exc:
                        return rollback_current(
                            op, f"Failed to unlink {target.target_path}: {exc}"
                        )
                    checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                    if checkpoint_error:
                        return rollback_current(
                            op, f"Failed to checkpoint unlink: {checkpoint_error}"
                        )
                    content_changes += 1
                    applied_ops.append(op)
                    continue

                if op.action == "CREATE" and op.desired_representation == "link":
                    require_symlink_support()
                    canon_path, canon_err = validate_canonical_skill_source(
                        plan.workspace_root, target.skill_name
                    )
                    if canon_err is not None:
                        return rollback_current(
                            op,
                            f"Canonical skill source invalid for '{target.skill_name}': {canon_err}",
                        )
                    target.target_path.parent.mkdir(parents=True, exist_ok=True)
                    if safe_symlink(canonical_source, target.target_path):
                        checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                        if checkpoint_error:
                            return rollback_current(
                                op,
                                f"Failed to checkpoint symlink creation: {checkpoint_error}",
                            )
                        content_changes += 1
                        applied_ops.append(op)
                    else:
                        return rollback_current(
                            op, f"Failed to create symlink for {target.skill_name}"
                        )
                    continue

                if (
                    op.action in ("CREATE", "UPDATE")
                    and op.desired_representation == "copy"
                ):
                    staging_dir, recovery_dir = op_staging_dirs[
                        (checkout_path, target.skill_name)
                    ]

                    try:
                        staging_dir.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copytree(canonical_source, staging_dir)
                    except Exception as exc:
                        return rollback_current(
                            op,
                            f"Failed to stage copy for skill '{target.skill_name}': {exc}",
                        )

                    stage_fp, stage_err = calculate_directory_fingerprint(staging_dir)
                    canon_fp, canon_err = calculate_directory_fingerprint(
                        canonical_source
                    )
                    if (
                        stage_err
                        or canon_err
                        or stage_fp != op.desired_fingerprint
                        or canon_fp != op.desired_fingerprint
                    ):
                        return rollback_current(
                            op,
                            f"Secondary canonical verification failed for skill '{target.skill_name}'. "
                            "Source changed during staging; apply aborted.",
                        )

                    prev_generation = doc.generation
                    target_had_entry = (
                        target.target_path.is_symlink() or target.target_path.exists()
                    )
                    try:
                        if target_had_entry:
                            target.target_path.replace(recovery_dir)
                        target.target_path.parent.mkdir(parents=True, exist_ok=True)
                        staging_dir.replace(target.target_path)
                    except Exception as exc:
                        return rollback_current(
                            op,
                            f"Failed to replace target for skill '{target.skill_name}': {exc}",
                        )

                    doc.records[target.skill_name] = SkillStateRecord(
                        skill_name=target.skill_name,
                        representation="copy",
                        lifecycle=op.next_state_lifecycle or "active",
                        baseline_fingerprint=op.desired_fingerprint or canon_fp or "",
                        baseline_origin=op.next_baseline_origin or "write",
                        last_observed_selected=True,
                    )
                    journal_error = persist_state_post_image(checkout_path, doc)
                    if journal_error:
                        return rollback_current(
                            op,
                            f"Failed to journal state for skill '{target.skill_name}': {journal_error}",
                        )
                    state_ok, state_err = save_project_skill_state(
                        home, doc, expected_generation=prev_generation
                    )
                    if not state_ok:
                        return rollback_current(
                            op,
                            f"Failed to commit state for skill '{target.skill_name}': {state_err}",
                        )

                    checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                    if checkpoint_error:
                        return rollback_current(
                            op,
                            f"Failed to checkpoint skill '{target.skill_name}': {checkpoint_error}",
                        )
                    content_changes += 1
                    applied_ops.append(op)
                    continue

                if op.action in ("RECONCILE_STATE", "CLAIM_STATE", "REACTIVATE_STATE"):
                    prev_generation = doc.generation
                    doc.records[target.skill_name] = SkillStateRecord(
                        skill_name=target.skill_name,
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint=op.desired_fingerprint or "",
                        baseline_origin=op.next_baseline_origin or "reconcile",
                        last_observed_selected=True,
                    )
                    journal_error = persist_state_post_image(checkout_path, doc)
                    if journal_error:
                        return rollback_current(
                            op,
                            f"Failed to journal state transition for '{target.skill_name}': {journal_error}",
                        )
                    state_ok, state_err = save_project_skill_state(
                        home, doc, expected_generation=prev_generation
                    )
                    if not state_ok:
                        return rollback_current(
                            op,
                            f"Failed to commit state transition for '{target.skill_name}': {state_err}",
                        )
                    checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                    if checkpoint_error:
                        return rollback_current(
                            op,
                            f"Failed to checkpoint state transition for '{target.skill_name}': {checkpoint_error}",
                        )
                    state_only_changes += 1
                    applied_ops.append(op)
                    continue

                if op.action == "DEACTIVATE_STATE":
                    if target.skill_name in doc.records:
                        old = doc.records[target.skill_name]
                        prev_generation = doc.generation
                        doc.records[target.skill_name] = SkillStateRecord(
                            skill_name=target.skill_name,
                            representation=old.representation,
                            lifecycle="inactive",
                            baseline_fingerprint=old.baseline_fingerprint,
                            baseline_origin=old.baseline_origin,
                            last_observed_selected=False,
                        )
                        journal_error = persist_state_post_image(checkout_path, doc)
                        if journal_error:
                            return rollback_current(
                                op,
                                f"Failed to journal state deactivation for '{target.skill_name}': {journal_error}",
                            )
                        state_ok, state_err = save_project_skill_state(
                            home, doc, expected_generation=prev_generation
                        )
                        if not state_ok:
                            return rollback_current(
                                op,
                                f"Failed to commit state deactivation for '{target.skill_name}': {state_err}",
                            )
                        state_only_changes += 1
                    checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                    if checkpoint_error:
                        return rollback_current(
                            op,
                            f"Failed to checkpoint state deactivation for '{target.skill_name}': {checkpoint_error}",
                        )
                    applied_ops.append(op)
                    continue

                if op.action == "UPDATE" and op.desired_representation == "link":
                    # copy -> link mode switch
                    _, recovery_dir = op_staging_dirs[
                        (checkout_path, target.skill_name)
                    ]

                    try:
                        recovery_dir.parent.mkdir(parents=True, exist_ok=True)
                        if (
                            target.target_path.exists()
                            or target.target_path.is_symlink()
                        ):
                            target.target_path.replace(recovery_dir)
                        target.target_path.parent.mkdir(parents=True, exist_ok=True)
                    except Exception as exc:
                        return rollback_current(
                            op,
                            f"Failed to stage copy for mode switch '{target.skill_name}': {exc}",
                        )

                    require_symlink_support()
                    if not safe_symlink(canonical_source, target.target_path):
                        return rollback_current(
                            op,
                            f"Failed to create symlink during copy→link switch for '{target.skill_name}'",
                        )

                    if target.skill_name in doc.records:
                        old = doc.records[target.skill_name]
                        prev_generation = doc.generation
                        doc.records[target.skill_name] = SkillStateRecord(
                            skill_name=target.skill_name,
                            representation="copy",
                            lifecycle="inactive",
                            baseline_fingerprint=old.baseline_fingerprint,
                            baseline_origin=old.baseline_origin,
                            last_observed_selected=False,
                        )
                        journal_error = persist_state_post_image(checkout_path, doc)
                        if journal_error:
                            return rollback_current(
                                op,
                                f"Failed to journal copy→link state for '{target.skill_name}': {journal_error}",
                            )
                        state_ok, state_err = save_project_skill_state(
                            home, doc, expected_generation=prev_generation
                        )
                        if not state_ok:
                            return rollback_current(
                                op,
                                f"Failed to commit state after copy→link switch for '{target.skill_name}': {state_err}",
                            )

                    checkpoint_error = checkpoint_operation(op, checkout_path, doc)
                    if checkpoint_error:
                        return rollback_current(
                            op,
                            f"Failed to checkpoint copy→link switch for '{target.skill_name}': {checkpoint_error}",
                        )
                    content_changes += 1
                    applied_ops.append(op)
                    continue

        # Two-phase commit:
        # Step 1: Mark committed
        journal.phase = "committed"
        _, commit_err = write_transaction_journal(home, journal)
        if commit_err:
            return rollback_current(
                plan.operations[-1]
                if plan.operations
                else SkillOperation(
                    action="NOOP",
                    rule_id="INV-TR-00",
                    target=SkillTarget(
                        plan.workspace_root,
                        "ws",
                        plan.project_name,
                        plan.workspace_root,
                        "",
                        Path(""),
                    ),
                    reason="commit failed",
                ),
                f"Failed to mark transaction committed: {commit_err}",
            )

        # Step 2: Clean up staging and recovery directories
        cleanup_errors: list[str] = []
        for tx_root in active_tx_roots:
            if tx_root.exists():
                try:
                    shutil.rmtree(tx_root)
                except Exception as exc:
                    cleanup_errors.append(f"{tx_root}: {exc}")

        # Step 3: Clean up committed journal only if all directories removed
        if cleanup_errors:
            error_msg = (
                f"Committed transaction {plan_tx_id} directory cleanup failed: "
                f"{'; '.join(cleanup_errors)}. Journal retained for recovery."
            )
            return SkillExecutionResult(
                applied_ops=tuple(applied_ops),
                skipped_ops=tuple(skipped_ops),
                failed_ops=(),
                rolled_back_ops=(),
                recovery_required=True,
                content_changes=content_changes,
                state_only_changes=state_only_changes,
                error_message=error_msg,
            )

        delete_transaction_journal(home, plan_tx_id)

        return SkillExecutionResult(
            applied_ops=tuple(applied_ops),
            skipped_ops=tuple(skipped_ops),
            failed_ops=(),
            rolled_back_ops=(),
            recovery_required=False,
            content_changes=content_changes,
            state_only_changes=state_only_changes,
        )


def execute_selection_transaction(
    home: Path,
    workspace_root: Path,
    project_names: Sequence[str],
    file_updates: Sequence[
        tuple[Path, str, str]
    ],  # (file_path, pre_content, post_content)
    skills_toml_update: tuple[Path, str, str] | None = None,
    canonical_backup_update: tuple[Path, Path]
    | None = None,  # (canonical_dir, backup_dir)
    deactivate_skills: Sequence[str] | None = None,
    write_fn: Any = None,
) -> tuple[bool, str | None]:
    """Perform atomic selection mutation covering configs, canonical staged backup, and local states."""
    if write_fn is None:

        def default_write(path: Path, content: str, encoding: str = "utf-8") -> None:
            path.write_text(content, encoding=encoding)

        write_fn = default_write

    with SkillWriterLock(home):
        # 1. Recovery pass — abort on unrecoverable corrupted journal
        rec_ok, rec_msg = run_recovery_pass(
            home,
            affected_workspace=workspace_root,
            affected_projects=project_names,
            affected_skills=deactivate_skills,
        )
        if rec_ok is False and rec_msg:
            # run_recovery_pass returns (False, error_msg) only for hard errors
            return False, f"Pre-transaction recovery failed: {rec_msg}"
        if rec_ok:
            return (
                False,
                f"Pending transaction recovered: {rec_msg}. Please re-run command.",
            )

        # CAS verification: verify all files match expected pre-image before modifying anything or writing journal
        for f_path, pre_text, _ in file_updates:
            if f_path.is_file():
                current = f_path.read_text(encoding="utf-8")
                if current.replace("\r\n", "\n") != pre_text.replace("\r\n", "\n"):
                    return (
                        False,
                        f"Concurrent modification detected in {f_path}: pre-image mismatch",
                    )
            else:
                if pre_text != "":
                    return (
                        False,
                        f"Expected existing file at {f_path} matching pre-image, but file does not exist",
                    )

        if skills_toml_update:
            s_path, s_pre, _ = skills_toml_update
            if s_path.is_file():
                current = s_path.read_text(encoding="utf-8")
                if current.replace("\r\n", "\n") != s_pre.replace("\r\n", "\n"):
                    return (
                        False,
                        f"Concurrent modification detected in {s_path}: pre-image mismatch",
                    )
            else:
                if s_pre != "":
                    return (
                        False,
                        f"Expected existing file at {s_path} matching pre-image, but file does not exist",
                    )

        tx_id = uuid.uuid4().hex
        journal_files: list[dict[str, Any]] = []

        for f_path, pre_text, post_text in file_updates:
            journal_files.append(
                {
                    "path": str(f_path),
                    "pre_image_base64": base64.b64encode(
                        pre_text.encode("utf-8")
                    ).decode("ascii"),
                    "post_image_base64": base64.b64encode(
                        post_text.encode("utf-8")
                    ).decode("ascii"),
                }
            )

        if skills_toml_update:
            s_path, s_pre, s_post = skills_toml_update
            journal_files.append(
                {
                    "path": str(s_path),
                    "pre_image_base64": base64.b64encode(s_pre.encode("utf-8")).decode(
                        "ascii"
                    ),
                    "post_image_base64": base64.b64encode(
                        s_post.encode("utf-8")
                    ).decode("ascii"),
                }
            )

        recovery_dirs: list[dict[str, str]] = []
        canon_dir: Path | None = None
        backup_dir: Path | None = None
        if canonical_backup_update:
            canon_dir, _ = canonical_backup_update
            backup_dir = workspace_root / ".aikito-tx" / tx_id / "canonical_backup"
            canonical_fingerprint, fingerprint_error = calculate_directory_fingerprint(
                canon_dir
            )
            if fingerprint_error:
                return (
                    False,
                    f"Cannot snapshot canonical skill for rollback: {fingerprint_error}",
                )
            recovery_dirs.append(
                {
                    "target_path": str(canon_dir),
                    "recovery_dir": str(backup_dir),
                    "staging_dir": "",
                    "pre_fingerprint": canonical_fingerprint or "",
                    "post_fingerprint": None,
                }
            )

        # Snapshot current state records for journal rollback entries
        tx_checkouts: set[Path] = set()
        state_transitions: list[dict[str, Any]] = []
        if deactivate_skills:
            from .project import resolve_project_binding

            for proj in project_names:
                proj_dir = workspace_root / "projects" / proj
                agent_toml = proj_dir / "agent.toml"
                if agent_toml.is_file():
                    try:
                        cfg = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
                        binding = resolve_project_binding(cfg, home)
                        for entry in binding.entries:
                            co_path = get_physical_path(entry.resolved_path)
                            tx_checkouts.add(co_path)
                            doc, load_err = load_project_skill_state(
                                home, workspace_root, proj, co_path
                            )
                            if load_err:
                                return (
                                    False,
                                    f"Cannot load state for pre-image snapshot: {load_err}",
                                )
                            b_hash = get_binding_hash(workspace_root, proj, co_path)
                            pre_gen = doc.generation if doc else 0
                            post_docs: list[dict[str, Any]] = []
                            if doc is not None:
                                anticipated = copy.deepcopy(doc)
                                for skill_name in deactivate_skills:
                                    if skill_name in anticipated.records:
                                        old = anticipated.records[skill_name]
                                        anticipated.records[skill_name] = (
                                            SkillStateRecord(
                                                skill_name=skill_name,
                                                representation=old.representation,
                                                lifecycle="inactive",
                                                baseline_fingerprint=old.baseline_fingerprint,
                                                baseline_origin=old.baseline_origin,
                                                last_observed_selected=False,
                                            )
                                        )
                                anticipated.generation += 1
                                post_docs.append(anticipated.to_dict())
                            state_transitions.append(
                                {
                                    "binding_hash": b_hash,
                                    "workspace_root": workspace_root.as_posix(),
                                    "project_name": proj,
                                    "physical_checkout": co_path.as_posix(),
                                    "pre_doc": doc.to_dict() if doc else None,
                                    "pre_generation": pre_gen,
                                    "post_docs": post_docs,
                                }
                            )
                    except Exception as exc:
                        return False, f"Failed to snapshot state for rollback: {exc}"

        checkout_paths_list = [cp.as_posix() for cp in sorted(tx_checkouts, key=str)]

        journal = SkillTransactionJournal(
            tx_id=tx_id,
            kind="selection_mutation",
            phase="pending",
            workspace_root=workspace_root.as_posix(),
            project_names=list(project_names),
            checkout_paths=checkout_paths_list,
            affected_skills=list(deactivate_skills) if deactivate_skills else [],
            files=journal_files,
            recovery_dirs=recovery_dirs,
            state_transitions=state_transitions,
        )
        j_path, j_err = write_transaction_journal(home, journal)
        if j_err:
            return False, f"Cannot persist transaction journal: {j_err}"

        canonical_staged = False
        try:
            # 1. Stage canonical directory if requested
            if canon_dir and backup_dir and canon_dir.exists():
                backup_dir.parent.mkdir(parents=True, exist_ok=True)
                canon_dir.replace(backup_dir)
                canonical_staged = True

            # 2. Apply file updates
            for f_path, _, post_text in file_updates:
                write_fn(f_path, post_text, encoding="utf-8")

            if skills_toml_update:
                s_path, _, s_post = skills_toml_update
                write_fn(s_path, s_post, encoding="utf-8")

            # 3. Apply state deactivations — propagate failures instead of swallowing
            if deactivate_skills:
                for proj in project_names:
                    proj_dir = workspace_root / "projects" / proj
                    agent_toml = proj_dir / "agent.toml"
                    if agent_toml.is_file():
                        cfg = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
                        binding = resolve_project_binding(cfg, home)
                        for entry in binding.entries:
                            co_path = get_physical_path(entry.resolved_path)
                            doc, load_err = load_project_skill_state(
                                home, workspace_root, proj, co_path
                            )
                            if load_err:
                                raise RuntimeError(
                                    f"State load failed during deactivation: {load_err}"
                                )
                            if doc:
                                for skill_name in deactivate_skills:
                                    if skill_name in doc.records:
                                        old = doc.records[skill_name]
                                        doc.records[skill_name] = SkillStateRecord(
                                            skill_name=skill_name,
                                            representation=old.representation,
                                            lifecycle="inactive",
                                            baseline_fingerprint=old.baseline_fingerprint,
                                            baseline_origin=old.baseline_origin,
                                            last_observed_selected=False,
                                        )
                                state_ok, state_save_err = save_project_skill_state(
                                    home, doc
                                )
                                if not state_ok:
                                    raise RuntimeError(
                                        f"State save failed during deactivation: {state_save_err}"
                                    )

            # 4. Mark committed before cleaning up backup
            committed_journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="selection_mutation",
                phase="committed",
                workspace_root=workspace_root.as_posix(),
                project_names=list(project_names),
                checkout_paths=checkout_paths_list,
                affected_skills=list(deactivate_skills) if deactivate_skills else [],
                files=journal_files,
                recovery_dirs=recovery_dirs,
                state_transitions=state_transitions,
            )
            _, commit_err = write_transaction_journal(home, committed_journal)
            if commit_err:
                raise RuntimeError(f"Cannot mark transaction committed: {commit_err}")

            cleanup_errors: list[str] = []
            if canonical_staged and backup_dir and backup_dir.exists():
                try:
                    shutil.rmtree(backup_dir)
                except Exception as exc:
                    cleanup_errors.append(f"backup dir {backup_dir}: {exc}")

            ws_tx_dir = workspace_root / ".aikito-tx" / tx_id
            if ws_tx_dir.is_dir():
                try:
                    shutil.rmtree(ws_tx_dir)
                except Exception as exc:
                    cleanup_errors.append(f"tx dir {ws_tx_dir}: {exc}")

            if cleanup_errors:
                return (
                    False,
                    f"Committed selection transaction cleanup failed: {'; '.join(cleanup_errors)}. Journal retained for recovery.",
                )

            delete_transaction_journal(home, tx_id)
            return True, None
        except Exception as exc:
            recovered, recovery_error = run_recovery_pass(
                home,
                affected_workspace=workspace_root,
                affected_projects=project_names,
                affected_skills=deactivate_skills,
            )
            message = str(exc)
            if not recovered:
                message += f"; rollback incomplete: {recovery_error or 'pending journal retained'}"
            return False, message
