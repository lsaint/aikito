"""Inspect copied project skills without mutating canonical or runtime content."""

import difflib
import json
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from aikito_mcp import MCPConfigError, collect_project_instruction_targets
from aikito_platform import safe_relative_path


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
class ProjectPathEntry:
    label: str
    raw_path: str
    resolved_path: Path
    exists: bool


@dataclass(frozen=True)
class ProjectBinding:
    entries: tuple[ProjectPathEntry, ...]

    @property
    def active_entries(self) -> tuple[ProjectPathEntry, ...]:
        return tuple(e for e in self.entries if e.exists)

    @property
    def offline_entries(self) -> tuple[ProjectPathEntry, ...]:
        return tuple(e for e in self.entries if not e.exists)

    @property
    def primary_path(self) -> Path | None:
        if self.active_entries:
            return self.active_entries[0].resolved_path
        if self.entries:
            return self.entries[0].resolved_path
        return None


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


def get_project_candidate_paths(config: dict) -> list[tuple[str, str]]:
    """Return list of (label, raw_path) from config.

    Supports:
      1. [paths] table: { "mac": "~/path1", "win": "D:/path2" }
      2. paths array: ["~/path1", "D:/path2"]
      3. path array: ["~/path1", "D:/path2"]
      4. path string: "~/path1"
    """
    candidates: list[tuple[str, str]] = []
    raw_paths_sec = config.get("paths")
    if isinstance(raw_paths_sec, dict):
        for key, val in raw_paths_sec.items():
            if isinstance(val, str) and val.strip():
                candidates.append((str(key), val.strip()))
    elif isinstance(raw_paths_sec, list):
        for idx, item in enumerate(raw_paths_sec, start=1):
            if isinstance(item, str) and item.strip():
                candidates.append((str(idx), item.strip()))

    if not candidates:
        raw_path = config.get("path")
        if isinstance(raw_path, list):
            for idx, item in enumerate(raw_path, start=1):
                if isinstance(item, str) and item.strip():
                    candidates.append((str(idx), item.strip()))
        elif isinstance(raw_path, str) and raw_path.strip():
            candidates.append(("default", raw_path.strip()))

    return candidates


def resolve_project_binding(config: dict, home: Path) -> ProjectBinding:
    """Resolve all configured project candidate paths and categorize them."""
    candidates = get_project_candidate_paths(config)
    entries: list[ProjectPathEntry] = []
    seen_paths: set[Path] = set()
    for label, raw in candidates:
        resolved = _resolve_project_path(raw, home)
        if resolved is not None:
            if resolved in seen_paths:
                continue
            seen_paths.add(resolved)
            entries.append(
                ProjectPathEntry(
                    label=label,
                    raw_path=raw,
                    resolved_path=resolved,
                    exists=resolved.is_dir(),
                )
            )
    return ProjectBinding(tuple(entries))


def append_candidate_path_to_config(
    config_path: Path, new_raw_path: str, home: Path
) -> bool:
    """Append a new candidate path to an existing agent.toml if not already present.

    Returns True if appended, False if candidate already exists.
    Raises FileNotFoundError if config_path does not exist.
    Raises ValueError on invalid input or if rewritten TOML is invalid.
    """
    if not config_path.is_file():
        raise FileNotFoundError(f"Project config file not found: {config_path}")
    try:
        content = config_path.read_text(encoding="utf-8")
        data = tomllib.loads(content)
    except OSError as exc:
        raise OSError(f"Failed to read {config_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"Invalid TOML in {config_path}: {exc}") from exc

    candidates = get_project_candidate_paths(data)
    new_resolved = _resolve_project_path(new_raw_path, home)
    for _, raw in candidates:
        if raw == new_raw_path or (
            new_resolved and _resolve_project_path(raw, home) == new_resolved
        ):
            return False

    lines = content.splitlines()

    first_section_idx = len(lines)
    paths_section_header_idx: int | None = None
    paths_section_end_idx = len(lines)

    for i, line in enumerate(lines):
        section_match = re.match(r"^\s*\[([a-zA-Z0-9_.\-]+)\]\s*(?:#.*)?$", line)
        if section_match:
            sec_name = section_match.group(1)
            if first_section_idx == len(lines):
                first_section_idx = i
            if sec_name == "paths":
                paths_section_header_idx = i
            elif paths_section_header_idx is not None and paths_section_end_idx == len(
                lines
            ):
                paths_section_end_idx = i

    new_path_repr = json.dumps(new_raw_path)

    if paths_section_header_idx is not None:
        existing_keys = (
            set(data["paths"].keys()) if isinstance(data.get("paths"), dict) else set()
        )
        idx = 1
        while f"path_{idx}" in existing_keys:
            idx += 1
        new_key = f"path_{idx}"
        lines.insert(paths_section_end_idx, f"{new_key} = {new_path_repr}")

    elif isinstance(data.get("paths"), dict):
        existing_keys = set(data["paths"].keys())
        idx = 1
        while f"path_{idx}" in existing_keys:
            idx += 1
        new_key = f"path_{idx}"

        replaced = False
        for i in range(first_section_idx):
            if re.match(r"^\s*paths\s*=\s*\{", lines[i]):
                if "}" in lines[i]:
                    r_idx = lines[i].rindex("}")
                    before = lines[i][:r_idx].rstrip()
                    after = lines[i][r_idx:]
                    sep = ", " if before and not before.endswith("{") else " "
                    lines[i] = (
                        f"{before}{sep}{new_key} = {new_path_repr} {after.lstrip()}"
                    )
                    replaced = True
                    break
                else:
                    for j in range(i + 1, first_section_idx):
                        if "}" in lines[j]:
                            lines.insert(j, f"    {new_key} = {new_path_repr},")
                            replaced = True
                            break
                    if replaced:
                        break
        if not replaced:
            lines.insert(first_section_idx, f"[paths]\n{new_key} = {new_path_repr}\n")

    elif isinstance(data.get("paths"), list):
        replaced = False
        for i in range(first_section_idx):
            if re.match(r"^\s*paths\s*=\s*\[", lines[i]):
                if "]" in lines[i]:
                    r_idx = lines[i].rindex("]")
                    before = lines[i][:r_idx].rstrip()
                    after = lines[i][r_idx:]
                    sep = ", " if before and not before.endswith("[") else " "
                    lines[i] = f"{before}{sep}{new_path_repr}{after}"
                    replaced = True
                    break
                else:
                    for j in range(i + 1, len(lines)):
                        if "]" in lines[j]:
                            lines.insert(j, f"    {new_path_repr},")
                            replaced = True
                            break
                    if replaced:
                        break
        if not replaced:
            lines.insert(first_section_idx, f"paths = [{new_path_repr}]")

    elif "path" in data and any(
        re.match(r"^\s*path\s*=", lines[i]) for i in range(first_section_idx)
    ):
        for i in range(first_section_idx):
            if re.match(r"^\s*path\s*=", lines[i]):
                old_val = data["path"]
                if isinstance(old_val, list):
                    items = [json.dumps(p) for p in old_val] + [new_path_repr]
                else:
                    items = [json.dumps(old_val), new_path_repr]
                lines[i] = (
                    "paths = [\n" + "".join(f"    {item},\n" for item in items) + "]"
                )
                break

    else:
        entry = f"paths = [\n    {new_path_repr},\n]\n"
        if first_section_idx < len(lines):
            lines.insert(first_section_idx, entry)
        else:
            lines.append(entry)

    new_content = "\n".join(lines).strip() + "\n"

    try:
        verified_data = tomllib.loads(new_content)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(
            f"Failed to generate valid TOML for {config_path}: {exc}\nGenerated:\n{new_content}"
        ) from exc

    verified_candidates = [raw for _, raw in get_project_candidate_paths(verified_data)]
    if new_raw_path not in verified_candidates:
        raise ValueError(
            f"Failed to record new candidate path '{new_raw_path}' in {config_path}"
        )

    config_path.write_text(new_content, encoding="utf-8")
    return True


def _resolve_project_path(raw_path: object, home: Path) -> Path | None:
    if not isinstance(raw_path, str) or not raw_path:
        return None
    raw = raw_path.strip()
    if raw == "~":
        return home.resolve()
    normalized = raw.replace("\\", "/")
    if normalized.startswith("~/"):
        return (home / normalized[2:]).resolve()
    return Path(raw).expanduser().resolve()


def _display_path(path: Path | None, home: Path) -> str:
    if path is None:
        return "-"
    return safe_relative_path(path, home)


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
        description = config.get("description", "")
        if not isinstance(description, str):
            summaries.append(
                ProjectSummary(
                    name=project_dir.name,
                    path=_display_path(binding.primary_path, home),
                    sync_mode=str(config.get("sync_mode", "link")).lower(),
                    instructions_status="MISSING",
                    skills_count=0,
                    memory_notes_count=0,
                    runtime_status="INVALID CONFIG",
                    config_path=config_path,
                    error="Project description must be a string",
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
            (e.label, _display_path(e.resolved_path, home))
            for e in binding.active_entries
        )
        offline_paths = tuple(
            (e.label, _display_path(e.resolved_path, home))
            for e in binding.offline_entries
        )

        if binding.active_entries:
            primary_disp = _display_path(binding.active_entries[0].resolved_path, home)
            other_active = len(binding.active_entries) - 1
            offline_count = len(binding.offline_entries)
            if other_active > 0 and offline_count > 0:
                summary_path = (
                    f"{primary_disp} (+{other_active} active, {offline_count} offline)"
                )
            elif other_active > 0:
                summary_path = f"{primary_disp} (+{other_active} active)"
            elif offline_count > 0:
                summary_path = f"{primary_disp} ({offline_count} offline)"
            else:
                summary_path = primary_disp
        elif binding.offline_entries:
            primary_disp = _display_path(binding.offline_entries[0].resolved_path, home)
            offline_count = len(binding.offline_entries)
            if offline_count > 1:
                summary_path = f"{primary_disp} (+{offline_count - 1} offline)"
            else:
                summary_path = primary_disp
        else:
            summary_path = "-"

        details: list[ProjectResourceDetail] = []
        instructions_notices: list[str] = []
        skills_notices: list[str] = []
        if not binding.entries:
            runtime_status = "UNBOUND"
        elif not binding.active_entries:
            runtime_status = "DORMANT"
        else:
            multi_active = len(binding.active_entries) > 1
            active_statuses: list[str] = []
            for active_entry in binding.active_entries:
                project_path = active_entry.resolved_path
                p_tag = f" [{active_entry.label}]" if multi_active else ""
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
                                f"Instructions ({', '.join(agent_names)}){p_tag}",
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
                                    f"Instructions ({', '.join(agent_names)}){p_tag}",
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
                        tag_str = f" in {active_entry.label}" if multi_active else ""
                        instructions_notices.append(
                            f"Project-owned AGENTS.md detected{tag_str}: {project_agents_md} "
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
                for name, source in sorted(expected_memory.items()):
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
                        f"Stale managed memory: {path}"
                        for path in memory_cleanup.cleanup
                    )
                else:
                    memory_status = _aggregate_runtime_status(memory_statuses)
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
                path=summary_path,
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
            )
        )
    return summaries


def classify_project_skill_state(
    aikito_dir: Path,
    project_name: str,
    project_path: Path | None,
    skill_name: str,
) -> ProjectSkillState:
    """Classify the synchronization state of a single copied skill for a project path."""
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
) -> list[ProjectSkillState]:
    """Classify copied runtime skills for a single project path."""
    return [
        classify_project_skill_state(
            aikito_dir, project_name, project_path, str(skill_name)
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
                    aikito_dir, project_dir.name, entry.resolved_path, skills
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
