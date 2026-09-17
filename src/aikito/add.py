"""
Resource addition module for Aikito.
Provides lightweight canonical skeleton creation and registration for skills, subagents, and MCP servers.
"""

import json
import os
import re
import shutil
import stat
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

from .compat import safe_relative_path


NAME_PATTERN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?$")

DEFAULT_SUBAGENT_AGENTS = ["codex", "claude-code", "agy", "github-copilot"]
DEFAULT_MCP_AGENTS = ["codex", "claude-code", "opencode", "agy", "github-copilot"]


def validate_resource_name(name: str, resource_type: str) -> Optional[str]:
    """Validate resource name conforms to kebab-case alphanumeric naming."""
    if not name or not isinstance(name, str) or not name.strip():
        return f"{resource_type.capitalize()} name cannot be empty."

    name_clean = name.strip()
    if (
        "/" in name_clean
        or "\\" in name_clean
        or "\0" in name_clean
        or ".." in name_clean
    ):
        return f"Invalid {resource_type} name '{name}'. Path separators and traversals are not allowed."

    if not NAME_PATTERN.fullmatch(name_clean):
        return (
            f"Invalid {resource_type} name '{name}'. "
            f"Must be kebab-case (lowercase alphanumeric characters separated by hyphens, e.g. 'my-{resource_type}')."
        )
    return None


def _check_workspace_initialized(aikito_dir: Path) -> Optional[str]:
    """Verify that aikito workspace exists and contains valid marker files."""
    if not aikito_dir.exists() or not aikito_dir.is_dir():
        return f"Aikito workspace directory not found: {aikito_dir}"

    # Check for basic workspace marker configs
    if (
        not (aikito_dir / "agents.toml").exists()
        and not (aikito_dir / "skills.toml").exists()
    ):
        return f"Aikito workspace is not initialized at: {aikito_dir}"
    return None


def _titleize(name: str) -> str:
    """Convert kebab-case or snake_case name to Title Case for markdown headings."""
    return " ".join(
        word.capitalize() for word in name.replace("-", " ").replace("_", " ").split()
    )


def _display_path(path: Path, home: Path) -> str:
    return safe_relative_path(path, home)


def _find_matching_bracket(text: str, start_bracket_pos: int) -> int:
    """Find the closing bracket ']' matching the opening bracket at start_bracket_pos."""
    in_string = False
    string_char = ""
    escape = False
    depth = 0
    i = start_bracket_pos
    while i < len(text):
        char = text[i]
        if escape:
            escape = False
            i += 1
            continue
        if char == "\\":
            if in_string:
                escape = True
            i += 1
            continue
        if char in ('"', "'"):
            if not in_string:
                in_string = True
                string_char = char
            elif string_char == char:
                in_string = False
                string_char = ""
            i += 1
            continue
        if not in_string:
            if char == "[":
                depth += 1
            elif char == "]":
                depth -= 1
                if depth == 0:
                    return i
        i += 1
    return -1


def _format_skills_array(skills: List[str]) -> str:
    """Format skills array in standard clean multi-line TOML format."""
    if not skills:
        return "skills = []"
    items_str = ",\n".join(f'    "{s}"' for s in skills)
    return f"skills = [\n{items_str}\n]"


def _update_skills_in_toml(original_text: str, new_skills: List[str]) -> str:
    """
    Update the top-level 'skills' array in TOML content while preserving all other keys,
    nested tables ([table]), comments, whitespace, and formatting intact.
    """
    formatted_skills = _format_skills_array(new_skills)

    # 1. Match top-level skills = [ ... ]
    match = re.search(r"(?m)^skills\s*=\s*\[", original_text)
    if match:
        start_bracket_idx = match.end() - 1
        end_bracket_idx = _find_matching_bracket(original_text, start_bracket_idx)
        if end_bracket_idx != -1:
            prefix = original_text[: match.start()]
            suffix = original_text[end_bracket_idx + 1 :]
            return prefix + formatted_skills + suffix

    # 2. Match top-level skills = ... (fallback for single-line or non-bracket forms)
    match_other = re.search(r"(?m)^skills\s*=.*$", original_text)
    if match_other:
        prefix = original_text[: match_other.start()]
        suffix = original_text[match_other.end() :]
        return prefix + formatted_skills + suffix

    # 3. If 'skills' is not present, insert before first table header (^\[[a-zA-Z0-9_.-]+\])
    table_match = re.search(r"(?m)^\[[a-zA-Z0-9_.-]+\]", original_text)
    if table_match:
        prefix = original_text[: table_match.start()].rstrip()
        suffix = original_text[table_match.start() :]
        if prefix:
            return f"{prefix}\n\n{formatted_skills}\n\n{suffix}"
        return f"{formatted_skills}\n\n{suffix}"

    # 4. If no table headers exist, append to end
    trimmed = original_text.rstrip()
    if trimmed:
        return f"{trimmed}\n\n{formatted_skills}\n"
    return f"{formatted_skills}\n"


def _atomic_write_text(
    target_path: Path, content: str, encoding: str = "utf-8"
) -> None:
    """
    Atomically write text content to target_path using a temporary file in the same
    directory and replacing target_path with os.replace.
    Preserves file mode, permissions, and metadata (ACLs/xattrs) of existing files,
    and applies standard umask permissions to newly created files.
    """
    target_path = target_path.resolve()
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        dir=parent,
        prefix=f".{target_path.name}.tmp.",
        delete=False,
        encoding=encoding,
    )
    temp_path = Path(temp_file.name)
    try:
        temp_file.write(content)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()

        if target_path.exists():
            try:
                shutil.copystat(target_path, temp_path)
                try:
                    os.utime(temp_path, None)
                except OSError:
                    pass
            except OSError:
                try:
                    st = target_path.stat()
                    os.chmod(temp_path, stat.S_IMODE(st.st_mode))
                except OSError:
                    pass
            if hasattr(os, "chown"):
                try:
                    st = target_path.stat()
                    os.chown(temp_path, -1, st.st_gid)
                except OSError:
                    pass
        else:
            try:
                current_umask = os.umask(0)
                os.umask(current_umask)
                os.chmod(temp_path, 0o666 & ~current_umask)
            except OSError:
                pass

        os.replace(temp_path, target_path)
    except Exception:
        temp_file.close()
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def _split_markdown_frontmatter(
    content: str,
) -> Optional[Tuple[str, str, str]]:
    """
    Split markdown content into (bom, frontmatter_text, body_text).
    Frontmatter must begin with a standalone '---' line and end with a standalone '---' line.
    Returns None if content does not have a valid frontmatter block.
    """
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    raw = content[len(bom) :]
    lines = raw.splitlines(keepends=True)
    if not lines:
        return None

    first_line = lines[0].rstrip("\r\n")
    if first_line.rstrip() != "---" or first_line.startswith((" ", "\t")):
        return None

    closing_idx = None
    for idx in range(1, len(lines)):
        line_stripped = lines[idx].rstrip("\r\n")
        if line_stripped.rstrip() == "---" and not line_stripped.startswith(
            (" ", "\t")
        ):
            closing_idx = idx
            break

    if closing_idx is None:
        return None

    frontmatter_raw = "".join(lines[1:closing_idx])
    body = "".join(lines[closing_idx + 1 :])
    return bom, frontmatter_raw, body


def _parse_markdown_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    split_res = _split_markdown_frontmatter(content)
    if not split_res:
        split_res = _split_markdown_frontmatter(content.strip())
    if not split_res:
        return {}, content.strip()

    _, frontmatter_raw, body = split_res

    meta: Dict[str, Any] = {}
    for line in frontmatter_raw.splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            key = k.strip()
            val_str = v.strip()
            if val_str.startswith("[") and val_str.endswith("]"):
                try:
                    parsed_val = json.loads(val_str)
                except json.JSONDecodeError:
                    parsed_val = val_str
            elif val_str.lower() == "true":
                parsed_val = True
            elif val_str.lower() == "false":
                parsed_val = False
            else:
                parsed_val = val_str.strip("\"'")
            meta[key] = parsed_val

    return meta, body.strip()


def _format_yaml_scalar(val: str) -> str:
    """Format a string value as a valid YAML scalar, escaping special characters as needed."""
    if not val:
        return '""'
    needs_quotes = (
        any(c in val for c in ':#{}[]|>&*!%@`"\n\r\t,?')
        or val.strip() != val
        or val.startswith(("-", "?", "@", "`", "%"))
        or val.lower() in ("true", "false", "null", "yes", "no", "on", "off")
    )
    if needs_quotes:
        escaped = (
            val.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    return val


def _update_markdown_frontmatter(content: str, updates: Dict[str, str]) -> str:
    """
    Targetedly update specific keys in markdown frontmatter while preserving all other keys,
    comments, formatting, and body.
    """
    split_res = _split_markdown_frontmatter(content)
    if not split_res:
        fm_lines = ["---"]
        for k, v in updates.items():
            fm_lines.append(f"{k}: {_format_yaml_scalar(v)}")
        fm_lines.append("---")
        return "\n".join(fm_lines) + "\n\n" + content.lstrip("\ufeff").lstrip()

    bom, frontmatter_raw, body = split_res

    fm_lines = frontmatter_raw.splitlines()
    remaining_updates = dict(updates)

    new_lines: List[str] = []
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        matched_key = None
        for key in list(remaining_updates.keys()):
            if re.match(rf"^{re.escape(key)}\s*:", line):
                matched_key = key
                break

        if matched_key:
            new_val = remaining_updates.pop(matched_key)
            formatted_val = _format_yaml_scalar(new_val)
            new_lines.append(f"{matched_key}: {formatted_val}")
            i += 1
            # Skip indented continuation lines or blank lines of previous scalar
            while i < len(fm_lines):
                next_line = fm_lines[i]
                if next_line.startswith((" ", "\t")):
                    i += 1
                elif next_line.strip() == "":
                    j = i + 1
                    while j < len(fm_lines) and fm_lines[j].strip() == "":
                        j += 1
                    if j < len(fm_lines) and fm_lines[j].startswith((" ", "\t")):
                        i = j + 1
                    else:
                        break
                else:
                    break
        else:
            new_lines.append(line)
            i += 1

    for key, val in remaining_updates.items():
        formatted_val = _format_yaml_scalar(val)
        if key == "name":
            insert_idx = 0
            while insert_idx < len(new_lines) and new_lines[
                insert_idx
            ].strip().startswith("#"):
                insert_idx += 1
            new_lines.insert(insert_idx, f"name: {formatted_val}")
        else:
            new_lines.append(f"{key}: {formatted_val}")

    joined_fm = "\n".join(new_lines).strip("\n")
    if joined_fm:
        return f"{bom}---\n{joined_fm}\n---\n\n{body.lstrip()}"
    return f"{bom}---\n---\n\n{body.lstrip()}"


def add_skill(
    aikito_dir: Path,
    home: Path,
    name: Optional[str] = None,
    description: Optional[str] = None,
    project_name: Optional[str] = None,
    projects: Optional[List[str]] = None,
    from_source: Optional[Union[str, Path]] = None,
    sync: bool = False,
) -> bool:
    """
    Create canonical Skill skeleton or import from external source, and register it in skills.toml or project agent.toml files.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    target_projects: List[str] = []
    if project_name:
        for p in project_name.split(","):
            p_clean = p.strip()
            if p_clean and p_clean not in target_projects:
                target_projects.append(p_clean)
    if projects:
        for p in projects:
            for sub_p in p.split(","):
                p_clean = sub_p.strip()
                if p_clean and p_clean not in target_projects:
                    target_projects.append(p_clean)

    # Pre-validate all target projects exist before modifying anything
    for proj in target_projects:
        proj_dir = aikito_dir / "projects" / proj
        agent_toml = proj_dir / "agent.toml"
        if not agent_toml.is_file():
            print(
                f"[ERROR] Project '{proj}' not found at {_display_path(agent_toml, home)}",
                file=sys.stderr,
            )
            return False

    source_path: Optional[Path] = None
    source_is_dir = False
    source_meta: Dict[str, Any] = {}
    source_body = ""

    if from_source is not None:
        source_path = Path(from_source).expanduser().resolve()
        if not source_path.exists():
            print(f"[ERROR] Source path does not exist: {from_source}", file=sys.stderr)
            return False

        if source_path.is_dir():
            source_is_dir = True
            source_skill_md = source_path / "SKILL.md"
            if not source_skill_md.is_file():
                print(
                    f"[ERROR] Source directory '{_display_path(source_path, home)}' does not contain a SKILL.md file.",
                    file=sys.stderr,
                )
                return False
        elif source_path.is_file():
            if source_path.suffix.lower() != ".md":
                print(
                    f"[ERROR] Source file '{_display_path(source_path, home)}' must be a markdown (.md) file.",
                    file=sys.stderr,
                )
                return False
            source_skill_md = source_path
        else:
            print(
                f"[ERROR] Source path is not a file or directory: {from_source}",
                file=sys.stderr,
            )
            return False

        try:
            raw_content = source_skill_md.read_text(encoding="utf-8")
            source_meta, source_body = _parse_markdown_frontmatter(raw_content)
        except Exception as exc:
            print(
                f"[ERROR] Failed to read source file '{_display_path(source_skill_md, home)}': {exc}",
                file=sys.stderr,
            )
            return False

        if not name or not name.strip():
            inferred_name = source_meta.get("name")
            if not inferred_name:
                if source_is_dir:
                    inferred_name = source_path.name
                else:
                    if source_path.stem.lower() == "skill":
                        inferred_name = source_path.parent.name
                    else:
                        inferred_name = source_path.stem
            name = str(inferred_name).strip() if inferred_name else ""

        if description is None or not description.strip():
            inferred_desc = source_meta.get("description")
            if inferred_desc:
                description = str(inferred_desc).strip()

    if not name or not name.strip():
        print(
            "[ERROR] Skill name is required. Please specify a name or provide a source via --from.",
            file=sys.stderr,
        )
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "skill")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    skills_root = aikito_dir / "skills"
    skill_dir = skills_root / name_clean
    skill_file = skill_dir / "SKILL.md"

    is_existing_canonical = False
    if skill_dir.exists():
        if not (skill_dir.is_dir() and skill_file.is_file()):
            print(
                f"[ERROR] Canonical skill path '{_display_path(skill_dir, home)}' exists but is not a valid skill directory (missing SKILL.md).",
                file=sys.stderr,
            )
            return False
        is_existing_canonical = True
        if from_source is not None:
            print(
                f"[ERROR] Skill '{name_clean}' already exists at {_display_path(skill_dir, home)}",
                file=sys.stderr,
            )
            return False
        if not target_projects:
            print(
                f"[ERROR] Skill '{name_clean}' already exists at {_display_path(skill_dir, home)}",
                file=sys.stderr,
            )
            return False

    desc_val = (description or f"Description for {name_clean} skill.").strip()
    title_val = _titleize(name_clean)

    if target_projects:
        # Check if already registered in all requested projects
        already_registered: List[str] = []
        pending_projects: List[str] = []
        for proj in target_projects:
            agent_toml = aikito_dir / "projects" / proj / "agent.toml"
            try:
                proj_data = tomllib.loads(agent_toml.read_text(encoding="utf-8"))
                p_skills = proj_data.get("skills", [])
                if isinstance(p_skills, list) and name_clean in p_skills:
                    already_registered.append(proj)
                else:
                    pending_projects.append(proj)
            except Exception as exc:
                print(f"[ERROR] Failed to parse {agent_toml}: {exc}", file=sys.stderr)
                return False

        if not pending_projects:
            if len(target_projects) == 1:
                print(
                    f"[ERROR] Skill '{name_clean}' already exists and is already registered in project '{target_projects[0]}'",
                    file=sys.stderr,
                )
            else:
                print(
                    f"[ERROR] Skill '{name_clean}' already exists and is already registered in all specified projects: {', '.join(target_projects)}",
                    file=sys.stderr,
                )
            return False

        # Phase 1: Preflight planning and semantic integrity verification (in-memory)
        planned_project_updates: List[Tuple[Path, str, str, str]] = []
        for proj in pending_projects:
            agent_toml = aikito_dir / "projects" / proj / "agent.toml"
            try:
                original_text = agent_toml.read_text(encoding="utf-8")
                proj_data = tomllib.loads(original_text)
                existing_skills = proj_data.get("skills", [])
                if not isinstance(existing_skills, list):
                    existing_skills = []
                new_skills = [str(s) for s in existing_skills] + [name_clean]
                new_agent_toml_content = _update_skills_in_toml(
                    original_text, new_skills
                )

                new_proj_data = tomllib.loads(new_agent_toml_content)
                for k, v in proj_data.items():
                    if k != "skills" and new_proj_data.get(k) != v:
                        raise ValueError(
                            f"Semantic integrity check failed for key '{k}'"
                        )
                if new_proj_data.get("skills") != new_skills:
                    raise ValueError("Semantic integrity check failed for skills list")

                planned_project_updates.append(
                    (agent_toml, original_text, new_agent_toml_content, proj)
                )
            except Exception as exc:
                print(
                    f"[ERROR] Failed to plan update for project '{proj}' config: {exc}",
                    file=sys.stderr,
                )
                return False

        # Phase 2: Execute writes with transactional rollback
        created_skill_dir = not is_existing_canonical
        written_project_files: List[Tuple[Path, str]] = []

        try:
            if not is_existing_canonical:
                if source_path is not None:
                    if source_is_dir:
                        shutil.copytree(
                            source_path,
                            skill_dir,
                            ignore=shutil.ignore_patterns(
                                ".git", "__pycache__", "*.pyc", ".DS_Store"
                            ),
                        )
                    else:
                        skill_dir.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_path, skill_file)

                    copied_text = skill_file.read_text(encoding="utf-8")
                    c_meta, _ = _parse_markdown_frontmatter(copied_text)
                    fm_updates: Dict[str, str] = {}
                    if c_meta.get("name") != name_clean:
                        fm_updates["name"] = name_clean
                    if description is not None and description.strip():
                        if c_meta.get("description") != description.strip():
                            fm_updates["description"] = description.strip()
                    elif "description" not in c_meta:
                        fm_updates["description"] = desc_val

                    if fm_updates:
                        updated_content = _update_markdown_frontmatter(
                            copied_text, fm_updates
                        )
                        _atomic_write_text(skill_file, updated_content)
                else:
                    skill_content = f"""---
name: {name_clean}
description: {desc_val}
---

# {title_val}

## Overview

Describe what this skill does and when agents should use it.
"""
                    skill_dir.mkdir(parents=True, exist_ok=True)
                    _atomic_write_text(skill_file, skill_content)

            # Write project configs atomically
            for agent_toml, original_text, new_content, proj in planned_project_updates:
                _atomic_write_text(agent_toml, new_content, encoding="utf-8")
                written_project_files.append((agent_toml, original_text))
                print(
                    f"[UPDATE FILE] {_display_path(agent_toml, home)} (registered skill for project '{proj}')"
                )

        except Exception as exc:
            # Full rollback of all planned project configs
            for agent_toml, original_text, _, _ in planned_project_updates:
                try:
                    _atomic_write_text(agent_toml, original_text, encoding="utf-8")
                except Exception as rb_exc:
                    print(
                        f"[ERROR] Failed to rollback project config {_display_path(agent_toml, home)}: {rb_exc}",
                        file=sys.stderr,
                    )
            # Remove newly created skill directory if it was created during this run
            if created_skill_dir and skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to write skill or project config: {exc}",
                file=sys.stderr,
            )
            return False

        if created_skill_dir:
            print(f"[CREATE DIR] {_display_path(skill_dir, home)}")
            print(f"[CREATE FILE] {_display_path(skill_file, home)}")
        else:
            print(
                f"[INFO] Using existing canonical skill '{name_clean}' at {_display_path(skill_dir, home)}"
            )

        for proj in already_registered:
            print(
                f"[INFO] Skill '{name_clean}' was already registered in project '{proj}'."
            )

        proj_names_str = ", ".join(f"'{p}'" for p in pending_projects)
        print(
            f"\n[SUCCESS] Added skill '{name_clean}' to project(s): {proj_names_str}."
        )
        print("💡 Next steps:")
        step = 1
        if created_skill_dir and source_path is None:
            print(
                f"  {step}. Update instructions in {_display_path(skill_file, home)} (or run 'aikito edit skill {name_clean}')"
            )
            step += 1
        sync_cmds = " && ".join(f"aikito sync project {p}" for p in pending_projects)
        print(f"  {step}. Synchronize project(s): {sync_cmds}")

        if sync:
            from .cli import sync_project_by_name

            for proj in target_projects:
                if not sync_project_by_name(aikito_dir, home, proj):
                    return False
        return True

    # Global skill registration in skills.toml
    skills_toml = aikito_dir / "skills.toml"
    existing_global_skills: List[str] = []
    original_skills_toml_text = ""
    if skills_toml.is_file():
        try:
            original_skills_toml_text = skills_toml.read_text(encoding="utf-8")
            data = tomllib.loads(original_skills_toml_text)
            raw_skills = data.get("skills", [])
            if isinstance(raw_skills, list):
                existing_global_skills = [str(s) for s in raw_skills]
        except Exception as exc:
            print(f"[ERROR] Failed to parse {skills_toml}: {exc}", file=sys.stderr)
            return False

    if name_clean in existing_global_skills:
        print(
            f"[ERROR] Skill '{name_clean}' is already registered in skills.toml",
            file=sys.stderr,
        )
        return False

    new_global_skills = existing_global_skills + [name_clean]
    skills_toml_content = _update_skills_in_toml(
        original_skills_toml_text, new_global_skills
    )

    # Pre-validate TOML syntax before touching disk
    try:
        new_global_data = tomllib.loads(skills_toml_content)
    except Exception as exc:
        print(
            f"[ERROR] Generated skills.toml is invalid TOML: {exc}",
            file=sys.stderr,
        )
        return False

    if new_global_data.get("skills") != new_global_skills:
        print(
            "[ERROR] Semantic integrity check failed for skills.toml",
            file=sys.stderr,
        )
        return False

    try:
        if source_path is not None:
            if source_is_dir:
                shutil.copytree(
                    source_path,
                    skill_dir,
                    ignore=shutil.ignore_patterns(
                        ".git", "__pycache__", "*.pyc", ".DS_Store"
                    ),
                )
            else:
                skill_dir.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_path, skill_file)

            copied_text = skill_file.read_text(encoding="utf-8")
            c_meta, _ = _parse_markdown_frontmatter(copied_text)
            fm_updates: Dict[str, str] = {}
            if c_meta.get("name") != name_clean:
                fm_updates["name"] = name_clean
            if description is not None and description.strip():
                if c_meta.get("description") != description.strip():
                    fm_updates["description"] = description.strip()
            elif "description" not in c_meta:
                fm_updates["description"] = desc_val

            if fm_updates:
                updated_content = _update_markdown_frontmatter(copied_text, fm_updates)
                _atomic_write_text(skill_file, updated_content)
        else:
            skill_content = f"""---
name: {name_clean}
description: {desc_val}
---

# {title_val}

## Overview

Describe what this skill does and when agents should use it.
"""
            skill_dir.mkdir(parents=True, exist_ok=True)
            _atomic_write_text(skill_file, skill_content)

        _atomic_write_text(skills_toml, skills_toml_content, encoding="utf-8")
    except Exception as exc:
        if original_skills_toml_text:
            try:
                _atomic_write_text(
                    skills_toml, original_skills_toml_text, encoding="utf-8"
                )
            except Exception:
                pass
        shutil.rmtree(skill_dir, ignore_errors=True)
        print(f"[ERROR] Failed to write global skill: {exc}", file=sys.stderr)
        return False

    print(f"[CREATE DIR] {_display_path(skill_dir, home)}")
    print(f"[CREATE FILE] {_display_path(skill_file, home)}")
    print(f"[UPDATE FILE] {_display_path(skills_toml, home)} (registered global skill)")
    print(f"\n[SUCCESS] Added global skill '{name_clean}'.")
    print("💡 Next steps:")
    step = 1
    if source_path is None:
        print(
            f"  {step}. Update instructions in {_display_path(skill_file, home)} (or run 'aikito edit skill {name_clean}')"
        )
        step += 1
    print(f"  {step}. Synchronize to agents: aikito sync global")

    if sync:
        from .cli import sync_global_resources
        from .compat import require_symlink_support

        require_symlink_support()
        if not sync_global_resources(aikito_dir, home):
            return False
    return True


def add_subagent(
    aikito_dir: Path,
    home: Path,
    name: str,
    description: Optional[str] = None,
    agents: Optional[List[str]] = None,
) -> bool:
    """
    Create canonical Subagent instructions (subagents/<name>.md) and register it in subagents.toml.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "subagent")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    subagents_dir = aikito_dir / "subagents"
    subagent_file = subagents_dir / f"{name_clean}.md"
    subagents_toml = aikito_dir / "subagents.toml"

    if not subagents_toml.is_file():
        print(
            f"[ERROR] subagents.toml not found at {_display_path(subagents_toml, home)}",
            file=sys.stderr,
        )
        return False

    try:
        with subagents_toml.open("rb") as f:
            data = tomllib.load(f)
    except Exception as exc:
        print(f"[ERROR] Failed to parse {subagents_toml}: {exc}", file=sys.stderr)
        return False

    existing_subagents = data.get("subagents", {})
    if isinstance(existing_subagents, dict) and name_clean in existing_subagents:
        print(
            f"[ERROR] Subagent '{name_clean}' is already registered in subagents.toml",
            file=sys.stderr,
        )
        return False

    if subagent_file.exists():
        print(
            f"[ERROR] Subagent instructions file already exists at {_display_path(subagent_file, home)}",
            file=sys.stderr,
        )
        return False

    target_agents = (
        agents if agents is not None and len(agents) > 0 else DEFAULT_SUBAGENT_AGENTS
    )
    desc_val = (description or f"Subagent {name_clean}.").strip()
    title_val = _titleize(name_clean)

    subagent_instructions = f"""# {title_val}

Add developer instructions for the {name_clean} subagent here.
"""

    # Format new subagent block for subagents.toml
    agents_json = json.dumps(target_agents, ensure_ascii=False)
    subagent_block = f"""
[subagents.{name_clean}]
description = {json.dumps(desc_val, ensure_ascii=False)}
agents = {agents_json}
"""

    existing_toml_content = subagents_toml.read_text(encoding="utf-8")
    new_toml_content = existing_toml_content.rstrip() + "\n" + subagent_block

    # Validate resulting TOML syntax
    try:
        tomllib.loads(new_toml_content)
    except Exception as exc:
        print(
            f"[ERROR] Generated subagents.toml is invalid TOML: {exc}", file=sys.stderr
        )
        return False

    try:
        subagents_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(subagent_file, subagent_instructions, encoding="utf-8")
        _atomic_write_text(subagents_toml, new_toml_content, encoding="utf-8")
    except Exception as exc:
        if subagent_file.exists():
            subagent_file.unlink(missing_ok=True)
        if existing_toml_content:
            try:
                _atomic_write_text(
                    subagents_toml, existing_toml_content, encoding="utf-8"
                )
            except Exception:
                pass
        print(f"[ERROR] Failed to write subagent: {exc}", file=sys.stderr)
        return False

    print(f"[CREATE FILE] {_display_path(subagent_file, home)}")
    print(
        f"[UPDATE FILE] {_display_path(subagents_toml, home)} (registered subagent '{name_clean}')"
    )
    print(f"\n[SUCCESS] Added subagent '{name_clean}'.")
    print("💡 Next steps:")
    print(
        f"  1. Update instructions in {_display_path(subagent_file, home)} (or run 'aikito edit subagent {name_clean}')"
    )
    print("  2. Synchronize to agents: aikito sync subagents")
    return True


def add_mcp(
    aikito_dir: Path,
    home: Path,
    name: str,
    transport: Optional[str] = None,
    command: Optional[str] = None,
    url: Optional[str] = None,
    agents: Optional[List[str]] = None,
) -> bool:
    """
    Create canonical MCP configuration (mcps/<name>.toml) with minimal valid schema.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "mcp")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    # Validate argument combinations and transport mutual exclusions
    if command and url:
        print("[ERROR] Cannot specify both --command and --url.", file=sys.stderr)
        return False

    if transport == "stdio" and url:
        print(
            "[ERROR] Cannot specify --url when --transport is 'stdio'.", file=sys.stderr
        )
        return False

    if transport == "remote" and command:
        print(
            "[ERROR] Cannot specify --command when --transport is 'remote'.",
            file=sys.stderr,
        )
        return False

    if transport == "remote" and not url:
        print(
            "[ERROR] --url is required when --transport is 'remote'.", file=sys.stderr
        )
        return False

    mcps_dir = aikito_dir / "mcps"
    mcp_file = mcps_dir / f"{name_clean}.toml"

    if mcp_file.exists():
        print(
            f"[ERROR] MCP server config already exists at {_display_path(mcp_file, home)}",
            file=sys.stderr,
        )
        return False

    is_remote = transport == "remote" or (transport is None and url is not None)

    target_agents = (
        agents if agents is not None and len(agents) > 0 else DEFAULT_MCP_AGENTS
    )
    agents_json = json.dumps(target_agents, ensure_ascii=False)

    if is_remote:
        mcp_content = f"""transport = "remote"
url = {json.dumps(url, ensure_ascii=False)}
agents = {agents_json}
"""
    else:
        cmd_val = command if command else "npx"
        mcp_content = f"""command = {json.dumps(cmd_val, ensure_ascii=False)}
args = []
agents = {agents_json}
"""

    # Validate TOML syntax
    try:
        tomllib.loads(mcp_content)
    except Exception as exc:
        print(f"[ERROR] Generated MCP config is invalid TOML: {exc}", file=sys.stderr)
        return False

    try:
        mcps_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(mcp_file, mcp_content, encoding="utf-8")
    except Exception as exc:
        if mcp_file.exists():
            mcp_file.unlink(missing_ok=True)
        print(f"[ERROR] Failed to write MCP config: {exc}", file=sys.stderr)
        return False

    print(f"[CREATE FILE] {_display_path(mcp_file, home)}")
    print(f"\n[SUCCESS] Added MCP server '{name_clean}'.")
    print("💡 Next steps:")
    print(
        f"  1. Configure server in {_display_path(mcp_file, home)} (or run 'aikito edit mcp {name_clean}')"
    )
    print("  2. Synchronize to agents: aikito sync mcp")
    return True
