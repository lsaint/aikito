"""
Deep workspace diagnostics for aikito.

Run all health checks and return a structured DoctorReport. This module is a
peer of aikito_status — neither imports the other. Shared symlink logic lives
in aikito_link.
"""

import json
import os
import re
import shutil
import shlex
import stat
import subprocess
import sys
import time
import tomllib
from pathlib import Path
from typing import List, Optional


from aikito_config import (
    get_project_memory_stale_days,
    get_workspace_config_path,
    load_workspace_config,
)
from aikito_init import (
    AGENTS_TOML_TEMPLATE,
    _detect_existing_agents,
    detected_agent_names,
)
from aikito_link import SymlinkVerdict, classify_symlink
from aikito_mcp import (
    MCPConfigError,
    _load_document,
    _parse_jsonc,
    evaluate_spec_status,
    load_agent_specs,
    load_agents,
)
from aikito_memory import extract_note_title, validate_memory_name
from aikito_project import collect_project_summaries
from aikito_registry import add_missing_agent_fields, missing_agent_fields
from aikito_render import DoctorFinding, DoctorReport, DoctorSection
from aikito_status import collect_subagents_matrix
from aikito_subagent import (
    SubagentConfigError,
    build_plan,
    load_subagent_definitions,
    validate_platform_opts,
)


def _has_user_files(directory: Path) -> bool:
    """
    Returns True if directory contains any regular files, symlinks, or unreadable entries.
    Prevents misidentifying directories containing symlinks or unreadable items as empty.
    """
    try:
        for p in directory.rglob("*"):
            if p.is_symlink() or p.is_file():
                return True
    except OSError:
        return True
    return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ok(message: str) -> DoctorFinding:
    return DoctorFinding(status="OK", message=message)


def _fail(message: str, fix_hint: str = "") -> DoctorFinding:
    return DoctorFinding(status="FAIL", message=message, fix_hint=fix_hint)


def _warn(message: str, fix_hint: str = "") -> DoctorFinding:
    return DoctorFinding(status="WARN", message=message, fix_hint=fix_hint)


def _home_rel(path: Path, home: Path) -> str:
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


# ---------------------------------------------------------------------------
# §1 Symlinks
# ---------------------------------------------------------------------------


def check_symlinks(aikito_dir: Path, home: Path) -> DoctorSection:
    """Check all managed symlinks for dangling, wrong-target, or missing."""
    findings: List[DoctorFinding] = []

    try:
        agents = load_agents(aikito_dir, home)
    except MCPConfigError as exc:
        findings.append(_fail(f"Cannot load agents.toml: {exc}"))
        return DoctorSection(name="Symlinks", findings=findings)

    global_instruction_source = aikito_dir / "global" / "AGENTS.md"

    # 1a. Per-agent instruction symlinks
    instr_ok = 0
    instr_total = 0
    for name, definition in agents.items():
        if definition.instruction_path is None:
            continue
        target = definition.instruction_path
        if not target.parent.exists():
            continue  # agent not installed — not a symlink issue
        instr_total += 1
        verdict = classify_symlink(target, global_instruction_source)
        display = _home_rel(target, home)
        if verdict == SymlinkVerdict.OK:
            instr_ok += 1
        elif verdict == SymlinkVerdict.DANGLING:
            findings.append(
                _fail(
                    f"{definition.display_name} instructions: dangling symlink ({display})",
                    "aikito sync global",
                )
            )
        elif verdict == SymlinkVerdict.WRONG_TARGET:
            findings.append(
                _fail(
                    f"{definition.display_name} instructions: points elsewhere ({display})",
                    "aikito sync global",
                )
            )
        elif verdict == SymlinkVerdict.NOT_SYMLINK:
            findings.append(
                _fail(
                    f"{definition.display_name} instructions: not a symlink ({display})",
                    "aikito sync global",
                )
            )
        else:  # MISSING
            findings.append(
                _fail(
                    f"{definition.display_name} instructions: missing ({display})",
                    "aikito sync global",
                )
            )

    if instr_total > 0 and instr_ok == instr_total:
        findings.append(_ok(f"Global instructions OK ({instr_ok} agents)"))

    # 1b. Per-agent skills directory symlinks
    agents_skills_dir = home / ".agents" / "skills"
    skills_toml_path = aikito_dir / "skills.toml"
    global_skills: List[str] = []
    if skills_toml_path.exists():
        try:
            with open(skills_toml_path, "rb") as f:
                data = tomllib.load(f)
            global_skills = data.get("skills", [])
        except tomllib.TOMLDecodeError:
            pass

    skills_fail_count = 0
    skills_checked_count = 0
    seen_skill_targets: set[Path] = set()
    for name, definition in agents.items():
        if definition.skills_path is None:
            continue
        skills_dir = definition.skills_path
        if not skills_dir.parent.exists():
            continue
        for skill_name in global_skills:
            skill_target = skills_dir / skill_name
            if skill_target in seen_skill_targets:
                continue
            seen_skill_targets.add(skill_target)
            expected = aikito_dir / "skills" / skill_name
            verdict = classify_symlink(skill_target, expected)
            display = _home_rel(skill_target, home)
            skills_checked_count += 1
            if verdict == SymlinkVerdict.DANGLING:
                skills_fail_count += 1
                findings.append(
                    _fail(
                        f"{definition.display_name}/{skill_name}: dangling symlink ({display})",
                        "aikito sync global",
                    )
                )
            elif verdict == SymlinkVerdict.WRONG_TARGET:
                skills_fail_count += 1
                findings.append(
                    _fail(
                        f"{definition.display_name}/{skill_name}: points elsewhere ({display})",
                        "aikito sync global",
                    )
                )
            elif verdict == SymlinkVerdict.NOT_SYMLINK:
                skills_fail_count += 1
                findings.append(
                    _fail(
                        f"{definition.display_name}/{skill_name}: not a symlink ({display})",
                        "aikito sync global",
                    )
                )
            elif verdict == SymlinkVerdict.MISSING:
                skills_fail_count += 1
                findings.append(
                    _fail(
                        f"{definition.display_name}/{skill_name}: missing symlink ({display})",
                        "aikito sync global",
                    )
                )

    if global_skills and skills_fail_count == 0 and skills_checked_count > 0:
        findings.append(
            _ok(
                f"Global skills OK ({len(global_skills)} skills, "
                f"{sum(1 for d in agents.values() if d.skills_path and d.skills_path.parent.exists())} agents)"
            )
        )

    # 1c. Global ~/.agents/skills/ aggregation directory entries
    if agents_skills_dir.is_dir():
        for item in sorted(agents_skills_dir.iterdir()):
            if item.is_symlink():
                try:
                    item.resolve(strict=True)
                except OSError:
                    findings.append(
                        _fail(
                            f"~/.agents/skills/{item.name}: dangling symlink",
                            "aikito sync global",
                        )
                    )

    # 1d. Project .agents/memory and .agents/skills symlinks
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if not proj_folder.is_dir():
                continue
            agent_toml = proj_folder / "agent.toml"
            if not agent_toml.is_file():
                continue
            try:
                with open(agent_toml, "rb") as f:
                    toml_data = tomllib.load(f)
                proj_path_val = toml_data.get("path")
            except (tomllib.TOMLDecodeError, OSError):
                continue
            if not proj_path_val:
                continue
            actual_proj = Path(proj_path_val).expanduser().resolve()
            if not actual_proj.exists():
                continue
            for subdir in ("memory", "skills"):
                link_path = actual_proj / ".agents" / subdir
                if link_path.is_symlink():
                    try:
                        link_path.resolve(strict=True)
                    except OSError:
                        findings.append(
                            _fail(
                                f"Project {proj_folder.name}/.agents/{subdir}: dangling symlink",
                                f"aikito sync project {proj_folder.name}",
                            )
                        )

    return DoctorSection(name="Symlinks", findings=findings)


# ---------------------------------------------------------------------------
# §2 Orphans & Unmanaged Files
# ---------------------------------------------------------------------------


def check_orphans(aikito_dir: Path, home: Path) -> DoctorSection:
    """Check for orphan subagent config files and unused skill directories."""
    findings: List[DoctorFinding] = []

    # 2a. Subagent orphans — reuse collect_subagents_matrix output
    try:
        _, orphan_files, _ = collect_subagents_matrix(aikito_dir, home)
        if orphan_files:
            for orphan in orphan_files:
                findings.append(
                    _fail(
                        f"{orphan.agent_display_name}: orphan subagent file {orphan.file_path}",
                        "aikito sync subagents --prune",
                    )
                )
        else:
            findings.append(_ok("No orphan subagent files"))
    except (SubagentConfigError, MCPConfigError) as exc:
        findings.append(_warn(f"Cannot check subagent orphans: {exc}"))

    # 2b. Skills directory — entries not in skills.toml or project agent.toml
    skills_toml_path = aikito_dir / "skills.toml"
    global_skills: set[str] = set()
    if skills_toml_path.exists():
        try:
            with open(skills_toml_path, "rb") as f:
                data = tomllib.load(f)
            global_skills = set(data.get("skills", []))
        except tomllib.TOMLDecodeError:
            pass

    project_skills: set[str] = set()
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in projects_dir.iterdir():
            if proj_folder.is_dir():
                agent_toml = proj_folder / "agent.toml"
                if agent_toml.is_file():
                    try:
                        with open(agent_toml, "rb") as f:
                            data = tomllib.load(f)
                            p_skills = data.get("skills", [])
                            if isinstance(p_skills, list):
                                project_skills.update(str(s) for s in p_skills)
                    except (tomllib.TOMLDecodeError, OSError):
                        pass

    all_registered_skills = global_skills | project_skills

    # Check for orphan skill directories in <workspace>/skills/
    source_skills_dir = aikito_dir / "skills"
    if source_skills_dir.is_dir():
        orphan_skills = []
        for item in sorted(source_skills_dir.iterdir()):
            if item.is_dir() and item.name not in all_registered_skills:
                orphan_skills.append(item.name)
        if orphan_skills:
            for name in orphan_skills:
                target_dir = source_skills_dir / name
                # Check if directory has no regular files (empty or only empty subdirectories)
                has_files = _has_user_files(target_dir)
                if not has_files:
                    msg = f"skills/{name}: empty directory, safe to delete"
                    fix_hint = f"rm -rf {shlex.quote(str(target_dir))}"
                else:
                    msg = f"skills/{name}: orphan skill directory (not in skills.toml or any project agent.toml)"
                    fix_hint = ""
                findings.append(_warn(msg, fix_hint))
        else:
            findings.append(_ok("No orphan skill directories in skills/"))

    agents_skills_dir = home / ".agents" / "skills"
    if agents_skills_dir.is_dir():
        stale: List[str] = []
        for item in sorted(agents_skills_dir.iterdir()):
            if item.name not in global_skills:
                stale.append(item.name)
        if stale:
            for name in stale:
                findings.append(
                    _fail(
                        f"~/.agents/skills/{name}: not in skills.toml",
                        "aikito sync global",
                    )
                )
        else:
            findings.append(_ok("No stale entries in ~/.agents/skills/"))

    # 2c. MCP residual managed entries in agent configs
    try:
        specs = load_agent_specs(aikito_dir, home)
        currently_defined = {(spec.agent, spec.target_name) for spec in specs}

        state_file = home / ".local/state/aikito/mcp-state.json"
        previously_managed: set[tuple[str, str]] = set()
        if state_file.is_file():
            try:
                state_doc = json.loads(state_file.read_text())
                for state_key, entry in state_doc.get("entries", {}).items():
                    if ":" in state_key and isinstance(entry, dict):
                        agent_part = state_key.split(":", 1)[0]
                        target_name = entry.get("target_name")
                        if target_name:
                            previously_managed.add((agent_part, target_name))
            except (json.JSONDecodeError, OSError):
                pass

        agents = load_agents(aikito_dir, home)
        for agent_name, definition in agents.items():
            if (
                definition.mcp_config_path is None
                or not definition.mcp_config_path.exists()
            ):
                continue
            cfg = definition.mcp_config_path
            try:
                text = cfg.read_text(encoding="utf-8")
            except (UnicodeDecodeError, PermissionError, OSError) as exc:
                findings.append(
                    _fail(
                        f"MCP config read error for {definition.display_name} ({_home_rel(cfg, home)}): {exc}",
                        f"Check file permissions or encoding of {_home_rel(cfg, home)}",
                    )
                )
                continue

            existing_servers: set[str] = set()
            fmt = definition.mcp_config_format
            if fmt in ("agy_json", "claude_json", "copilot_json"):
                try:
                    doc = json.loads(text)
                    existing_servers = set(doc.get("mcpServers", {}).keys())
                except json.JSONDecodeError:
                    pass
            elif fmt == "toml":
                try:
                    doc = tomllib.loads(text)
                    existing_servers = set(doc.get("mcp_servers", {}).keys())
                except tomllib.TOMLDecodeError:
                    pass
            elif fmt == "jsonc":
                try:
                    doc = _parse_jsonc(text)
                    if isinstance(doc, dict):
                        existing_servers = set(doc.get("mcp", {}).keys())
                except Exception:
                    pass

            for srv_key in sorted(existing_servers):
                if (
                    agent_name,
                    srv_key,
                ) in previously_managed and (
                    agent_name,
                    srv_key,
                ) not in currently_defined:
                    findings.append(
                        _fail(
                            f"{definition.display_name}: residual managed MCP entry '{srv_key}' in {_home_rel(cfg, home)}",
                            "aikito sync mcp",
                        )
                    )
    except MCPConfigError:
        pass

    return DoctorSection(name="Orphans", findings=findings)


# ---------------------------------------------------------------------------
# §3 Memory Integrity & Freshness
# ---------------------------------------------------------------------------


def _memory_scope_dirs(aikito_dir: Path) -> List[tuple]:
    """Return [(scope_dir, scope_label, proj_folder)] for global memory plus every
    registered project's memory directory."""
    scope_dirs: List[tuple] = [(aikito_dir / "memory", "Global", None)]
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for p in sorted(projects_dir.iterdir()):
            if p.is_dir():
                scope_dirs.append((p / "memory", f"Project:{p.name}", p))
    return scope_dirs


def _git_last_commit_epoch(repo_dir: Path, file_path: Path) -> int | None:
    """Return the Unix epoch of the last commit touching file_path, or None
    if the file has no commit history (e.g. untracked, or git is unavailable).
    """
    try:
        result = subprocess.run(
            [
                "git",
                "-C",
                str(repo_dir),
                "log",
                "-1",
                "--format=%ct",
                "--",
                str(file_path),
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = result.stdout.strip()
    if result.returncode != 0 or not output:
        return None
    try:
        return int(output)
    except ValueError:
        return None


def check_memory_integrity(
    aikito_dir: Path, home: Path, stale_days_override: Optional[int] = None
) -> DoctorSection:
    """Validate durable memory notes: index/link consistency and staleness.

    This is separate from Configuration because it inspects memory *content*
    (index completeness, cross-note wikilinks, edit recency) rather than
    config file syntax. Memory is curated knowledge, not a config file, and
    its health signals are different: a dangling [[wikilink]] or a note that
    has not been touched in months is a curation gap, not a parse error.
    """
    findings: List[DoctorFinding] = []
    scope_dirs = _memory_scope_dirs(aikito_dir)
    ws_config = load_workspace_config(aikito_dir)

    global_stale_days = (
        stale_days_override
        if (stale_days_override is not None and stale_days_override > 0)
        else ws_config.memory.stale_days
    )

    # Index completeness: notes not referenced from index.md, and index.md
    # entries pointing at notes that no longer exist.
    for scope_dir, scope_label, _proj_folder in scope_dirs:
        index_file = scope_dir / "index.md"
        notes_dir = scope_dir / "notes"
        if not index_file.exists():
            continue
        content = index_file.read_text(encoding="utf-8", errors="ignore")
        indexed_stems = {
            m.group(1).strip()
            for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content)
        }
        if notes_dir.is_dir():
            for note_file in sorted(notes_dir.glob("*.md")):
                stem = note_file.stem
                name_err = validate_memory_name(stem)
                if name_err:
                    findings.append(
                        _fail(
                            f"{scope_label} note '{stem}' has invalid filename: {name_err}",
                            f"Rename note using 'aikito rename memory {stem} <valid-name>'",
                        )
                    )
                if stem not in indexed_stems:
                    findings.append(
                        _warn(
                            f"{scope_label} note '{stem}' exists but is not referenced in index.md",
                            f"Add [[{stem}]] to {scope_label}'s index.md, or delete the note if it's obsolete",
                        )
                    )
            for stem in sorted(indexed_stems):
                note_path = notes_dir / f"{stem}.md"
                if not note_path.exists():
                    findings.append(
                        _fail(
                            f"{scope_label} index.md references [[{stem}]] but file not found",
                            f"Remove the [[{stem}]] entry from index.md, or restore notes/{stem}.md",
                        )
                    )

        # Check index entry formatting: standard format is "- [[note-stem|Display Text]]"
        for line in content.splitlines():
            if "[[" in line and "]]" in line:
                m_link = re.search(r"\[\[([^\]|]+)(?:\|([^\]]+))?\]\]", line)
                if m_link:
                    link_stem = m_link.group(1).strip()
                    has_pipe = bool(m_link.group(2))
                    has_trailing_desc = bool(re.search(r"\]\]\s*[-—:]\s*\S+", line))
                    if not has_pipe or has_trailing_desc:
                        findings.append(
                            _warn(
                                f"{scope_label} index.md entry for '[[{link_stem}]]' uses non-standard format",
                                "Run 'aikito doctor --fix' to format index entries as [[note-stem|Display Text]]",
                            )
                        )

    # Cross-note wikilinks: notes reference each other via [[note-name]] in
    # their body, not just from index.md. A dangling reference here is
    # invisible to index-only checks and rots silently.
    for scope_dir, scope_label, _proj_folder in scope_dirs:
        notes_dir = scope_dir / "notes"
        if not notes_dir.is_dir():
            continue
        note_stems = {p.stem for p in notes_dir.glob("*.md")}
        for note_file in sorted(notes_dir.glob("*.md")):
            body = note_file.read_text(encoding="utf-8", errors="ignore")
            referenced = {
                m.group(1).strip()
                for m in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", body)
            }
            for target_stem in sorted(referenced):
                if target_stem not in note_stems:
                    findings.append(
                        _fail(
                            f"{scope_label} note '{note_file.stem}' links to "
                            f"[[{target_stem}]] but that note does not exist",
                            f"Write notes/{target_stem}.md if the topic is still worth keeping, "
                            f"or edit {note_file.stem}.md to drop the [[{target_stem}]] link",
                        )
                    )

    # Freshness: notes untouched in Git for a long time. Age is read from Git
    # history rather than filesystem mtime, since mtime changes on
    # checkout/clone/sync and does not reflect real edit recency. Staleness
    # is a prompt to review, not a verdict — some notes (stable architecture
    # constraints) are legitimately old and still correct.
    now = int(time.time())
    checked_any = False
    stale_found = False
    stale_days_used = set()

    for scope_dir, scope_label, proj_folder in scope_dirs:
        notes_dir = scope_dir / "notes"
        if not notes_dir.is_dir():
            continue

        if stale_days_override is not None and stale_days_override > 0:
            scope_stale_days = stale_days_override
        elif proj_folder is not None:
            scope_stale_days = get_project_memory_stale_days(
                proj_folder, default_stale_days=global_stale_days
            )
        else:
            scope_stale_days = global_stale_days

        stale_days_used.add(scope_stale_days)
        threshold = scope_stale_days * 86400

        for note_file in sorted(notes_dir.glob("*.md")):
            checked_any = True
            last_commit = _git_last_commit_epoch(aikito_dir, note_file)
            if last_commit is None:
                continue
            age_days = (now - last_commit) // 86400
            if now - last_commit > threshold:
                stale_found = True
                findings.append(
                    _warn(
                        f"{scope_label} note '{note_file.stem}' has not been updated "
                        f"in {age_days} days (threshold: {scope_stale_days}d) — worth a re-read to confirm it still holds",
                        f"Re-read notes/{note_file.stem}.md; rewrite if it drifted, "
                        "or leave it if it's still accurate",
                    )
                )

    if checked_any and not stale_found:
        if len(stale_days_used) == 1:
            stale_desc = f"{next(iter(stale_days_used))} days"
        else:
            stale_desc = f"configured thresholds ({', '.join(str(d) for d in sorted(stale_days_used))} days)"
        findings.append(_ok(f"No memory notes older than {stale_desc}"))
    elif not checked_any:
        findings.append(_ok("No memory notes found"))

    return DoctorSection(name="Memory", findings=findings)


# ---------------------------------------------------------------------------
# §4 Config Syntax & Schema
# ---------------------------------------------------------------------------


def check_config_syntax(aikito_dir: Path, home: Path) -> DoctorSection:
    """Validate syntax of all TOML/JSON workspace config files."""
    findings: List[DoctorFinding] = []

    # Optional workspace config (config.toml)
    cfg_path = get_workspace_config_path(aikito_dir)
    if cfg_path:
        try:
            with open(cfg_path, "rb") as f:
                tomllib.load(f)
            findings.append(_ok(f"{cfg_path.name}: valid TOML"))
        except tomllib.TOMLDecodeError as exc:
            findings.append(_fail(f"{cfg_path.name}: TOML parse error — {exc}"))

    # 3a. Canonical TOML files and mcps directory
    for filename in ("agents.toml", "skills.toml", "subagents.toml"):
        path = aikito_dir / filename
        if not path.exists():
            findings.append(_fail(f"{filename}: file not found"))
            continue
        try:
            with open(path, "rb") as f:
                tomllib.load(f)
            findings.append(_ok(f"{filename}: valid TOML"))
            if filename == "agents.toml":
                with path.open("rb") as registry_file:
                    registered_agents = set(
                        tomllib.load(registry_file).get("agents", {})
                    )
                installed_agents = detected_agent_names(
                    _detect_existing_agents(home)
                )
                for agent_name in installed_agents:
                    if agent_name not in registered_agents:
                        findings.append(
                            _warn(
                                f"agents.toml: installed Agent '{agent_name}' is not registered",
                                "aikito doctor --fix",
                            )
                        )
                for agent_name, fields in missing_agent_fields(
                    path, AGENTS_TOML_TEMPLATE
                ).items():
                    field_names = ", ".join(".".join(field) for field in fields)
                    findings.append(
                        _warn(
                            f"agents.toml [{agent_name}]: missing bundled fields: "
                            f"{field_names}",
                            "aikito doctor --fix",
                        )
                    )
        except tomllib.TOMLDecodeError as exc:
            findings.append(_fail(f"{filename}: TOML parse error — {exc}"))

    mcps_dir = aikito_dir / "mcps"
    if not mcps_dir.exists():
        findings.append(_fail("mcps: directory not found"))
    elif not mcps_dir.is_dir():
        findings.append(_fail("mcps: path is not a directory"))
    else:
        toml_files = sorted(mcps_dir.glob("*.toml"))
        if not toml_files:
            findings.append(_ok("mcps: directory present (empty)"))
        for toml_path in toml_files:
            try:
                with open(toml_path, "rb") as f:
                    tomllib.load(f)
                findings.append(_ok(f"mcps/{toml_path.name}: valid TOML"))
            except tomllib.TOMLDecodeError as exc:
                findings.append(
                    _fail(f"mcps/{toml_path.name}: TOML parse error — {exc}")
                )

    # 3b. Project agent.toml files
    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if not proj_folder.is_dir():
                continue
            agent_toml = proj_folder / "agent.toml"
            if not agent_toml.exists():
                continue
            try:
                with open(agent_toml, "rb") as f:
                    data = tomllib.load(f)
                findings.append(
                    _ok(f"projects/{proj_folder.name}/agent.toml: valid TOML")
                )
                # Check required fields and path validity
                proj_path_val = data.get("path")
                if not proj_path_val:
                    findings.append(
                        _warn(
                            f"projects/{proj_folder.name}/agent.toml: missing 'path' field",
                        )
                    )
                else:
                    proj_path = Path(proj_path_val).expanduser().resolve()
                    if not proj_path.exists():
                        findings.append(
                            _fail(
                                f"projects/{proj_folder.name}/agent.toml: path '{proj_path_val}' does not exist",
                                f"Update projects/{proj_folder.name}/agent.toml",
                            )
                        )
            except tomllib.TOMLDecodeError as exc:
                findings.append(
                    _fail(
                        f"projects/{proj_folder.name}/agent.toml: TOML parse error — {exc}"
                    )
                )

    # 3c. Agent native config files
    try:
        agents = load_agents(aikito_dir, home)
        for name, definition in agents.items():
            cfg = definition.mcp_config_path
            if cfg is None or not cfg.exists():
                continue
            fmt = definition.mcp_config_format
            display = _home_rel(cfg, home)
            try:
                text = cfg.read_text(encoding="utf-8")
                if fmt in ("agy_json", "claude_json", "copilot_json"):
                    json.loads(text)
                    findings.append(
                        _ok(f"{definition.display_name} config: valid JSON ({display})")
                    )
                elif fmt == "toml":
                    tomllib.loads(text)
                    findings.append(
                        _ok(f"{definition.display_name} config: valid TOML ({display})")
                    )
                elif fmt == "jsonc":
                    # Basic parse via our internal parser
                    _parse_jsonc(text)
                    findings.append(
                        _ok(
                            f"{definition.display_name} config: valid JSONC ({display})"
                        )
                    )
                elif fmt == "dsh_cordis":
                    _load_document(fmt, text)
                    findings.append(
                        _ok(
                            f"{definition.display_name} config: valid Cordis patch YAML ({display})"
                        )
                    )
            except (
                json.JSONDecodeError,
                tomllib.TOMLDecodeError,
                MCPConfigError,
                UnicodeDecodeError,
                PermissionError,
                OSError,
            ) as exc:
                findings.append(
                    _fail(
                        f"{definition.display_name} config: read/parse error — {exc} ({display})"
                    )
                )
    except MCPConfigError as exc:
        findings.append(_warn(f"Cannot load agents for config check: {exc}"))

    # 3d. Subagent platform option schema
    try:
        defs = load_subagent_definitions(aikito_dir, allow_empty=True)
        subagent_schema_ok = True
        for sub_name, definition in defs.items():
            for agent_name, opts in definition.platform_configs.items():
                try:
                    validate_platform_opts(agent_name, sub_name, opts)
                except SubagentConfigError as exc:
                    findings.append(_fail(f"subagents.toml [{sub_name}]: {exc}"))
                    subagent_schema_ok = False
        if subagent_schema_ok and defs:
            findings.append(_ok("Subagent platform options: all valid"))
    except SubagentConfigError as exc:
        findings.append(_fail(f"subagents.toml: {exc}"))

    return DoctorSection(name="Configuration", findings=findings)


def check_projects(aikito_dir: Path, home: Path) -> DoctorSection:
    """Report project runtime health using the same model as `show project`."""
    findings: List[DoctorFinding] = []
    projects = collect_project_summaries(aikito_dir, home)
    for project in projects:
        if project.runtime_status == "OK":
            continue
        if project.details:
            counts: dict[str, int] = {}
            for detail in project.details:
                if detail.status != "OK":
                    counts[detail.status] = counts.get(detail.status, 0) + 1
            summary = ", ".join(
                f"{count} {status.lower()}"
                for status in ("MISSING", "DRIFT", "CONFLICT")
                if (count := counts.get(status, 0))
            )
            message = f"Project '{project.name}': {project.runtime_status}"
            if summary:
                message += f" — {summary}"
        else:
            message = f"Project '{project.name}': {project.runtime_status}"
            if project.error:
                message += f" — {project.error}"

        action = (
            f"aikito sync project {project.name}"
            if project.runtime_status == "MISSING"
            else f"aikito show project {project.name}"
        )
        findings.append(_fail(message, action))

    if not findings:
        findings.append(_ok(f"Project runtimes OK ({len(projects)} projects)"))
    return DoctorSection(name="Projects", findings=findings)


# ---------------------------------------------------------------------------
# §5 Fingerprint Drift
# ---------------------------------------------------------------------------


def check_drift(aikito_dir: Path, home: Path) -> DoctorSection:
    """Check MCP managed-section fingerprint drift via evaluate_spec_status."""
    findings: List[DoctorFinding] = []

    try:
        specs = load_agent_specs(aikito_dir, home)
    except MCPConfigError as exc:
        findings.append(_fail(f"Cannot load MCP specs: {exc}"))
        return DoctorSection(name="Drift", findings=findings)

    drift_count = 0
    checked = 0
    for spec in specs:
        if not spec.enabled:
            continue
        st = evaluate_spec_status(spec)
        if st == "SKIP":
            continue
        checked += 1
        if st == "OK":
            pass
        elif st == "DRIFT":
            drift_count += 1
            if spec.missing_credential_env:
                findings.append(
                    _warn(
                        f"{spec.agent} × {spec.server}: credential-dependent MCP config differs; "
                        f"current shell may have stale or missing {spec.missing_credential_env}",
                        "open a new shell and run: aikito doctor",
                    )
                )
            else:
                findings.append(
                    _fail(
                        f"{spec.agent} × {spec.server}: managed MCP config differs",
                        "aikito sync mcp",
                    )
                )
        elif st == "MISSING":
            findings.append(
                _fail(
                    f"{spec.agent} × {spec.server}: managed entry missing from config",
                    "aikito sync mcp",
                )
            )
        elif st == "ERROR":
            findings.append(
                _fail(
                    f"{spec.agent} × {spec.server}: config parse error",
                    "aikito sync mcp",
                )
            )

    if drift_count == 0 and checked > 0:
        findings.append(_ok(f"MCP fingerprints OK ({checked} entries)"))
    elif checked == 0:
        findings.append(_ok("No managed MCP entries to check"))

    try:
        subagent_plan, _ = build_plan(aikito_dir, home, allow_empty=True)
    except SubagentConfigError as exc:
        findings.append(_fail(f"Cannot build subagent synchronization plan: {exc}"))
        return DoctorSection(name="Drift", findings=findings)

    subagent_checked = 0
    subagent_issues = 0
    for item in subagent_plan:
        if item.action in ("SKIP", "ORPHAN") or item.subagent_name == "*":
            continue
        subagent_checked += 1
        target = _home_rel(item.target_path, home)
        if item.action == "CREATE":
            subagent_issues += 1
            findings.append(
                _fail(
                    f"{item.agent_name}/{item.subagent_name}: managed subagent missing ({target})",
                    "aikito sync subagents",
                )
            )
        elif item.action in ("UPDATE", "FORCE UPDATE"):
            subagent_issues += 1
            findings.append(
                _fail(
                    f"{item.agent_name}/{item.subagent_name}: managed subagent drift ({target})",
                    "aikito sync subagents",
                )
            )
        elif item.action == "CONFLICT":
            subagent_issues += 1
            findings.append(
                _fail(
                    f"{item.agent_name}/{item.subagent_name}: unmanaged target conflict ({target})",
                    f"aikito sync subagents --force {item.agent_name}/{item.subagent_name}",
                )
            )
        elif item.action == "ERROR":
            subagent_issues += 1
            findings.append(
                _fail(
                    f"{item.agent_name}/{item.subagent_name}: {item.reason}",
                    "Review subagents.toml and the target agent configuration",
                )
            )

    if subagent_checked == 0:
        findings.append(_ok("No managed subagent entries to check"))
    elif subagent_issues == 0:
        findings.append(_ok(f"Subagent managed files OK ({subagent_checked} entries)"))

    return DoctorSection(name="Drift", findings=findings)


# ---------------------------------------------------------------------------
# §6 Security & Permissions
# ---------------------------------------------------------------------------


def check_security(aikito_dir: Path, home: Path) -> DoctorSection:
    """Check file permissions and security-sensitive configurations."""
    findings: List[DoctorFinding] = []

    # 5a. Platform warning for Windows
    if sys.platform == "win32":
        findings.append(
            _warn(
                "Running on Windows: chmod(0o600) is a no-op, credential files may be world-readable",
                "Use WSL2 for full symlink and permission support",
            )
        )

    # 5b. Credential config files must be chmod 0600
    try:
        specs = load_agent_specs(aikito_dir, home)
        cred_issues = 0
        cred_checked = 0
        seen_paths: set[Path] = set()
        for spec in specs:
            if not spec.contains_secret or spec.config_path in seen_paths:
                continue
            seen_paths.add(spec.config_path)
            if not spec.config_path.exists():
                continue
            cred_checked += 1
            mode = stat.S_IMODE(spec.config_path.stat().st_mode)
            if mode != 0o600:
                cred_issues += 1
                display = _home_rel(spec.config_path, home)
                findings.append(
                    _fail(
                        f"Credential file has insecure permissions {oct(mode)}: {display}",
                        f"chmod 600 {display}",
                    )
                )
        if cred_checked > 0 and cred_issues == 0:
            findings.append(
                _ok(f"Credential file permissions OK ({cred_checked} files, mode 0600)")
            )
        elif cred_checked == 0:
            findings.append(_ok("No secret-bearing credential config files detected"))
    except MCPConfigError:
        pass

    # 5c. .gitignore covers .local/state/
    gitignore = aikito_dir / ".gitignore"
    if gitignore.is_file():
        content = gitignore.read_text()
        if ".local/state" not in content and ".local/" not in content:
            findings.append(
                _warn(
                    "Workspace .gitignore may not cover .local/state/ (MCP state & backups)",
                    "Add '/.local/' to .gitignore",
                )
            )
        else:
            findings.append(_ok(".gitignore covers .local/state/"))

    return DoctorSection(name="Security", findings=findings)


# ---------------------------------------------------------------------------
# §7 Runtime Environment
# ---------------------------------------------------------------------------


def check_environment(aikito_dir: Path, home: Path) -> DoctorSection:
    """Check runtime environment: AIKITO_DIR, interpreter consistency, agent CLIs."""
    findings: List[DoctorFinding] = []

    # 6a. AIKITO_DIR resolves to a valid workspace
    env_dir = os.environ.get("AIKITO_DIR")
    if env_dir:
        env_path = Path(env_dir).expanduser().resolve()
        if env_path != aikito_dir:
            findings.append(
                _warn(
                    f"$AIKITO_DIR ({env_dir}) resolved to {env_path}, but aikito_dir={aikito_dir}",
                )
            )
        elif not env_path.exists():
            findings.append(
                _fail(
                    f"$AIKITO_DIR points to non-existent path: {env_dir}",
                )
            )
        else:
            findings.append(_ok(f"$AIKITO_DIR → {_home_rel(env_path, home)}"))
    else:
        findings.append(
            _ok(
                "AIKITO_DIR not set; using configured workspace: "
                f"{_home_rel(aikito_dir, home)}"
            )
        )

    # 6b. Interpreter consistency: $PATH python3 vs sys.executable
    path_python3 = shutil.which("python3")
    running = sys.executable
    if path_python3:
        try:
            path_resolved = Path(path_python3).resolve()
            running_resolved = Path(running).resolve()
            if path_resolved != running_resolved:
                findings.append(
                    _warn(
                        f"$PATH python3 ({path_python3}) differs from running interpreter ({running})",
                        "Adjust $PATH or the aikito shebang to use the same interpreter",
                    )
                )
            else:
                findings.append(_ok(f"Interpreter consistent: {path_python3}"))
        except Exception:
            findings.append(_warn("Cannot compare interpreter paths"))
    else:
        findings.append(_warn("python3 not found in $PATH"))

    # 6c. Agent CLI availability
    cli_map = {
        "codex": "codex",
        "claude": "claude",
        "agy": "agy",
        "opencode": "opencode",
        "copilot": "copilot",
        "dsh": "dsh",
        "grok": "grok",
    }
    for label, binary in cli_map.items():
        if shutil.which(binary):
            findings.append(_ok(f"{label} CLI found ({binary})"))
        else:
            findings.append(_warn(f"{label} CLI not found in $PATH ({binary})"))

    return DoctorSection(name="Environment", findings=findings)


def run_doctor_fixes(aikito_dir: Path, home: Optional[Path] = None) -> List[str]:
    """Apply safe automated fixes to memory index files.

    1. Prune dangling entries in index.md (pointing to non-existent notes).
    2. Normalize non-standard index entries (convert bare [[stem]] or entries with
       trailing descriptions to [[stem|Title]] using the note's heading title).

    Returns a list of human-readable descriptions of the fixes performed.
    """
    fixes: List[str] = []
    resolved_home = home or Path.home()
    agents_path = aikito_dir / "agents.toml"
    if agents_path.is_file():
        try:
            installed_agents = detected_agent_names(
                _detect_existing_agents(resolved_home)
            )
            fixes.extend(
                add_missing_agent_fields(
                    agents_path, AGENTS_TOML_TEMPLATE, installed_agents
                )
            )
        except (OSError, tomllib.TOMLDecodeError):
            pass
    scope_dirs = _memory_scope_dirs(aikito_dir)

    for scope_dir, scope_label, _proj_folder in scope_dirs:
        index_file = scope_dir / "index.md"
        notes_dir = scope_dir / "notes"
        if not index_file.exists():
            continue

        existing_notes = (
            {p.stem: p for p in notes_dir.glob("*.md") if p.is_file()}
            if notes_dir.is_dir()
            else {}
        )
        content = index_file.read_text(encoding="utf-8", errors="ignore")
        lines = content.splitlines()

        new_lines: List[str] = []
        modified = False

        for line in lines:
            m = re.search(
                r"^(\s*-\s*)\[\[([^\]|]+)(?:\|([^\]]+))?\]\](?:\s*[-—:]\s*.+)?$", line
            )
            if not m:
                new_lines.append(line)
                continue

            prefix = m.group(1)
            stem = m.group(2).strip()
            existing_display = m.group(3).strip() if m.group(3) else None

            # 1. Prune dangling index entry
            if stem not in existing_notes:
                fixes.append(
                    f"Removed dangling index entry [[{stem}]] from {scope_label}/index.md"
                )
                modified = True
                continue

            # 2. Normalize index entry format: [[stem|Title]] without trailing descriptions
            display_title = existing_display or extract_note_title(existing_notes[stem])
            normalized_line = f"{prefix}[[{stem}|{display_title}]]"

            if normalized_line != line:
                fixes.append(
                    f"Normalized index entry for [[{stem}]] in {scope_label}/index.md"
                )
                new_lines.append(normalized_line)
                modified = True
            else:
                new_lines.append(line)

        if modified:
            output_text = "\n".join(new_lines)
            if output_text and not output_text.endswith("\n"):
                output_text += "\n"
            index_file.write_text(output_text, encoding="utf-8")

    return fixes


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def run_doctor(
    aikito_dir: Path, home: Path, stale_days: Optional[int] = None
) -> DoctorReport:
    """Run all diagnostic checks and return a structured DoctorReport."""
    sections = [
        check_symlinks(aikito_dir, home),
        check_orphans(aikito_dir, home),
        check_memory_integrity(aikito_dir, home, stale_days_override=stale_days),
        check_drift(aikito_dir, home),
        check_security(aikito_dir, home),
        check_environment(aikito_dir, home),
        check_projects(aikito_dir, home),
        # Keep check_config_syntax last: it's the slowest section (parses every
        # workspace config file), so cheaper checks report first.
        check_config_syntax(aikito_dir, home),
    ]
    return DoctorReport(sections=sections)
