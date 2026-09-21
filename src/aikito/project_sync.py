"""Project synchronization orchestration, preflight validation, and batch execution.

Coordinates target resolution, granular SkillPlan evaluation, explicit path CAS binding,
and backward-compatible project instructions/memory synchronization.
"""

from __future__ import annotations

import hashlib
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .compat import check_case_collision, safe_relative_path
from .conflict import collect_resource_conflicts
from .init import project_sync_validation_error
from .instructions import (
    InstructionExecutionResult,
    InstructionPlan,
    build_project_instruction_batch,
    execute_instruction_plan,
    plan_instructions,
)
from .memory_runtime import (
    MemoryExecutionResult,
    MemoryPlan,
    build_project_memory_batch,
    execute_memory_plan,
    plan_project_memory,
)
from .project import (
    append_candidate_path_to_config,
    resolve_project_binding,
)
from .skill_plan import (
    CandidatePathCAS,
    DesiredSkill,
    ObservedSkill,
    SkillOperation,
    SkillPlan,
    SkillTarget,
    build_skill_plan,
    plan_single_skill,
)
from .skill_runtime import (
    SkillExecutionResult,
    execute_skill_plan,
    inspect_skill_target,
)
from .skill_state import load_project_skill_state


@dataclass(frozen=True)
class ProjectSyncBatch:
    """Complete preflight batch result for one project synchronization request."""

    workspace_root: Path
    project_name: str
    active_checkouts: tuple[Path, ...]
    offline_checkouts: tuple[Path, ...]
    skill_plan: SkillPlan
    preflight_findings: tuple[str, ...]
    can_apply: bool
    config_cas: CandidatePathCAS | None = None
    instruction_plan: InstructionPlan | None = None
    memory_plan: MemoryPlan | None = None


@dataclass(frozen=True)
class ProjectSyncExecutionResult:
    """Segmented execution outcome for a project sync batch."""

    skill_result: SkillExecutionResult
    instruction_result: InstructionExecutionResult | None = None
    memory_result: MemoryExecutionResult | None = None
    error_message: str | None = None

    @property
    def is_success(self) -> bool:
        return (
            self.skill_result.is_success
            and (self.instruction_result is None or self.instruction_result.success)
            and (self.memory_result is None or self.memory_result.success)
            and self.error_message is None
        )

    @property
    def applied_ops(self) -> tuple[SkillOperation, ...]:
        return self.skill_result.applied_ops

    @property
    def skipped_ops(self) -> tuple[SkillOperation, ...]:
        return self.skill_result.skipped_ops

    @property
    def failed_ops(self) -> tuple[SkillOperation, ...]:
        return self.skill_result.failed_ops

    @property
    def rolled_back_ops(self) -> tuple[SkillOperation, ...]:
        return self.skill_result.rolled_back_ops

    @property
    def recovery_required(self) -> bool:
        return self.skill_result.recovery_required

    @property
    def content_changes(self) -> int:
        return self.skill_result.content_changes

    @property
    def state_only_changes(self) -> int:
        return self.skill_result.state_only_changes


def build_project_sync_batch(
    workspace_root: Path,
    home: Path,
    project_name: str,
    data: Mapping[str, Any],
    *,
    explicit_path: Path | None = None,
    force: bool = False,
    register_explicit_path: bool = True,
    append_fn: Any = None,
) -> ProjectSyncBatch:
    """Evaluate preflight checks and construct a deterministic ProjectSyncBatch.

    *register_explicit_path* controls whether a CandidatePathCAS entry is built to
    write the explicit path into agent.toml.  Set to False when the caller must not
    persistently register the path (e.g. Project.prepare() which honors INV-API-05).
    """
    binding = resolve_project_binding(dict(data), home)
    active_checkouts: list[Path] = []
    offline_checkouts: list[Path] = [e.resolved_path for e in binding.offline_entries]

    if explicit_path is not None:
        active_checkouts = [explicit_path]
    else:
        active_checkouts = [e.resolved_path for e in binding.active_entries]

    errors: list[str] = []
    for checkout in active_checkouts:
        v_err = project_sync_validation_error(
            workspace_root, project_name, checkout, home
        )
        if v_err and v_err not in errors:
            errors.append(v_err)

    skills = [str(s) for s in data.get("skills", [])]
    memory_files = [str(m) for m in data.get("memory", [])]
    sync_mode = str(data.get("sync_mode", "link")).lower()

    # Preflight memory sources and git conflict markers
    proj_mem_source = workspace_root / "projects" / project_name / "memory"
    if not proj_mem_source.exists():
        proj_mem_source = workspace_root / "memory" / project_name

    for skill_name in skills:
        skill_source = workspace_root / "skills" / skill_name
        if not skill_source.is_dir():
            errors.append(f"Project skill source does not exist: {skill_source}")

    resource_paths: list[Path] = []
    agent_toml = workspace_root / "projects" / project_name / "agent.toml"
    if agent_toml.is_file():
        resource_paths.append(agent_toml)
    project_instructions = workspace_root / "projects" / project_name / "AGENTS.md"
    if project_instructions.is_file():
        resource_paths.append(project_instructions)
    for skill_name in skills:
        skill_dir = workspace_root / "skills" / skill_name
        if skill_dir.is_dir():
            resource_paths.append(skill_dir)
    if proj_mem_source.is_dir():
        notes_dir = proj_mem_source / "notes"
        if notes_dir.is_dir():
            resource_paths.append(notes_dir)
    for mem_file in memory_files:
        mem_p = workspace_root / "memory" / mem_file
        if mem_p.exists():
            resource_paths.append(mem_p)

    errors.extend(collect_resource_conflicts(resource_paths, home))

    # CAS registration planning for explicit path (only when persistence is permitted)
    config_cas: CandidatePathCAS | None = None
    if explicit_path is not None and register_explicit_path and agent_toml.is_file():
        try:
            pre_bytes = agent_toml.read_bytes()
            pre_hash = hashlib.sha256(pre_bytes).hexdigest()
            raw_to_append = safe_relative_path(explicit_path, home)
            # Check if explicit path is already covered in config
            already_configured = False
            for entry in binding.entries:
                if entry.resolved_path.resolve(strict=False) == explicit_path.resolve(
                    strict=False
                ):
                    already_configured = True
                    break

            if already_configured:
                config_cas = CandidatePathCAS(
                    config_path=agent_toml,
                    pre_image_bytes=pre_bytes,
                    pre_image_hash=pre_hash,
                    post_image_bytes=pre_bytes,
                    post_image_hash=pre_hash,
                    is_noop=True,
                )
            else:
                # Simulate appending candidate path
                import tempfile

                with tempfile.NamedTemporaryFile(mode="wb", delete=False) as tmp_f:
                    tmp_f.write(pre_bytes)
                    tmp_path = Path(tmp_f.name)
                fn = (
                    append_fn
                    if append_fn is not None
                    else append_candidate_path_to_config
                )
                try:
                    fn(tmp_path, raw_to_append, home)
                    post_bytes = tmp_path.read_bytes()
                    post_hash = hashlib.sha256(post_bytes).hexdigest()
                    config_cas = CandidatePathCAS(
                        config_path=agent_toml,
                        pre_image_bytes=pre_bytes,
                        pre_image_hash=pre_hash,
                        post_image_bytes=post_bytes,
                        post_image_hash=post_hash,
                        is_noop=False,
                    )
                except Exception as exc:
                    errors.append(
                        f"Failed to save codebase path for project '{project_name}': {exc}\n"
                        f"Please configure the codebase path for project '{project_name}'."
                    )
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink()
        except Exception as exc:
            errors.append(
                f"Failed to save codebase path for project '{project_name}': {exc}\n"
                f"Please configure the codebase path for project '{project_name}'."
            )

    # Inspect skills and build operations across all checkouts
    all_operations: list[SkillOperation] = []
    extra_findings: list[str] = list(errors)

    selected_skill_set = set(skills)

    for checkout in active_checkouts:
        agents_skills_dir = checkout / ".agents" / "skills"

        # Case collision detection
        colliding = check_case_collision(skills, agents_skills_dir)
        if colliding:
            name1, name2 = colliding
            collision_msg = (
                f"Case collision detected between skills '{name1}' and '{name2}' "
                f"on case-insensitive filesystem at {agents_skills_dir}"
            )
            extra_findings.append(collision_msg)

        # Enumerate union of selected skills and existing runtime entries/state
        state_doc, _ = load_project_skill_state(
            home, workspace_root, project_name, checkout
        )
        known_skill_names = set(skills)
        if state_doc:
            known_skill_names.update(state_doc.records.keys())
        if agents_skills_dir.is_dir():
            for child in agents_skills_dir.iterdir():
                known_skill_names.add(child.name)

        for s_name in sorted(known_skill_names):
            target = SkillTarget(
                workspace_root=workspace_root,
                workspace_id=workspace_root.name,
                project_name=project_name,
                physical_checkout=checkout,
                skill_name=s_name,
                target_path=agents_skills_dir / s_name,
            )
            desired_mode = sync_mode if s_name in selected_skill_set else "absent"
            observed, desired = inspect_skill_target(target, desired_mode, home)
            op = plan_single_skill(
                target, desired, observed, force=force, is_offline=False
            )
            all_operations.append(op)

    # Add offline checkout operations
    for offline_co in offline_checkouts:
        agents_skills_dir = offline_co / ".agents" / "skills"
        for s_name in sorted(skills):
            target = SkillTarget(
                workspace_root=workspace_root,
                workspace_id=workspace_root.name,
                project_name=project_name,
                physical_checkout=offline_co,
                skill_name=s_name,
                target_path=agents_skills_dir / s_name,
            )
            desired = DesiredSkill(
                skill_name=s_name,
                mode=sync_mode,
                canonical_path=workspace_root / "skills" / s_name,
            )
            observed = ObservedSkill(target=target, entry_type="missing")
            op = plan_single_skill(
                target, desired, observed, force=False, is_offline=True
            )
            all_operations.append(op)

    plan = build_skill_plan(
        workspace_root,
        project_name,
        all_operations,
        config_cas=config_cas,
        extra_findings=extra_findings,
    )

    instruction_plan: InstructionPlan | None = None
    if project_instructions.is_file():
        inst_batch = build_project_instruction_batch(
            workspace_root,
            project_name,
            checkout=active_checkouts,
            home=home,
            offline_checkouts=offline_checkouts,
        )
        instruction_plan = plan_instructions(inst_batch, home)
        for conflict_op in instruction_plan.conflicts:
            extra_findings.append(conflict_op.finding or conflict_op.reason)

    mem_batch = build_project_memory_batch(
        workspace_root,
        project_name,
        data,
        active_checkouts=active_checkouts,
        offline_checkouts=offline_checkouts,
    )
    memory_plan = plan_project_memory(mem_batch)
    for conflict_op in memory_plan.conflicts:
        finding = conflict_op.finding or conflict_op.reason
        if finding and finding not in extra_findings:
            extra_findings.append(finding)

    can_apply = (
        plan.can_apply
        and (instruction_plan is None or instruction_plan.can_apply)
        and memory_plan.can_apply
        and not extra_findings
    )

    return ProjectSyncBatch(
        workspace_root=workspace_root,
        project_name=project_name,
        active_checkouts=tuple(active_checkouts),
        offline_checkouts=tuple(offline_checkouts),
        skill_plan=plan,
        preflight_findings=tuple(extra_findings),
        can_apply=can_apply,
        config_cas=config_cas,
        instruction_plan=instruction_plan,
        memory_plan=memory_plan,
    )


def apply_project_sync_batch(
    batch: ProjectSyncBatch,
    data: Mapping[str, Any],
    home: Path,
    *,
    dry_run: bool = False,
) -> ProjectSyncExecutionResult:
    """Execute skill plan and compatible instruction/memory synchronizations."""
    if not batch.can_apply and not dry_run:
        err_msg = (
            batch.preflight_findings[0]
            if batch.preflight_findings
            else "Project sync batch contains unresolved conflicts; cannot apply."
        )
        skill_failed_ops = (
            tuple(op for op in batch.skill_plan.operations if op.finding)
            if not batch.skill_plan.can_apply
            else ()
        )
        skill_err = err_msg if not batch.skill_plan.can_apply else None
        skill_res = SkillExecutionResult(
            applied_ops=(),
            skipped_ops=(),
            failed_ops=skill_failed_ops,
            rolled_back_ops=(),
            recovery_required=False,
            content_changes=0,
            state_only_changes=0,
            error_message=skill_err,
        )
        instruction_res = None
        if batch.instruction_plan is not None:
            instruction_res = InstructionExecutionResult(
                operations=batch.instruction_plan.operations,
                success=batch.instruction_plan.can_apply,
                conflict_count=len(batch.instruction_plan.conflicts),
                error_message=err_msg if not batch.instruction_plan.can_apply else None,
            )
        memory_res = None
        if batch.memory_plan is not None:
            memory_res = MemoryExecutionResult(
                operations=batch.memory_plan.operations,
                success=batch.memory_plan.can_apply,
                conflict_count=len(batch.memory_plan.conflicts),
                error_message=err_msg if not batch.memory_plan.can_apply else None,
            )
        return ProjectSyncExecutionResult(
            skill_result=skill_res,
            instruction_result=instruction_res,
            memory_result=memory_res,
            error_message=err_msg,
        )

    # 1. Apply SkillPlan
    skill_result = execute_skill_plan(batch.skill_plan, home, dry_run=dry_run)
    if not skill_result.is_success:
        return ProjectSyncExecutionResult(
            skill_result=skill_result,
            instruction_result=None,
            memory_result=None,
            error_message=skill_result.error_message,
        )

    # 2. Apply InstructionPlan
    instruction_result: InstructionExecutionResult | None = None
    if batch.instruction_plan is not None:
        instruction_result = execute_instruction_plan(
            batch.instruction_plan, home, dry_run=dry_run
        )
        if not instruction_result.success:
            return ProjectSyncExecutionResult(
                skill_result=skill_result,
                instruction_result=instruction_result,
                memory_result=None,
                error_message=instruction_result.error_message
                or "Failed to synchronize project instructions",
            )

    # 3. Apply MemoryPlan
    memory_result: MemoryExecutionResult | None = None
    if batch.memory_plan is not None:
        memory_result = execute_memory_plan(batch.memory_plan, dry_run=dry_run)
        if not memory_result.success:
            return ProjectSyncExecutionResult(
                skill_result=skill_result,
                instruction_result=instruction_result,
                memory_result=memory_result,
                error_message=memory_result.error_message
                or "Failed to synchronize project memory",
            )

    return ProjectSyncExecutionResult(
        skill_result=skill_result,
        instruction_result=instruction_result,
        memory_result=memory_result,
    )


def sync_project(
    aikito_dir: Path,
    home: Path,
    project_name: str,
    *,
    project_path: Path | str | None = None,
    dry_run: bool = False,
    force: bool = False,
    append_fn: Any = None,
) -> bool:
    """Coordinate preflight, display, and execution for 'aikito sync project' command."""
    proj_dir = aikito_dir / "projects" / project_name
    agent_toml_path = proj_dir / "agent.toml"
    if not agent_toml_path.is_file():
        print(
            f"[ERROR] Project '{project_name}' not found at {proj_dir}.",
            file=sys.stderr,
        )
        return False

    try:
        data = tomllib.loads(agent_toml_path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        print(
            f"[ERROR] Failed to read configuration for project '{project_name}': {exc}",
            file=sys.stderr,
        )
        return False

    binding = resolve_project_binding(data, home)
    target_path: Path | None = None

    if project_path:
        target_path = Path(project_path).expanduser().resolve()
        if not target_path.exists():
            print(
                f"[ERROR] Project path does not exist: {target_path}", file=sys.stderr
            )
            return False
        if not target_path.is_dir():
            print(
                f"[ERROR] Project path is not a directory: {target_path}",
                file=sys.stderr,
            )
            return False
    else:
        if not binding.entries:
            print(
                f"[ERROR] Project path not provided and no saved path found for project '{project_name}'.\n"
                f"Usage: aikito sync project {project_name} <project_path>",
                file=sys.stderr,
            )
            return False
        if not binding.active_entries:
            offline_list = "\n".join(
                f"  - [{e.label}] {e.resolved_path}" for e in binding.offline_entries
            )
            print(
                f"[ERROR] None of the configured paths for project '{project_name}' "
                f"exist on this machine:\n"
                f"{offline_list}\n\n"
                f"Please clone/create the project directory or specify the path explicitly:\n"
                f"  aikito sync project {project_name} <project_path>",
                file=sys.stderr,
            )
            return False

    batch = build_project_sync_batch(
        aikito_dir,
        home,
        project_name,
        data,
        explicit_path=target_path,
        force=force,
        append_fn=append_fn,
    )

    if not batch.can_apply:
        for err in batch.preflight_findings:
            print(f"[ERROR] {err}", file=sys.stderr)
        for op in batch.skill_plan.operations:
            if op.action == "CONFLICT":
                print(f"[CONFLICT] {op.reason}", file=sys.stderr)
            elif op.finding and op.finding not in batch.preflight_findings:
                print(f"[ERROR] {op.finding}", file=sys.stderr)
        if batch.instruction_plan is not None:
            for op in batch.instruction_plan.operations:
                if op.action == "CONFLICT":
                    prefix = (
                        f"[CONFLICT] {op.resource_name} instructions:"
                        if op.resource_name
                        else "[CONFLICT]"
                    )
                    print(f"{prefix} {op.reason}", file=sys.stderr)
                elif op.finding and op.finding not in batch.preflight_findings:
                    print(f"[ERROR] {op.finding}", file=sys.stderr)
        if batch.memory_plan is not None:
            for op in batch.memory_plan.operations:
                if op.action == "CONFLICT":
                    prefix = (
                        f"[CONFLICT] {op.resource_name} memory:"
                        if op.resource_name
                        else "[CONFLICT]"
                    )
                    print(f"{prefix} {op.reason}", file=sys.stderr)
                elif op.finding and op.finding not in batch.preflight_findings:
                    print(f"[ERROR] {op.finding}", file=sys.stderr)
        print("[ERROR] Project synchronization aborted.", file=sys.stderr)
        return False

    operation = "Previewing sync for" if dry_run else "Syncing"
    sync_mode = str(data.get("sync_mode", "link")).lower()

    if target_path is not None:
        print(f"[INFO] {operation} project '{project_name}' (mode: {sync_mode})")
        _render_batch_ops(batch, target_path, dry_run)
        if not dry_run and batch.config_cas and not batch.config_cas.is_noop:
            print(f"[INFO] Registered codebase path for project '{project_name}'.")

        res = apply_project_sync_batch(batch, data, home, dry_run=dry_run)
        if not res.is_success:
            if res.error_message:
                print(f"[ERROR] {res.error_message}", file=sys.stderr)
            return False
        result = "sync preview completed" if dry_run else "synced successfully"
        print(f"[SUCCESS] Project '{project_name}' {result} at {target_path}.")
        return True

    # Active paths
    active_entries = binding.active_entries
    multi = len(active_entries) > 1
    if multi:
        print(
            f"[INFO] {operation} project '{project_name}' across {len(active_entries)} active paths:"
        )

    for idx, entry in enumerate(active_entries, start=1):
        if multi:
            print(
                f"\n[INFO] === [{idx}/{len(active_entries)}] Path [{entry.label}]: {entry.resolved_path} ==="
            )
        else:
            print(f"[INFO] {operation} project '{project_name}' (mode: {sync_mode})")
        _render_batch_ops(batch, entry.resolved_path, dry_run)

    res = apply_project_sync_batch(batch, data, home, dry_run=dry_run)
    if not res.is_success:
        if res.error_message:
            print(f"[ERROR] {res.error_message}", file=sys.stderr)
        return False

    result = "sync preview completed" if dry_run else "synced successfully"
    print(f"[SUCCESS] Project '{project_name}' {result}.")
    return True


def _render_batch_ops(
    batch: ProjectSyncBatch, checkout_path: Path, dry_run: bool
) -> None:
    """Print terminal lines for skill operations matching target checkout."""
    for auth in batch.skill_plan.authorizations:
        print(f"[AUTH] {auth}")

    for op in batch.skill_plan.operations:
        if op.target.physical_checkout.resolve(strict=False) != checkout_path.resolve(
            strict=False
        ):
            continue
        canonical_source = batch.workspace_root / "skills" / op.target.skill_name
        target = op.target.target_path
        if op.action == "UNLINK":
            action = (
                "Would remove stale managed item"
                if dry_run
                else "Removed stale managed item"
            )
            tag = "[DRY RUN CLEANUP]" if dry_run else "[CLEANUP]"
            print(f"{tag} {action}: {target}")
        elif op.action in ("CREATE", "UPDATE"):
            if op.desired_representation == "link":
                if dry_run:
                    print(f"[DRY RUN LINK] {canonical_source} -> {target}")
                else:
                    print(f"[LINK] {target} -> {canonical_source}")
            else:
                if dry_run:
                    print(f"[DRY RUN COPY] {canonical_source} -> {target}")
                else:
                    print(f"[COPY DIR] {canonical_source} -> {target}")
        elif op.action in ("RECONCILE_STATE", "CLAIM_STATE", "REACTIVATE_STATE"):
            if not dry_run:
                print(f"[INFO] {op.reason}")
        elif op.action == "DEACTIVATE_STATE":
            print(f"[INFO] Preserving project-owned skill: {target}")
        elif op.action == "NOOP":
            if op.rule_id in ("INV-TR-15", "INV-TR-17"):
                print(f"[INFO] Preserving project-owned skill: {target}")
            elif op.rule_id == "INV-TR-11":
                print(
                    f"[INFO] Skill '{op.target.skill_name}' at {target} is registered but unmanaged "
                    f"(matches canonical without state). Run 'aikito sync project {op.target.project_name} {op.target.physical_checkout} --force' to claim management."
                )
            elif op.rule_id == "INV-TR-18":
                print(
                    f"[INFO] Skill '{op.target.skill_name}' at {target} is registered but inactive "
                    f"(matches canonical). Run 'aikito sync project {op.target.project_name} {op.target.physical_checkout} --force' to reactivate management."
                )
