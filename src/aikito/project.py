"""Inspect copied project skills without mutating canonical or runtime content."""

import difflib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .instructions import build_project_instruction_batch, plan_instructions
from .memory_runtime import build_project_memory_batch, plan_project_memory
from .compat import resolve_symlink_target
from .project_config import (
    candidate_path_views as _candidate_path_views,
    joined_candidate_paths as _joined_candidate_paths,
    resolve_project_binding,
)
from .skill_plan import SkillOperation, SkillTarget, plan_single_skill
from .skill_runtime import ObservedSkill, inspect_skill_target


@dataclass(frozen=True)
class ProjectSkillState:
    project_name: str
    skill_name: str
    canonical_path: Path
    runtime_path: Path
    status: str
    reason: str = ""


@dataclass(frozen=True)
class RuntimeCleanupPlan:
    cleanup: tuple[Path, ...]
    conflicts: tuple[Path, ...]


@dataclass(frozen=True)
class ProjectResourceDetail:
    resource: str
    canonical_path: Path
    runtime_path: Path | None
    status: str
    detail: str = ""


@dataclass(frozen=True)
class ProjectSummary:
    name: str
    path: str
    sync_mode: str
    instructions_status: str
    skills_count: int
    memory_notes_count: int
    runtime_status: str
    config_path: Path
    description: str = ""
    skill_names: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    details: tuple[ProjectResourceDetail, ...] = ()
    instructions_notice: str = ""
    skills_notice: str = ""
    error: str = ""
    active_paths: tuple[tuple[str, str], ...] = ()
    offline_paths: tuple[tuple[str, str], ...] = ()
    candidate_paths: tuple[tuple[str, str, bool], ...] = ()

    @property
    def has_conflict(self) -> bool:
        if self.runtime_status == "CONFLICT":
            return True
        return any(detail.status == "CONFLICT" for detail in self.details)

    @property
    def has_copied_skill_drift(self) -> bool:
        if self.sync_mode != "copy":
            return False
        for detail in self.details:
            if detail.resource.startswith("Skills") and detail.status == "DRIFT":
                if not (detail.detail or "").startswith("Deselected managed skill"):
                    return True
        return False

    @property
    def is_sync_fixable(self) -> bool:
        if self.runtime_status not in ("MISSING", "DRIFT"):
            return False
        if self.has_conflict or self.has_copied_skill_drift:
            return False
        return True

    @property
    def fix_action(self) -> str:
        """Command recommendation for doctor or CLI diagnosis."""
        if self.has_conflict:
            return f"aikito show project {self.name}"
        if self.has_copied_skill_drift:
            return "aikito diff"
        if self.is_sync_fixable:
            return f"aikito sync project {self.name}"
        return ""

    @property
    def fix_hint(self) -> str:
        """User-facing resolution instructions for project details view."""
        if self.has_conflict:
            return "Remove unmanaged files from .agents/ or reconcile conflicting resources"
        if self.has_copied_skill_drift:
            return (
                f"Run 'aikito diff' to review changes, then 'aikito sync "
                f"project {self.name} --force' after review"
            )
        if self.is_sync_fixable:
            return (
                f"Run 'aikito sync project {self.name}' (or 'aikito sync') "
                "to reconcile runtime"
            )
        return ""


def _link_status(target: Path, expected: Path) -> str:
    if target.is_symlink():
        return (
            "OK"
            if target.resolve(strict=False) == expected.resolve(strict=False)
            else "CONFLICT"
        )
    return "CONFLICT" if target.exists() else "MISSING"


def _link_issue(target: Path, expected: Path, status: str) -> str:
    if status == "MISSING":
        return f"Missing {target}"
    if status == "CONFLICT":
        return f"Expected {target} to link to {expected}"
    return ""


def _file_inventory(root: Path) -> tuple[dict[str, Path], str | None]:
    files: dict[str, Path] = {}
    if not root.is_dir():
        return files, f"Skill path is not a directory: {root}"

    for item in sorted(root.rglob("*")):
        relative = item.relative_to(root).as_posix()
        if item.is_symlink():
            return {}, f"Symbolic links are not supported inside copied skills: {item}"
        if item.is_file():
            files[relative] = item
        elif not item.is_dir():
            return {}, f"Unsupported filesystem entry: {item}"
    return files, None


# NOTE: Retired from project skill classification in Phase 3 (unified under inspect_skill_target).
# Remaining callers: plan_runtime_cleanup (memory) and find_selected_runtime_conflicts (memory).
# Scheduled for removal in Phase 5 (project memory migration).
def _directories_match(canonical: Path, runtime: Path) -> tuple[bool, str | None]:
    canonical_files, canonical_error = _file_inventory(canonical)
    if canonical_error:
        return False, canonical_error
    runtime_files, runtime_error = _file_inventory(runtime)
    if runtime_error:
        return False, runtime_error
    if canonical_files.keys() != runtime_files.keys():
        return False, None
    try:
        return all(
            canonical_files[name].read_bytes() == runtime_files[name].read_bytes()
            for name in canonical_files
        ), None
    except OSError as exc:
        return False, str(exc)


def _symlink_points_within(path: Path, expected_targets: tuple[Path, ...]) -> bool:
    """Check if a symlink precisely resolves to one of the expected canonical target paths."""
    if not path.is_symlink():
        return False
    target = resolve_symlink_target(path)
    fallback = path.resolve(strict=False)
    for expected in expected_targets:
        try:
            resolved_expected = expected.resolve(strict=False)
            if target == resolved_expected or fallback == resolved_expected:
                return True
        except (ValueError, OSError):
            continue
    return False


def plan_runtime_cleanup(
    runtime_dir: Path,
    selected_names: set[str],
    canonical_roots: tuple[Path, ...],
    *,
    allow_matching_copies: bool,
) -> RuntimeCleanupPlan:
    """Classify stale runtime entries by provable Aikito ownership."""
    cleanup: list[Path] = []
    conflicts: list[Path] = []
    if not runtime_dir.is_dir():
        return RuntimeCleanupPlan((), ())

    for item in sorted(runtime_dir.iterdir()):
        if item.name in selected_names:
            continue
        expected_targets = tuple(root / item.name for root in canonical_roots)
        owned = _symlink_points_within(item, expected_targets)
        if not owned and allow_matching_copies and item.is_dir():
            canonical = canonical_roots[0] / item.name
            if canonical.is_dir():
                matches, error = _directories_match(canonical, item)
                owned = error is None and matches
        (cleanup if owned else conflicts).append(item)
    return RuntimeCleanupPlan(tuple(cleanup), tuple(conflicts))


def find_selected_runtime_conflicts(
    runtime_dir: Path,
    selected_names: set[str],
    canonical_root: Path,
    *,
    allow_drifted_copies: bool,
) -> tuple[Path, ...]:
    """Return selected targets that cannot be proven safe to replace or inspect."""
    conflicts: list[Path] = []
    for name in sorted(selected_names):
        target = runtime_dir / name
        if not target.exists() and not target.is_symlink():
            continue
        if _symlink_points_within(target, (canonical_root / name,)):
            continue
        if target.is_dir():
            if allow_drifted_copies:
                continue
            canonical = canonical_root / name
            if canonical.is_dir():
                matches, error = _directories_match(canonical, target)
                if error is None and matches:
                    continue
        conflicts.append(target)
    return tuple(conflicts)


def _aggregate_runtime_status(statuses: list[str]) -> str:
    for status in ("CONFLICT", "DRIFT", "MISSING"):
        if status in statuses:
            return status
    return "OK"


def collect_project_summaries(aikito_dir: Path, home: Path) -> list[ProjectSummary]:
    """Collect canonical resources and runtime health for registered projects."""
    summaries: list[ProjectSummary] = []
    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        return summaries

    copied_skill_states = {
        (state.project_name, state.skill_name, str(state.runtime_path)): state
        for state in collect_project_skill_states(aikito_dir, home)
    }
    for project_dir in sorted(projects_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.name.startswith("."):
            continue
        config_path = project_dir / "agent.toml"
        try:
            with open(config_path, "rb") as config_file:
                config = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError) as exc:
            summaries.append(
                ProjectSummary(
                    name=project_dir.name,
                    path="-",
                    sync_mode="-",
                    instructions_status="MISSING",
                    skills_count=0,
                    memory_notes_count=0,
                    runtime_status="INVALID CONFIG",
                    config_path=config_path,
                    error=str(exc),
                )
            )
            continue

        binding = resolve_project_binding(config, home)
        candidate_paths = _candidate_path_views(binding, home)
        joined_paths = _joined_candidate_paths(candidate_paths)
        description = config.get("description", "")
        if not isinstance(description, str):
            summaries.append(
                ProjectSummary(
                    name=project_dir.name,
                    path=joined_paths,
                    sync_mode=str(config.get("sync_mode", "link")).lower(),
                    instructions_status="MISSING",
                    skills_count=0,
                    memory_notes_count=0,
                    runtime_status="INVALID CONFIG",
                    config_path=config_path,
                    error="Project description must be a string",
                    candidate_paths=candidate_paths,
                )
            )
            continue
        description = description.strip()
        sync_mode = str(config.get("sync_mode", "link")).lower()
        skill_names = tuple(sorted(str(name) for name in config.get("skills", [])))
        memory_refs = tuple(sorted(str(name) for name in config.get("memory", [])))
        instructions = project_dir / "AGENTS.md"
        if not instructions.is_file():
            instructions_status = "MISSING"
        elif instructions.read_text(encoding="utf-8", errors="replace").strip():
            instructions_status = "OK"
        else:
            instructions_status = "EMPTY"

        notes_dir = project_dir / "memory" / "notes"
        memory_notes_count = (
            sum(1 for note in notes_dir.rglob("*.md") if note.is_file())
            if notes_dir.is_dir()
            else 0
        )

        active_paths = tuple(
            (label, path) for label, path, exists in candidate_paths if exists
        )
        offline_paths = tuple(
            (label, path) for label, path, exists in candidate_paths if not exists
        )

        details: list[ProjectResourceDetail] = []
        instructions_notices: list[str] = []
        skills_notices: list[str] = []
        if not binding.entries:
            runtime_status = "UNBOUND"
        elif not binding.active_entries:
            runtime_status = "OFFLINE"
        else:
            multi_active = len(binding.active_entries) > 1
            active_statuses: list[str] = []
            for active_entry in binding.active_entries:
                project_path = active_entry.resolved_path
                p_tag = f" [{active_entry.label}]" if multi_active else ""
                agents_dir = project_path / ".agents"
                statuses: list[str] = []
                try:
                    inst_batch = build_project_instruction_batch(
                        aikito_dir,
                        project_dir.name,
                        checkout=project_path,
                        home=home,
                    )
                    inst_plan = plan_instructions(inst_batch, home)
                except Exception:
                    inst_plan = None

                if inst_plan is not None:
                    if instructions_status == "OK":
                        for op in inst_plan.operations:
                            if op.action in ("NOOP", "SHARED_PATH"):
                                status = "OK"
                                issue = ""
                            elif op.action == "CREATE":
                                status = "MISSING"
                                issue = f"Missing {op.target_path}"
                            elif op.action == "CONFLICT":
                                status = "CONFLICT"
                                issue = (
                                    op.finding
                                    or f"Expected {op.target_path} to link to {instructions}"
                                )
                            elif op.action == "SKIP":
                                status = "SKIP"
                                issue = op.reason or ""
                            else:
                                status = op.action
                                issue = op.reason or ""
                            statuses.append(status)
                            details.append(
                                ProjectResourceDetail(
                                    f"Instructions ({op.resource_name}){p_tag}",
                                    instructions,
                                    op.target_path,
                                    status,
                                    issue,
                                )
                            )
                    elif instructions_status == "EMPTY":
                        for op in inst_plan.operations:
                            if op.action == "UNLINK":
                                statuses.append("DRIFT")
                                details.append(
                                    ProjectResourceDetail(
                                        f"Instructions ({op.resource_name}){p_tag}",
                                        instructions,
                                        op.target_path,
                                        "DRIFT",
                                        f"Empty canonical instructions no longer require {op.target_path}",
                                    )
                                )
                            elif (
                                op.action == "NOOP"
                                and op.expected_representation == "file"
                                and op.target_path.name == "AGENTS.md"
                            ):
                                tag_str = (
                                    f" in {active_entry.label}" if multi_active else ""
                                )
                                instructions_notices.append(
                                    f"Project-owned AGENTS.md detected{tag_str}: {op.target_path} "
                                    "(not managed because canonical instructions are empty)"
                                )

                skills_runtime = agents_dir / "skills"
                selected_skills = set(skill_names)
                selected_conflicts = find_selected_runtime_conflicts(
                    skills_runtime,
                    selected_skills,
                    aikito_dir / "skills",
                    allow_drifted_copies=sync_mode == "copy",
                )
                cleanup_plan = plan_runtime_cleanup(
                    skills_runtime,
                    selected_skills,
                    (aikito_dir / "skills",),
                    allow_matching_copies=False,
                )
                skill_issues: list[str] = []
                if cleanup_plan.conflicts:
                    tag_str = f" [{active_entry.label}]" if multi_active else ""
                    skills_notices.append(
                        f"Project-owned skills detected{tag_str}: "
                        + ", ".join(path.name for path in cleanup_plan.conflicts)
                    )
                if selected_conflicts:
                    skills_status = "CONFLICT"
                    skill_issues.extend(
                        f"Selected skill conflicts with project-owned entry: {path}"
                        for path in selected_conflicts
                    )
                elif cleanup_plan.cleanup:
                    skills_status = "DRIFT"
                    skill_issues.extend(
                        f"Deselected managed skill: {path}"
                        for path in cleanup_plan.cleanup
                    )
                else:
                    skill_statuses: list[str] = []
                    for skill_name in skill_names:
                        canonical = aikito_dir / "skills" / skill_name
                        runtime = skills_runtime / skill_name
                        if sync_mode == "copy":
                            state = copied_skill_states.get(
                                (project_dir.name, skill_name, str(runtime))
                            )
                            status = state.status if state else "MISSING"
                            skill_statuses.append(status)
                            if status != "OK":
                                reason = (
                                    state.reason if state and state.reason else status
                                )
                                skill_issues.append(f"{skill_name}: {reason}")
                        else:
                            status = _link_status(runtime, canonical)
                            skill_statuses.append(status)
                            issue = _link_issue(runtime, canonical, status)
                            if issue:
                                skill_issues.append(f"{skill_name}: {issue}")
                    skills_status = _aggregate_runtime_status(skill_statuses)
                statuses.append(skills_status)
                details.append(
                    ProjectResourceDetail(
                        f"Skills{p_tag}",
                        aikito_dir / "skills",
                        skills_runtime,
                        skills_status,
                        "; ".join(skill_issues),
                    )
                )

                project_memory = project_dir / "memory"
                memory_runtime = agents_dir / "memory"
                mem_batch = build_project_memory_batch(
                    aikito_dir,
                    project_dir.name,
                    {"memory": memory_refs},
                    active_checkouts=[active_entry.resolved_path],
                )
                mem_plan = plan_project_memory(mem_batch)

                memory_statuses: list[str] = []
                memory_issues: list[str] = []
                for op in mem_plan.operations:
                    prefix = f"{op.resource_name}: " if op.resource_name else ""
                    if op.action in ("NOOP", "SHARED_PATH"):
                        memory_statuses.append("OK")
                    elif op.action == "CREATE":
                        memory_statuses.append("MISSING")
                        memory_issues.append(f"{prefix}target is missing")
                    elif op.action == "UNLINK":
                        memory_statuses.append("DRIFT")
                        memory_issues.append(f"Stale managed memory: {op.target_path}")
                    elif op.action == "CONFLICT":
                        memory_statuses.append("CONFLICT")
                        memory_issues.append(f"{prefix}{op.finding or op.reason}")
                    elif op.action == "SKIP":
                        memory_statuses.append("SKIP")

                memory_status = (
                    _aggregate_runtime_status(memory_statuses)
                    if memory_statuses
                    else "OK"
                )
                statuses.append(memory_status)
                details.append(
                    ProjectResourceDetail(
                        f"Memory{p_tag}",
                        project_memory,
                        memory_runtime,
                        memory_status,
                        "; ".join(memory_issues),
                    )
                )
                active_statuses.append(_aggregate_runtime_status(statuses))
            runtime_status = _aggregate_runtime_status(active_statuses)

        instructions_notice = "\n".join(instructions_notices)
        skills_notice = "\n".join(skills_notices)

        summaries.append(
            ProjectSummary(
                name=project_dir.name,
                path=joined_paths,
                sync_mode=sync_mode,
                instructions_status=instructions_status,
                skills_count=len(skill_names),
                memory_notes_count=memory_notes_count,
                runtime_status=runtime_status,
                config_path=config_path,
                description=description,
                skill_names=skill_names,
                memory_refs=memory_refs,
                details=tuple(details),
                instructions_notice=instructions_notice,
                skills_notice=skills_notice,
                active_paths=active_paths,
                offline_paths=offline_paths,
                candidate_paths=candidate_paths,
            )
        )
    return summaries


def map_skill_operation_to_project_state(
    op: SkillOperation,
    observed: ObservedSkill,
) -> tuple[str, str]:
    """Map SkillOperation and ObservedSkill to (status, reason) for ProjectSkillState.

    Explicit mapping table:
    - NOOP (INV-TR-07, 11, 18) -> ("OK", "")
    - RECONCILE_STATE (INV-TR-10) -> ("OK", "")
    - CREATE (INV-TR-01) -> ("MISSING", "Runtime skill is missing")
    - UPDATE (INV-TR-08) -> ("UPDATE", "Canonical skill updated upstream; safe to sync without --force")
    - UPDATE (INV-TR-20) -> ("UPDATE", op.reason)
    - CONFLICT (INV-TR-09, 12, 19) -> ("DRIFT", "Copied project skill drifted from workspace skill")
    - CONFLICT (INV-TR-14 with missing source) -> ("MISSING", "Canonical skill is missing")
    - CONFLICT (INV-TR-13 with unsupported entry) -> ("CONFLICT", "Runtime skill is not a directory")
    - Other CONFLICT -> ("CONFLICT", op.reason)
    """
    if op.action in ("NOOP", "RECONCILE_STATE"):
        return "OK", ""
    if op.action == "CREATE":
        return "MISSING", "Runtime skill is missing"
    if op.rule_id == "INV-TR-08":
        return (
            "UPDATE",
            "Canonical skill updated upstream; safe to sync without --force",
        )
    if op.rule_id == "INV-TR-20" and op.action == "UPDATE":
        return "UPDATE", op.reason
    if op.rule_id in ("INV-TR-09", "INV-TR-12", "INV-TR-19"):
        return "DRIFT", "Copied project skill drifted from workspace skill"
    if op.rule_id == "INV-TR-14":
        if observed.canonical_error and (
            "does not exist" in observed.canonical_error
            or "missing" in observed.canonical_error.lower()
        ):
            return "MISSING", "Canonical skill is missing"
        return "CONFLICT", op.reason
    if op.rule_id == "INV-TR-13":
        if observed.entry_type == "unsupported":
            return "CONFLICT", "Runtime skill is not a directory"
        return "CONFLICT", op.reason
    if op.action == "CONFLICT":
        return "CONFLICT", op.reason
    return op.action, op.reason


def classify_project_skill_state(
    aikito_dir: Path,
    project_name: str,
    project_path: Path | None,
    skill_name: str,
    home: Path | None = None,
) -> ProjectSkillState:
    """Classify the synchronization state of a single copied skill for a project path."""
    if home is None:
        home = Path.home()
    canonical = aikito_dir / "skills" / skill_name
    runtime = (
        project_path / ".agents" / "skills" / skill_name
        if project_path is not None
        else Path("<unbound>") / skill_name
    )
    if Path(skill_name).name != skill_name or skill_name in ("", ".", ".."):
        return ProjectSkillState(
            project_name=project_name,
            skill_name=skill_name,
            canonical_path=canonical,
            runtime_path=runtime,
            status="CONFLICT",
            reason="Skill name must be a single path component",
        )
    if project_path is None:
        return ProjectSkillState(
            project_name=project_name,
            skill_name=skill_name,
            canonical_path=canonical,
            runtime_path=runtime,
            status="CONFLICT",
            reason="Project path is not configured",
        )

    target = SkillTarget(
        workspace_root=aikito_dir,
        workspace_id=aikito_dir.name,
        project_name=project_name,
        physical_checkout=project_path,
        skill_name=skill_name,
        target_path=runtime,
    )
    observed, desired = inspect_skill_target(target, "copy", home)
    op = plan_single_skill(target, desired, observed, force=False, is_offline=False)
    status, reason = map_skill_operation_to_project_state(op, observed)

    return ProjectSkillState(
        project_name=project_name,
        skill_name=skill_name,
        canonical_path=canonical,
        runtime_path=runtime,
        status=status,
        reason=reason,
    )


def collect_single_project_skill_states(
    aikito_dir: Path,
    project_name: str,
    project_path: Path | None,
    skills: list[str],
    home: Path | None = None,
) -> list[ProjectSkillState]:
    """Classify copied runtime skills for a single project path."""
    return [
        classify_project_skill_state(
            aikito_dir, project_name, project_path, str(skill_name), home=home
        )
        for skill_name in sorted(skills)
    ]


def collect_project_skill_states(
    aikito_dir: Path, home: Path
) -> list[ProjectSkillState]:
    """Classify runtime copies for every project configured with copy mode."""
    states: list[ProjectSkillState] = []
    projects_dir = aikito_dir / "projects"
    if not projects_dir.is_dir():
        return states

    for project_dir in sorted(projects_dir.iterdir()):
        config_path = project_dir / "agent.toml"
        if not project_dir.is_dir() or not config_path.is_file():
            continue
        try:
            with open(config_path, "rb") as config_file:
                config = tomllib.load(config_file)
        except (OSError, tomllib.TOMLDecodeError):
            continue
        if str(config.get("sync_mode", "link")).lower() != "copy":
            continue

        binding = resolve_project_binding(config, home)
        if not binding.active_entries:
            continue
        skills = [str(name) for name in config.get("skills", [])]
        for entry in binding.active_entries:
            states.extend(
                collect_single_project_skill_states(
                    aikito_dir, project_dir.name, entry.resolved_path, skills, home=home
                )
            )
    return states


def _is_binary(content: bytes) -> bool:
    return b"\0" in content


def collect_project_skill_diffs(
    aikito_dir: Path, home: Path, *, project_filter: str | None = None
) -> list[tuple[str, str]]:
    """Return unified diffs for every drifted copied project skill."""
    results: list[tuple[str, str]] = []
    for state in collect_project_skill_states(aikito_dir, home):
        if project_filter is not None and state.project_name != project_filter:
            continue
        if state.status != "DRIFT":
            continue
        canonical_files, canonical_error = _file_inventory(state.canonical_path)
        runtime_files, runtime_error = _file_inventory(state.runtime_path)
        if canonical_error or runtime_error:
            continue
        for relative in sorted(canonical_files.keys() | runtime_files.keys()):
            actual_path = runtime_files.get(relative)
            expected_path = canonical_files.get(relative)
            try:
                actual = actual_path.read_bytes() if actual_path else b""
                expected = expected_path.read_bytes() if expected_path else b""
            except OSError:
                continue
            if actual == expected:
                continue
            label = (
                f"Project {state.project_name}/skill {state.skill_name} — {relative}"
            )
            actual_label = str(actual_path) if actual_path else "/dev/null"
            expected_label = str(expected_path) if expected_path else "/dev/null"
            if _is_binary(actual) or _is_binary(expected):
                diff = f"Binary files differ: {actual_label} and {expected_label}"
            else:
                diff = "".join(
                    difflib.unified_diff(
                        actual.decode("utf-8", errors="replace").splitlines(
                            keepends=True
                        ),
                        expected.decode("utf-8", errors="replace").splitlines(
                            keepends=True
                        ),
                        fromfile=f"actual: {actual_label}",
                        tofile=f"expected: {expected_label}",
                    )
                ).rstrip()
            results.append((label, diff))
    return results


def get_instructions_line_count_display(agents_md_path: Path) -> str:
    """Return physical line count string (e.g. '45L') or '-' if missing or empty."""
    try:
        if not agents_md_path.is_file():
            return "-"
        text = agents_md_path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            return "-"
        return f"{len(text.splitlines())}L"
    except Exception:
        return "-"


def format_project_path_counts(project: ProjectSummary) -> str:
    """Return 'active/total' deduplicated candidate paths count string, e.g. '2/3'."""
    candidate_paths = getattr(project, "candidate_paths", ())
    if candidate_paths:
        paths_seen: dict[str, bool] = {}
        for _label, path_str, exists in candidate_paths:
            if path_str not in paths_seen:
                paths_seen[path_str] = exists
            else:
                paths_seen[path_str] = paths_seen[path_str] or exists
        total = len(paths_seen)
        active = sum(1 for exists in paths_seen.values() if exists)
        return f"{active}/{total}"
    path_val = getattr(project, "path", "-")
    if path_val and str(path_val) != "-":
        try:
            exists = Path(path_val).exists()
            return "1/1" if exists else "0/1"
        except Exception:
            return "0/1"
    return "0/0"


def evaluate_project_health(
    project: ProjectSummary,
    memory_status: str | None = None,
) -> str:
    """Evaluate unified project health status across runtime, instructions, and memory."""
    candidate_paths = getattr(project, "candidate_paths", ())
    if candidate_paths:
        exists_count = sum(1 for _lbl, _path, exists in candidate_paths if exists)
        if exists_count == 0:
            return "-"
    elif project.runtime_status == "OFFLINE":
        return "-"

    statuses = [project.runtime_status, project.instructions_status]
    if memory_status is not None:
        statuses.append(memory_status)

    priority_order = [
        ("INVALID CONFIG", "invalid config"),
        ("CONFLICT", "conflict"),
        ("DRIFT", "drift"),
        ("MISSING", "missing"),
        ("UNBOUND", "unbound"),
    ]

    for key, reason in priority_order:
        if any(s == key or (s and s.startswith(f"{key} ")) for s in statuses):
            return f"! {reason}"

    return "OK"
