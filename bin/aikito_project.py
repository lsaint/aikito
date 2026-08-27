"""Inspect copied project skills without mutating canonical or runtime content."""

import difflib
import tomllib
from dataclasses import dataclass
from pathlib import Path

from aikito_mcp import MCPConfigError, collect_project_instruction_targets


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
    skill_names: tuple[str, ...] = ()
    memory_refs: tuple[str, ...] = ()
    details: tuple[ProjectResourceDetail, ...] = ()
    instructions_notice: str = ""
    skills_notice: str = ""
    error: str = ""


def _resolve_project_path(raw_path: object, home: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    if raw_path == "~":
        return home.resolve()
    if raw_path.startswith("~/"):
        return (home / raw_path[2:]).resolve()
    return Path(raw_path).expanduser().resolve()


def _display_path(path: Path | None, home: Path) -> str:
    if path is None:
        return "-"
    try:
        return f"~/{path.relative_to(home.resolve())}"
    except ValueError:
        return str(path)


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


def _symlink_points_within(path: Path, roots: tuple[Path, ...]) -> bool:
    if not path.is_symlink():
        return False
    target = path.resolve(strict=False)
    return any(target.is_relative_to(root.resolve()) for root in roots)


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
        owned = _symlink_points_within(item, canonical_roots)
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
        if _symlink_points_within(target, (canonical_root,)):
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
        (state.project_name, state.skill_name): state
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

        project_path = _resolve_project_path(config.get("path"), home)
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
        details: list[ProjectResourceDetail] = []
        instructions_notice = ""
        skills_notice = ""
        if project_path is None:
            runtime_status = "UNBOUND"
        elif not project_path.is_dir():
            runtime_status = "PATH MISSING"
        else:
            agents_dir = project_path / ".agents"
            statuses: list[str] = []
            try:
                instruction_targets = collect_project_instruction_targets(
                    aikito_dir, project_path, home
                )
            except MCPConfigError:
                # An unreadable agent registry is reported by doctor, not here.
                instruction_targets = {}
            if instructions_status == "OK":
                for target, agent_names in instruction_targets.items():
                    status = _link_status(target, instructions)
                    statuses.append(status)
                    details.append(
                        ProjectResourceDetail(
                            f"Instructions ({', '.join(agent_names)})",
                            instructions,
                            target,
                            status,
                            _link_issue(target, instructions, status),
                        )
                    )
            elif instructions_status == "EMPTY":
                for target, agent_names in instruction_targets.items():
                    if target.is_symlink() and target.resolve(
                        strict=False
                    ) == instructions.resolve(strict=False):
                        statuses.append("DRIFT")
                        details.append(
                            ProjectResourceDetail(
                                f"Instructions ({', '.join(agent_names)})",
                                instructions,
                                target,
                                "DRIFT",
                                f"Empty canonical instructions no longer require {target}",
                            )
                        )
                project_agents_md = project_path / "AGENTS.md"
                if project_agents_md.exists() and not (
                    project_agents_md.is_symlink()
                    and project_agents_md.resolve(strict=False)
                    == instructions.resolve(strict=False)
                ):
                    instructions_notice = (
                        f"Project-owned AGENTS.md detected: {project_agents_md} "
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
                allow_matching_copies=True,
            )
            skill_issues: list[str] = []
            if cleanup_plan.conflicts:
                skills_notice = "Project-owned skills detected: " + ", ".join(
                    path.name for path in cleanup_plan.conflicts
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
                    f"Deselected managed skill: {path}" for path in cleanup_plan.cleanup
                )
            else:
                skill_statuses: list[str] = []
                for skill_name in skill_names:
                    canonical = aikito_dir / "skills" / skill_name
                    runtime = skills_runtime / skill_name
                    if sync_mode == "copy":
                        state = copied_skill_states.get((project_dir.name, skill_name))
                        status = state.status if state else "MISSING"
                        skill_statuses.append(status)
                        if status != "OK":
                            reason = state.reason if state and state.reason else status
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
                    "Skills",
                    aikito_dir / "skills",
                    skills_runtime,
                    skills_status,
                    "; ".join(skill_issues),
                )
            )

            project_memory = project_dir / "memory"
            memory_runtime = agents_dir / "memory"
            expected_memory: dict[str, Path] = {
                Path(reference).parts[0]: aikito_dir / "memory" / reference
                for reference in memory_refs
                if Path(reference).parts
            }
            if project_memory.is_dir():
                expected_memory.update(
                    {item.name: item for item in project_memory.iterdir()}
                )
            memory_statuses: list[str] = []
            memory_issues: list[str] = []
            for name, source in expected_memory.items():
                target = memory_runtime / name
                status = _link_status(target, source)
                memory_statuses.append(status)
                issue = _link_issue(target, source, status)
                if issue:
                    memory_issues.append(f"{name}: {issue}")
            memory_cleanup = plan_runtime_cleanup(
                memory_runtime,
                set(expected_memory),
                (aikito_dir / "memory", project_memory),
                allow_matching_copies=False,
            )
            if memory_cleanup.conflicts:
                memory_status = "CONFLICT"
                memory_issues.extend(
                    f"Unmanaged runtime entry: {path}"
                    for path in memory_cleanup.conflicts
                )
            elif memory_cleanup.cleanup:
                memory_status = "DRIFT"
                memory_issues.extend(
                    f"Stale managed memory: {path}" for path in memory_cleanup.cleanup
                )
            else:
                memory_status = _aggregate_runtime_status(memory_statuses)
            statuses.append(memory_status)
            details.append(
                ProjectResourceDetail(
                    "Memory",
                    project_memory,
                    memory_runtime,
                    memory_status,
                    "; ".join(memory_issues),
                )
            )
            runtime_status = _aggregate_runtime_status(statuses)

        summaries.append(
            ProjectSummary(
                name=project_dir.name,
                path=_display_path(project_path, home),
                sync_mode=sync_mode,
                instructions_status=instructions_status,
                skills_count=len(skill_names),
                memory_notes_count=memory_notes_count,
                runtime_status=runtime_status,
                config_path=config_path,
                skill_names=skill_names,
                memory_refs=memory_refs,
                details=tuple(details),
                instructions_notice=instructions_notice,
                skills_notice=skills_notice,
            )
        )
    return summaries


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

        project_path = _resolve_project_path(config.get("path"), home)
        for skill_name in sorted(str(name) for name in config.get("skills", [])):
            canonical = aikito_dir / "skills" / skill_name
            runtime = (
                project_path / ".agents" / "skills" / skill_name
                if project_path is not None
                else Path("<unbound>") / skill_name
            )
            status = "OK"
            reason = ""
            if Path(skill_name).name != skill_name or skill_name in ("", ".", ".."):
                status, reason = (
                    "CONFLICT",
                    "Skill name must be a single path component",
                )
            elif project_path is None:
                status, reason = "CONFLICT", "Project path is not configured"
            elif not canonical.is_dir():
                status, reason = "MISSING", "Canonical skill is missing"
            elif not runtime.exists():
                status, reason = "MISSING", "Runtime skill is missing"
            elif not runtime.is_dir():
                status, reason = "CONFLICT", "Runtime skill is not a directory"
            else:
                matches, error = _directories_match(canonical, runtime)
                if error:
                    status, reason = "CONFLICT", error
                elif not matches:
                    status = "DRIFT"
            states.append(
                ProjectSkillState(
                    project_name=project_dir.name,
                    skill_name=skill_name,
                    canonical_path=canonical,
                    runtime_path=runtime,
                    status=status,
                    reason=reason,
                )
            )
    return states


def _is_binary(content: bytes) -> bool:
    return b"\0" in content


def collect_project_skill_diffs(aikito_dir: Path, home: Path) -> list[tuple[str, str]]:
    """Return unified diffs for every drifted copied project skill."""
    results: list[tuple[str, str]] = []
    for state in collect_project_skill_states(aikito_dir, home):
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
