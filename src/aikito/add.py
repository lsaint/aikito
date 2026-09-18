"""
Resource addition module for Aikito.
Provides lightweight canonical skeleton creation and registration for skills, subagents, and MCP servers.
"""

from dataclasses import dataclass, field
import io
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
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from . import mcp
from .compat import safe_relative_path
from .subagent import KNOWN_PLATFORM_FIELDS
from .templating import BUNDLED_SKILL_NAMES


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


def _parse_yaml_value(val_str: str) -> Any:
    val_str = val_str.strip()
    if not val_str:
        return ""
    if val_str.startswith("[") and val_str.endswith("]"):
        try:
            return json.loads(val_str)
        except Exception:
            inner = val_str[1:-1].strip()
            if not inner:
                return []
            return [_parse_yaml_value(item.strip()) for item in inner.split(",")]
    if val_str.startswith("{") and val_str.endswith("}"):
        try:
            return json.loads(val_str)
        except Exception:
            inner = val_str[1:-1].strip()
            if not inner:
                return {}
            res: Dict[str, Any] = {}
            for pair in inner.split(","):
                if ":" in pair:
                    pk, pv = pair.split(":", 1)
                    res[pk.strip().strip("\"'")] = _parse_yaml_value(pv.strip())
            return res
    if val_str.lower() == "true":
        return True
    if val_str.lower() == "false":
        return False
    if val_str.lower() in ("null", "~"):
        return None
    return val_str.strip("\"'")


def _parse_markdown_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    split_res = _split_markdown_frontmatter(content)
    if not split_res:
        split_res = _split_markdown_frontmatter(content.strip())
    if not split_res:
        return {}, content.strip()

    _, frontmatter_raw, body = split_res

    meta: Dict[str, Any] = {}
    lines = frontmatter_raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if not line.startswith((" ", "\t")) and ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            val_str = v.strip()

            is_block_scalar = val_str in ("|", ">", "|-", ">-", "|+", ">+")
            is_folded = val_str.startswith(">")

            if val_str and not is_block_scalar:
                meta[key] = _parse_yaml_value(val_str)
                i += 1
            else:
                i += 1
                child_lines: List[str] = []
                while i < len(lines):
                    next_line = lines[i]
                    if next_line.strip() == "":
                        child_lines.append(next_line)
                        i += 1
                        continue
                    if next_line.startswith((" ", "\t")):
                        child_lines.append(next_line)
                        i += 1
                    else:
                        break

                non_empty = [
                    c
                    for c in child_lines
                    if c.strip() and not c.strip().startswith("#")
                ]
                if is_block_scalar:
                    if is_folded:
                        meta[key] = " ".join(c.strip() for c in non_empty)
                    else:
                        meta[key] = "\n".join(c.strip() for c in non_empty)
                elif not non_empty:
                    meta[key] = ""
                elif key in KNOWN_PLATFORM_FIELDS and any(":" in c for c in non_empty):
                    sub_dict: Dict[str, Any] = {}
                    for c in non_empty:
                        if ":" in c:
                            sub_k, sub_v = c.split(":", 1)
                            sub_dict[sub_k.strip()] = _parse_yaml_value(sub_v)
                    meta[key] = sub_dict
                elif any(c.strip().startswith("- ") for c in non_empty):
                    items: List[Any] = []
                    for c in non_empty:
                        s = c.strip()
                        if s.startswith("- "):
                            items.append(_parse_yaml_value(s[2:]))
                    meta[key] = items
                else:
                    meta[key] = " ".join(c.strip() for c in non_empty)
        else:
            i += 1

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


class _SkillImportTransaction:
    """Stage a complete Skill snapshot and swap it into place with rollback."""

    def __init__(
        self,
        source_path: Path,
        source_is_dir: bool,
        target_dir: Path,
        name: str,
        description: Optional[str],
        default_description: str,
    ) -> None:
        self.source_path = source_path
        self.source_is_dir = source_is_dir
        self.target_dir = target_dir
        self.name = name
        self.description = description
        self.default_description = default_description
        self.had_target = target_dir.exists()
        self.temp_root: Optional[Path] = None
        self.staged_dir: Optional[Path] = None
        self.backup_dir: Optional[Path] = None
        self.applied = False

    def prepare(self) -> None:
        self.target_dir.parent.mkdir(parents=True, exist_ok=True)
        self.temp_root = Path(
            tempfile.mkdtemp(prefix=f".{self.name}-import-", dir=self.target_dir.parent)
        )
        self.staged_dir = self.temp_root / "staged"
        self.backup_dir = self.temp_root / "original"

        try:
            if self.source_is_dir:
                ignored_files = shutil.ignore_patterns(
                    ".git", "__pycache__", "*.pyc", ".DS_Store"
                )

                def ignore_import_artifacts(path: str, names: List[str]) -> set[str]:
                    ignored = set(ignored_files(path, names))
                    if (
                        self.temp_root is not None
                        and Path(path) == self.temp_root.parent
                        and self.temp_root.name in names
                    ):
                        ignored.add(self.temp_root.name)
                    return ignored

                shutil.copytree(
                    self.source_path,
                    self.staged_dir,
                    ignore=ignore_import_artifacts,
                )
            else:
                self.staged_dir.mkdir(parents=True)
                shutil.copy2(self.source_path, self.staged_dir / "SKILL.md")

            skill_file = self.staged_dir / "SKILL.md"
            copied_text = skill_file.read_text(encoding="utf-8")
            copied_meta, _ = _parse_markdown_frontmatter(copied_text)
            updates: Dict[str, str] = {}
            if copied_meta.get("name") != self.name:
                updates["name"] = self.name
            if self.description is not None and self.description.strip():
                if copied_meta.get("description") != self.description.strip():
                    updates["description"] = self.description.strip()
            elif "description" not in copied_meta:
                updates["description"] = self.default_description

            if updates:
                _atomic_write_text(
                    skill_file,
                    _update_markdown_frontmatter(copied_text, updates),
                )
        except Exception:
            self.discard()
            raise

    def apply(self) -> None:
        if self.staged_dir is None or self.backup_dir is None:
            raise RuntimeError("Skill import transaction was not prepared")

        try:
            if self.had_target:
                self.target_dir.replace(self.backup_dir)
            self.staged_dir.replace(self.target_dir)
            self.applied = True
        except Exception:
            if self.had_target and self.backup_dir.exists():
                self.backup_dir.replace(self.target_dir)
            raise

    def rollback(self) -> None:
        if self.applied:
            if self.target_dir.exists():
                shutil.rmtree(self.target_dir, ignore_errors=True)
            if self.had_target and self.backup_dir and self.backup_dir.exists():
                self.backup_dir.replace(self.target_dir)
            self.applied = False
        self.discard()

    def discard(self) -> None:
        if self.temp_root is not None:
            shutil.rmtree(self.temp_root, ignore_errors=True)

    def commit(self) -> None:
        self.discard()


def add_skill(
    aikito_dir: Path,
    home: Path,
    name: Optional[str] = None,
    description: Optional[str] = None,
    project_name: Optional[str] = None,
    projects: Optional[List[str]] = None,
    from_source: Optional[Union[str, Path]] = None,
    sync: bool = False,
    force: bool = False,
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

    if force and from_source is None:
        print("[ERROR] --force requires --from when adding a skill.", file=sys.stderr)
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
            print(f"[ERROR] Project '{proj}' not found.", file=sys.stderr)
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
        if from_source is not None and not force:
            print(
                f"[ERROR] Skill '{name_clean}' already exists at {_display_path(skill_dir, home)}",
                file=sys.stderr,
            )
            return False
        if not target_projects and not (from_source is not None and force):
            print(
                f"[ERROR] Skill '{name_clean}' already exists at {_display_path(skill_dir, home)}",
                file=sys.stderr,
            )
            return False

    if name_clean in BUNDLED_SKILL_NAMES:
        if from_source is not None:
            print(
                f"[ERROR] Cannot overwrite bundled system skill '{name_clean}'.",
                file=sys.stderr,
            )
            return False
        if not is_existing_canonical:
            print(
                f"[ERROR] Cannot create custom skill with reserved bundled system skill name '{name_clean}'.",
                file=sys.stderr,
            )
            return False
        if target_projects:
            print(
                f"[INFO] '{name_clean}' is a built-in skill and already active globally."
            )

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
                print(
                    f"[ERROR] Failed to read configuration for project '{proj}': {exc}",
                    file=sys.stderr,
                )
                return False

        updating_import = is_existing_canonical and from_source is not None and force
        if is_existing_canonical and not pending_projects and not updating_import:
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
                new_skills = list(dict.fromkeys(list(existing_skills) + [name_clean]))
                new_agent_toml_content = _update_skills_in_toml(
                    original_text, new_skills
                )
                # Pre-validate TOML syntax before touching disk
                new_proj_data = tomllib.loads(new_agent_toml_content)
                for k, v in proj_data.items():
                    if k != "skills" and new_proj_data.get(k) != v:
                        raise ValueError(
                            f"Semantic integrity check failed for key '{k}'"
                        )
                if new_proj_data.get("skills") != new_skills:
                    raise ValueError(f"Semantic check failed for project '{proj}'")
                planned_project_updates.append(
                    (agent_toml, original_text, new_agent_toml_content, proj)
                )
            except Exception as exc:
                print(
                    f"[ERROR] Failed to update configuration for project '{proj}': {exc}",
                    file=sys.stderr,
                )
                return False

        import_transaction: Optional[_SkillImportTransaction] = None
        if source_path is not None:
            import_transaction = _SkillImportTransaction(
                source_path=source_path,
                source_is_dir=source_is_dir,
                target_dir=skill_dir,
                name=name_clean,
                description=description,
                default_description=desc_val,
            )
            try:
                import_transaction.prepare()
            except Exception as exc:
                print(f"[ERROR] Failed to prepare skill import: {exc}", file=sys.stderr)
                return False

        # Phase 2: Execute writes with transactional rollback
        created_skill_dir = not is_existing_canonical

        try:
            if import_transaction is not None:
                import_transaction.apply()
            elif not is_existing_canonical:
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
                        f"[ERROR] Failed to rollback configuration for project '{proj}': {rb_exc}",
                        file=sys.stderr,
                    )
            if import_transaction is not None:
                import_transaction.rollback()
            elif created_skill_dir and skill_dir.exists():
                shutil.rmtree(skill_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to write skill or project configuration: {exc}",
                file=sys.stderr,
            )
            return False

        if import_transaction is not None:
            import_transaction.commit()

        if updating_import:
            print(f"[UPDATE DIR] {_display_path(skill_dir, home)}")
            print(f"[UPDATE FILE] {_display_path(skill_file, home)}")
        elif created_skill_dir:
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

        if pending_projects:
            proj_names_str = ", ".join(f"'{p}'" for p in pending_projects)
            if updating_import:
                print(
                    f"\n[SUCCESS] Updated skill '{name_clean}' for project(s): {proj_names_str}."
                )
            else:
                print(
                    f"\n[SUCCESS] Added skill '{name_clean}' to project(s): {proj_names_str}."
                )
        else:
            registered_projects = ", ".join(f"'{p}'" for p in target_projects)
            print(
                f"\n[SUCCESS] Updated skill '{name_clean}' for project(s): {registered_projects}."
            )
        print("💡 Next steps:")
        step = 1
        if created_skill_dir and source_path is None:
            print(
                f"  {step}. Update instructions in {_display_path(skill_file, home)} (or run 'aikito edit skill {name_clean}')"
            )
            step += 1
        sync_targets = target_projects if updating_import else pending_projects
        sync_cmds = " && ".join(f"aikito sync project {p}" for p in sync_targets)
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
            print(
                f"[ERROR] Failed to read global skills configuration: {exc}",
                file=sys.stderr,
            )
            return False

    already_registered_global = name_clean in existing_global_skills
    updating_import = is_existing_canonical and from_source is not None and force
    if is_existing_canonical and already_registered_global and not updating_import:
        print(
            f"[ERROR] Skill '{name_clean}' is already registered globally.",
            file=sys.stderr,
        )
        return False

    new_global_skills = (
        existing_global_skills
        if already_registered_global
        else existing_global_skills + [name_clean]
    )
    skills_toml_content = _update_skills_in_toml(
        original_skills_toml_text, new_global_skills
    )

    # Pre-validate TOML syntax before touching disk
    try:
        new_global_data = tomllib.loads(skills_toml_content)
    except Exception as exc:
        print(
            f"[ERROR] Failed to update global skills configuration: {exc}",
            file=sys.stderr,
        )
        return False

    if new_global_data.get("skills") != new_global_skills:
        print(
            "[ERROR] Failed to verify global skills configuration.",
            file=sys.stderr,
        )
        return False

    import_transaction: Optional[_SkillImportTransaction] = None
    if source_path is not None:
        import_transaction = _SkillImportTransaction(
            source_path=source_path,
            source_is_dir=source_is_dir,
            target_dir=skill_dir,
            name=name_clean,
            description=description,
            default_description=desc_val,
        )
        try:
            import_transaction.prepare()
        except Exception as exc:
            print(f"[ERROR] Failed to prepare skill import: {exc}", file=sys.stderr)
            return False

    try:
        if import_transaction is not None:
            import_transaction.apply()
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
        if import_transaction is not None:
            import_transaction.rollback()
        else:
            shutil.rmtree(skill_dir, ignore_errors=True)
        print(f"[ERROR] Failed to write global skill: {exc}", file=sys.stderr)
        return False

    if import_transaction is not None:
        import_transaction.commit()

    if updating_import:
        print(f"[UPDATE DIR] {_display_path(skill_dir, home)}")
        print(f"[UPDATE FILE] {_display_path(skill_file, home)}")
    else:
        print(f"[CREATE DIR] {_display_path(skill_dir, home)}")
        print(f"[CREATE FILE] {_display_path(skill_file, home)}")
    if not already_registered_global:
        print(
            f"[UPDATE FILE] {_display_path(skills_toml, home)} (registered global skill)"
        )
    action = "Updated" if updating_import else "Added"
    print(f"\n[SUCCESS] {action} global skill '{name_clean}'.")
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


def _format_toml_key(key: str) -> str:
    is_bare = all(c.isalnum() or c in ("_", "-") for c in key) if key else False
    if is_bare:
        return key
    return json.dumps(key, ensure_ascii=False)


def _format_toml_value(val: Any) -> str:
    if isinstance(val, str):
        return json.dumps(val, ensure_ascii=False)
    elif isinstance(val, bool):
        return "true" if val else "false"
    elif isinstance(val, (int, float)):
        return str(val)
    elif isinstance(val, list):
        items = [_format_toml_value(x) for x in val]
        return f"[{', '.join(items)}]"
    elif isinstance(val, dict):
        pairs = []
        for k in sorted(val.keys()):
            k_repr = _format_toml_key(str(k))
            v_repr = _format_toml_value(val[k])
            pairs.append(f"{k_repr} = {v_repr}")
        return f"{{ {', '.join(pairs)} }}"
    else:
        return json.dumps(str(val), ensure_ascii=False)


def _remove_subagent_from_toml(text: str, name: str) -> str:
    pattern = re.compile(
        rf"(?m)^[ \t]*\[subagents\.(?:{re.escape(name)}|{re.escape(json.dumps(name))})\][ \t]*(?:#.*)?$"
    )
    match = pattern.search(text)
    if match is None:
        return text

    header_pattern = re.compile(r"(?m)^[ \t]*\[([^\]]+)\][ \t]*(?:#.*)?$")
    end = len(text)
    for next_match in header_pattern.finditer(text, match.end()):
        hdr = next_match.group(1).strip()
        prefix = f"subagents.{name}."
        prefix_quoted = f'subagents."{name}".'
        if (
            hdr == f"subagents.{name}"
            or hdr.startswith(prefix)
            or hdr == f'subagents."{name}"'
            or hdr.startswith(prefix_quoted)
        ):
            continue
        end = next_match.start()
        break

    new_text = text[: match.start()] + text[end:]
    cleaned = re.sub(r"\n{3,}", "\n\n", new_text)
    try:
        chk = tomllib.loads(cleaned)
        if "subagents" not in chk:
            cleaned = (cleaned.rstrip() + "\n\n[subagents]\n").lstrip("\n")
    except Exception:
        pass
    return cleaned


@dataclass
class ImportedSubagent:
    name: Optional[str] = None
    description: Optional[str] = None
    instructions: str = ""
    target_agents: Optional[List[str]] = None
    explicit_platform_configs: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    top_level_options: Dict[str, Any] = field(default_factory=dict)
    source_path: Optional[Path] = None


def _resolve_subagent_source(
    from_source: Union[str, Path],
    name: Optional[str],
    description: Optional[str],
    home: Path,
) -> ImportedSubagent:
    """
    Resolve and parse an external subagent source (file or directory).
    Extracts instructions, metadata, target agents, and platform configurations.
    """
    from .subagent import KNOWN_PLATFORM_FIELDS, validate_platform_opts

    source_path = Path(from_source).expanduser().resolve()
    if not source_path.exists():
        raise ValueError(f"Source path does not exist: {from_source}")

    if source_path.is_file():
        if not source_path.name.endswith(".md"):
            raise ValueError(
                f"Source file '{_display_path(source_path, home)}' must be a markdown (.md) file."
            )
        source_md_file = source_path
    elif source_path.is_dir():
        candidates: List[Path] = []
        if name and name.strip():
            candidates.append(source_path / f"{name.strip()}.md")
        candidates.extend(
            [
                source_path / "instructions.md",
                source_path / "prompt.md",
                source_path / "subagent.md",
                source_path / f"{source_path.name}.md",
            ]
        )
        source_md_file = None
        for cand in candidates:
            if cand.is_file():
                source_md_file = cand
                break
        if source_md_file is None:
            md_files = sorted(
                [
                    f
                    for f in source_path.iterdir()
                    if f.is_file() and f.name.endswith(".md")
                ]
            )
            if len(md_files) == 1:
                source_md_file = md_files[0]
            elif len(md_files) > 1:
                raise ValueError(
                    f"Multiple markdown files found in '{_display_path(source_path, home)}'. Please specify the file directly with --from."
                )
            else:
                raise ValueError(
                    f"Source directory '{_display_path(source_path, home)}' does not contain a subagent markdown file."
                )
    else:
        raise ValueError(f"Source path is not a file or directory: {from_source}")

    try:
        raw_content = source_md_file.read_text(encoding="utf-8")
        source_meta, source_body = _parse_markdown_frontmatter(raw_content)
    except Exception as exc:
        raise ValueError(
            f"Failed to read source file '{_display_path(source_md_file, home)}': {exc}"
        ) from exc

    subagent_instructions = source_body.strip()
    if not subagent_instructions:
        raise ValueError(
            f"Source file '{_display_path(source_md_file, home)}' does not contain any instructions."
        )
    subagent_instructions += "\n"

    # Name inference
    inferred_name = None
    if name and name.strip():
        inferred_name = name.strip()
    elif source_meta.get("name"):
        inferred_name = str(source_meta["name"]).strip()
    else:
        if source_path.is_dir():
            inferred_name = source_path.name
        elif source_md_file.name.endswith(".agent.md"):
            inferred_name = source_md_file.name[: -len(".agent.md")]
        elif source_md_file.stem.lower() in (
            "instructions",
            "prompt",
            "subagent",
            "agent",
        ):
            inferred_name = source_md_file.parent.name
        else:
            inferred_name = source_md_file.stem

    # Description inference
    inferred_desc = None
    if description and description.strip():
        inferred_desc = description.strip()
    elif source_meta.get("description"):
        inferred_desc = str(source_meta["description"]).strip()

    # Agents inference
    source_agents = None
    if isinstance(source_meta.get("agents"), list) and source_meta["agents"]:
        source_agents = [
            str(a).strip() for a in source_meta["agents"] if str(a).strip()
        ]

    # Explicit platform tables in frontmatter (e.g. claude-code: { ... })
    explicit_platform_configs: Dict[str, Dict[str, Any]] = {}
    for plat in KNOWN_PLATFORM_FIELDS:
        if plat in source_meta and isinstance(source_meta[plat], dict):
            validated = validate_platform_opts(
                plat, inferred_name or "subagent", source_meta[plat]
            )
            explicit_platform_configs[plat] = validated

    # Top-level platform options (e.g. tools, model, user-invocable, etc.)
    top_level_opts: Dict[str, Any] = {}
    all_known_platform_fields = set().union(*KNOWN_PLATFORM_FIELDS.values()) - {
        "name",
        "description",
    }
    for k, v in source_meta.items():
        if k in ("name", "description", "agents") or k in KNOWN_PLATFORM_FIELDS:
            continue
        if k in all_known_platform_fields:
            top_level_opts[k] = v

    return ImportedSubagent(
        name=inferred_name,
        description=inferred_desc,
        instructions=subagent_instructions,
        target_agents=source_agents,
        explicit_platform_configs=explicit_platform_configs,
        top_level_options=top_level_opts,
        source_path=source_md_file,
    )


def _render_subagent_block(
    name: str,
    description: str,
    agents: List[str],
    platform_configs: Dict[str, Dict[str, Any]],
) -> str:
    """Render a clean TOML subagents block including platform sub-tables."""
    safe_key = _format_toml_key(name)
    header = f"[subagents.{safe_key}]"
    sub_lines = [
        f"\n{header}",
        f"description = {_format_toml_value(description)}",
        f"agents = {_format_toml_value(agents)}",
    ]
    for agent_name, options in sorted(platform_configs.items()):
        if options:
            sub_lines.append(f"\n[{header[1:-1]}.{_format_toml_key(agent_name)}]")
            for key, value in sorted(options.items()):
                sub_lines.append(
                    f"{_format_toml_key(key)} = {_format_toml_value(value)}"
                )

    return "\n".join(sub_lines) + "\n"


def add_subagent(
    aikito_dir: Path,
    home: Path,
    name: Optional[str] = None,
    description: Optional[str] = None,
    agents: Optional[List[str]] = None,
    from_source: Optional[Union[str, Path]] = None,
    sync: bool = False,
    force: bool = False,
) -> bool:
    """
    Create canonical Subagent instructions (subagents/<name>.md) or import from external source,
    and register it in subagents.toml.
    """
    from .subagent import (
        SubagentConfigError,
        sync_subagent_configs,
        validate_platform_opts,
    )

    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    if force and from_source is None:
        print(
            "[ERROR] --force requires --from when adding a subagent.",
            file=sys.stderr,
        )
        return False

    imported: Optional[ImportedSubagent] = None
    if from_source is not None:
        try:
            imported = _resolve_subagent_source(
                from_source=from_source,
                name=name,
                description=description,
                home=home,
            )
        except (ValueError, SubagentConfigError) as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return False

        name_to_use = imported.name
        subagent_instructions = imported.instructions
    else:
        name_to_use = name
        subagent_instructions = ""

    if not name_to_use or not name_to_use.strip():
        print(
            "[ERROR] Subagent name is required. Please specify a name or provide a source via --from.",
            file=sys.stderr,
        )
        return False

    name_clean = name_to_use.strip()
    name_error = validate_resource_name(name_clean, "subagent")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    if from_source is None:
        title_val = _titleize(name_clean)
        subagent_instructions = f"""# {title_val}

Add developer instructions for the {name_clean} subagent here.
"""

    subagents_dir = aikito_dir / "subagents"
    subagent_file = subagents_dir / f"{name_clean}.md"
    subagents_toml = aikito_dir / "subagents.toml"

    if not subagents_toml.is_file():
        print(
            f"[ERROR] Subagents configuration not found at {_display_path(subagents_toml, home)}",
            file=sys.stderr,
        )
        return False

    try:
        existing_toml_content = subagents_toml.read_text(encoding="utf-8")
        toml_data = tomllib.loads(existing_toml_content)
    except Exception as exc:
        print(
            f"[ERROR] Failed to read subagents configuration: {exc}",
            file=sys.stderr,
        )
        return False

    existing_subagents = toml_data.get("subagents", {})
    is_already_registered = (
        isinstance(existing_subagents, dict) and name_clean in existing_subagents
    )
    file_already_exists = subagent_file.exists()

    if (is_already_registered or file_already_exists) and not force:
        if is_already_registered:
            print(
                f"[ERROR] Subagent '{name_clean}' is already registered.",
                file=sys.stderr,
            )
        else:
            print(
                f"[ERROR] Subagent instructions file already exists at {_display_path(subagent_file, home)}",
                file=sys.stderr,
            )
        return False

    old_info: Dict[str, Any] = {}
    if is_already_registered and isinstance(existing_subagents.get(name_clean), dict):
        old_info = existing_subagents[name_clean]

    # Resolve target_agents
    if agents is not None and len(agents) > 0:
        target_agents = agents
    elif imported and imported.target_agents:
        target_agents = imported.target_agents
    elif (
        is_already_registered
        and isinstance(old_info.get("agents"), list)
        and old_info.get("agents")
    ):
        target_agents = list(old_info["agents"])
    else:
        target_agents = DEFAULT_SUBAGENT_AGENTS

    # Resolve description
    if description is not None and description.strip():
        desc_val = description.strip()
    elif imported and imported.description:
        desc_val = imported.description
    elif is_already_registered and old_info.get("description"):
        desc_val = str(old_info["description"]).strip()
    else:
        desc_val = f"Subagent {name_clean}."

    # Seed platform configurations with existing registered ones if present
    platform_configs: Dict[str, Dict[str, Any]] = {}
    if is_already_registered:
        for k, v in old_info.items():
            if k not in ("description", "agents") and isinstance(v, dict):
                platform_configs[k] = dict(v)

    if imported is not None:
        for plat, opts in imported.explicit_platform_configs.items():
            if plat not in platform_configs:
                platform_configs[plat] = {}
            platform_configs[plat].update(opts)

        if imported.top_level_options:
            if len(target_agents) == 1:
                single_plat = target_agents[0]
                try:
                    validated = validate_platform_opts(
                        single_plat, name_clean, imported.top_level_options
                    )
                    if single_plat not in platform_configs:
                        platform_configs[single_plat] = {}
                    platform_configs[single_plat].update(validated)
                except SubagentConfigError as exc:
                    print(f"[ERROR] {exc}", file=sys.stderr)
                    return False
            else:
                ambiguous_keys = ", ".join(
                    f"'{k}'" for k in sorted(imported.top_level_options.keys())
                )
                print(
                    f"[ERROR] Source frontmatter specifies top-level {ambiguous_keys} but multiple target agents are specified ({', '.join(target_agents)}). Use explicit platform tables in frontmatter or specify a single agent with --agents.",
                    file=sys.stderr,
                )
                return False

    if is_already_registered:
        base_toml = _remove_subagent_from_toml(existing_toml_content, name_clean)
    else:
        base_toml = existing_toml_content

    subagent_block = _render_subagent_block(
        name=name_clean,
        description=desc_val,
        agents=target_agents,
        platform_configs=platform_configs,
    )
    new_toml_content = base_toml.rstrip() + "\n" + subagent_block

    # Validate resulting TOML syntax
    try:
        tomllib.loads(new_toml_content)
    except Exception as exc:
        print(
            f"[ERROR] Failed to update subagents configuration: {exc}",
            file=sys.stderr,
        )
        return False

    backup_dir = None
    staged_backup = None
    if file_already_exists:
        try:
            backup_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{name_clean}.add_backup.",
                    dir=subagents_dir,
                )
            )
            staged_backup = backup_dir / subagent_file.name
            shutil.copy2(subagent_file, staged_backup)
        except Exception as exc:
            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to backup existing subagent file: {exc}",
                file=sys.stderr,
            )
            return False

    try:
        subagents_dir.mkdir(parents=True, exist_ok=True)
        _atomic_write_text(subagent_file, subagent_instructions, encoding="utf-8")
        _atomic_write_text(subagents_toml, new_toml_content, encoding="utf-8")
    except Exception as exc:
        if staged_backup and staged_backup.exists():
            staged_backup.replace(subagent_file)
        elif subagent_file.exists() and not file_already_exists:
            subagent_file.unlink(missing_ok=True)
        if backup_dir:
            shutil.rmtree(backup_dir, ignore_errors=True)
        if existing_toml_content:
            try:
                _atomic_write_text(
                    subagents_toml, existing_toml_content, encoding="utf-8"
                )
            except Exception:
                pass
        print(f"[ERROR] Failed to write subagent: {exc}", file=sys.stderr)
        return False

    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)

    if is_already_registered or file_already_exists:
        print(f"[UPDATE FILE] {_display_path(subagent_file, home)}")
        print(
            f"[UPDATE FILE] {_display_path(subagents_toml, home)} (updated subagent '{name_clean}')"
        )
        print(f"\n[SUCCESS] Updated subagent '{name_clean}'.")
    else:
        print(f"[CREATE FILE] {_display_path(subagent_file, home)}")
        print(
            f"[UPDATE FILE] {_display_path(subagents_toml, home)} (registered subagent '{name_clean}')"
        )
        print(f"\n[SUCCESS] Added subagent '{name_clean}'.")

    if sync:
        print(
            f"\n[SYNC] Synchronizing subagent '{name_clean}' to target agent platforms..."
        )
        try:
            sync_ok = sync_subagent_configs(
                aikito_dir=aikito_dir,
                home=home,
            )
            if not sync_ok:
                return False
        except SubagentConfigError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return False
    else:
        print("💡 Next steps:")
        if from_source is None:
            print(
                f"  1. Update instructions in {_display_path(subagent_file, home)} (or run 'aikito edit subagent {name_clean}')"
            )
            print("  2. Synchronize to agents: aikito sync subagents")
        else:
            print("  1. Synchronize to agents: aikito sync subagents")

    return True


@dataclass
class ImportedMCP:
    name: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    agents: Optional[List[str]] = None


GENERIC_MCP_PATH_NAMES = {
    "mcp",
    "v1",
    "v2",
    "sse",
    "api",
    "tools",
    "tool",
    "endpoint",
    "server",
}


def _select_mcp_server(
    servers_dict: Dict[str, Any],
    name: Optional[str],
    source_desc: str,
) -> Tuple[str, Dict[str, Any]]:
    """
    Select one MCP server configuration from a multi-server dictionary.
    Handles exact/case-insensitive/hyphen/underscore key resolution, single server
    inference, ambiguous multiple servers, and empty dictionary errors.
    """
    if name and name.strip():
        target_key = name.strip()
        matched = None
        for k in servers_dict:
            if (
                k == target_key
                or k.replace("_", "-") == target_key
                or k.replace("-", "_") == target_key
            ):
                matched = k
                break
        if not matched:
            raise ValueError(f"Server '{target_key}' not found in '{source_desc}'.")
        return target_key, servers_dict[matched]

    if len(servers_dict) == 1:
        key = next(iter(servers_dict.keys()))
        return key.replace("_", "-").lower(), servers_dict[key]

    if len(servers_dict) > 1:
        available = ", ".join(sorted(servers_dict.keys()))
        raise ValueError(
            f"Source file contains multiple MCP servers ({available}). "
            f"Please specify which server to import via 'name'."
        )

    raise ValueError(f"No MCP servers found in '{source_desc}'.")


def _resolve_mcp_source(
    from_source: Union[str, Path],
    name: Optional[str],
    home: Path,
) -> ImportedMCP:
    """
    Resolve and parse an external MCP source (file or remote URL).
    Currently supports importing remote MCP configurations.
    """
    # Check if from_source is a remote URL
    if isinstance(from_source, str) and (
        from_source.startswith("http://") or from_source.startswith("https://")
    ):
        parsed = urlsplit(from_source)
        if not parsed.netloc:
            raise ValueError(f"Invalid remote MCP URL: {from_source}")

        inferred_name = None
        if name and name.strip():
            inferred_name = name.strip()
        else:
            path_parts = [p for p in parsed.path.split("/") if p]
            if path_parts:
                candidate = path_parts[-1].replace("_", "-").lower()
                if (
                    candidate not in GENERIC_MCP_PATH_NAMES
                    and validate_resource_name(candidate, "mcp") is None
                ):
                    inferred_name = candidate

        _temp_name = inferred_name or name or "mcp"
        sanitized_url, url_warnings = _sanitize_mcp_url(from_source, _temp_name)
        for w in url_warnings:
            print(f"[WARN] {w}", file=sys.stderr)
        return ImportedMCP(
            name=inferred_name,
            url=sanitized_url,
        )

    # Otherwise treated as a local file path
    source_path = Path(from_source).expanduser().resolve()
    if not source_path.exists():
        raise ValueError(f"Source file does not exist: {from_source}")
    if not source_path.is_file():
        raise ValueError(
            f"Source path must be a JSON or TOML file: {_display_path(source_path, home)}"
        )

    ext = source_path.suffix.lower()
    if ext not in (".json", ".jsonc", ".toml"):
        raise ValueError(
            f"Unsupported file format '{source_path.name}'. Expected .json, .jsonc, or .toml"
        )

    source_desc = _display_path(source_path, home)
    content = source_path.read_text(encoding="utf-8")
    server_cfg: Dict[str, Any] = {}
    inferred_name: Optional[str] = None

    if ext == ".toml":
        try:
            doc = tomllib.loads(content)
        except Exception as exc:
            raise ValueError(
                f"Failed to parse TOML from '{source_desc}': {exc}"
            ) from exc

        servers_dict = doc.get("servers") or doc.get("mcp_servers")
        if isinstance(servers_dict, dict):
            inferred_name, server_cfg = _select_mcp_server(
                servers_dict, name, source_desc
            )
        else:
            server_cfg = doc
            inferred_name = name.strip() if name and name.strip() else source_path.stem
    else:
        # JSON or JSONC
        try:
            doc = json.loads(content)
        except Exception:
            try:
                doc = mcp.parse_jsonc(content)
            except Exception as exc:
                raise ValueError(
                    f"Failed to parse JSON from '{source_desc}': {exc}"
                ) from exc

        if not isinstance(doc, dict):
            raise ValueError(f"JSON content in '{source_desc}' must be an object.")

        servers_dict = (
            doc.get("mcpServers") or doc.get("mcp_servers") or doc.get("servers")
        )
        if isinstance(servers_dict, dict):
            inferred_name, server_cfg = _select_mcp_server(
                servers_dict, name, source_desc
            )
        else:
            server_cfg = doc
            inferred_name = name.strip() if name and name.strip() else source_path.stem

    if not isinstance(server_cfg, dict):
        raise ValueError(
            f"MCP server configuration in '{source_desc}' must be a dictionary/table."
        )

    # Extract fields
    url = server_cfg.get("url") or server_cfg.get("serverUrl")
    command = server_cfg.get("command")

    if not url:
        if command:
            raise ValueError(
                f"Aikito currently supports synchronizing remote MCP servers. "
                f"Stdio server '{inferred_name or name}' without a URL cannot be imported as a remote MCP."
            )
        raise ValueError(f"No remote URL found for MCP server in '{source_desc}'.")

    if not isinstance(url, str) or not (
        url.startswith("http://") or url.startswith("https://")
    ):
        raise ValueError(
            f"Invalid remote MCP URL '{url}': must begin with http:// or https://"
        )

    # Build the merged headers dict from all three possible source keys.
    # Priority per key: headers > http_headers > env_http_headers.
    # env_http_headers values are bare environment variable names (Codex convention);
    # convert them to ${VAR} references so _sanitize_mcp_headers preserves them intact
    # instead of wrapping them in a new AIKITO_* placeholder.
    merged_headers: Dict[str, str] = {}

    env_hdr = server_cfg.get("env_http_headers")
    if isinstance(env_hdr, dict):
        for k, v in env_hdr.items():
            v_str = str(v)
            # Bare var name → ${VAR}; already-formatted references pass through.
            if mcp.environment_reference(v_str) is None and v_str.strip():
                v_str = "${" + v_str.strip() + "}"
            merged_headers[str(k)] = v_str

    http_hdr = server_cfg.get("http_headers")
    if isinstance(http_hdr, dict):
        for k, v in http_hdr.items():
            merged_headers[str(k)] = str(v)  # overrides env_http_headers per key

    canonical_hdr = server_cfg.get("headers")
    if isinstance(canonical_hdr, dict):
        for k, v in canonical_hdr.items():
            merged_headers[str(k)] = str(v)  # highest priority

    headers: Optional[Dict[str, str]] = merged_headers if merged_headers else None

    agents_val = server_cfg.get("agents")
    agents: Optional[List[str]] = None
    if isinstance(agents_val, list) and agents_val:
        agents = [str(a).strip() for a in agents_val if str(a).strip()]

    return ImportedMCP(
        name=inferred_name,
        url=url,
        headers=headers,
        agents=agents,
    )


def _sanitize_mcp_url(url: str, server_name: str) -> Tuple[str, List[str]]:
    """
    Strip credentials from a URL before writing it to the Git-tracked canonical TOML.

    - Removes userinfo (user:password@host) from the netloc.
    - Removes query-string parameters whose names are identified as sensitive
      by mcp.is_sensitive_url_parameter() (exact match + substring heuristics).

    Returns the sanitized URL and a list of warning messages (never including the
    actual secret values).
    """
    try:
        parts = urlsplit(url)
    except Exception:
        return url, []

    warnings: List[str] = []

    # Strip userinfo (e.g. https://user:pass@host/path)
    netloc = parts.netloc
    if parts.username or parts.password:
        host = parts.hostname or ""
        if parts.port:
            host = f"{host}:{parts.port}"
        netloc = host
        warnings.append(
            f"[SECURITY] URL for '{server_name}' contains userinfo credentials. "
            f"Stripped from canonical TOML to prevent leakage into workspace. "
            f"Supply credentials via headers or environment variables."
        )

    raw_query = parts.query
    if raw_query:
        params = parse_qsl(raw_query, keep_blank_values=True)
        clean_params = []
        stripped_names: List[str] = []
        for k, v in params:
            if mcp.is_sensitive_url_parameter(k):
                stripped_names.append(k)
            else:
                clean_params.append((k, v))
        if stripped_names:
            raw_query = urlencode(clean_params)
            warnings.append(
                f"[SECURITY] URL for '{server_name}' contains sensitive query parameter(s): "
                f"{', '.join(stripped_names)}. "
                f"Stripped from canonical TOML to prevent leakage into workspace. "
                f"Supply credentials via headers or environment variables."
            )

    sanitized = urlunsplit(
        (parts.scheme, netloc, parts.path, raw_query, parts.fragment)
    )
    return sanitized, warnings


def _sanitize_mcp_headers(
    headers: Dict[str, Any], server_name: str
) -> Tuple[Dict[str, str], List[str]]:
    """
    Sanitize headers to detect plaintext secrets (e.g. Bearer tokens, API keys, passwords, cookies)
    and replace them with secure environment variable placeholders (${AIKITO_<SERVER>_<KEY>})
    to prevent committing plaintext secrets to the Git-tracked workspace.
    """
    sanitized: Dict[str, str] = {}
    warnings: List[str] = []
    safe_server_name = "".join(
        char if char.isalnum() else "_" for char in server_name.upper()
    )
    for key, value in headers.items():
        value_text = str(value)
        is_reference = mcp.environment_reference(value_text) is not None
        is_sensitive = mcp.is_credential_header(key)
        if is_sensitive and not is_reference:
            safe_key = "".join(char if char.isalnum() else "_" for char in key.upper())
            placeholder = f"${{AIKITO_{safe_server_name}_{safe_key}}}"
            sanitized[key] = placeholder
            env_var_name = f"AIKITO_{safe_server_name}_{safe_key}"
            warnings.append(
                f"[SECURITY] Detected plaintext secret in header '{key}'. "
                f"Replaced with environment variable reference '{placeholder}' to prevent secret leakage into workspace.\n"
                f"       To provide this credential at runtime, set the environment variable and run:\n"
                f"         export {env_var_name}=<your-secret-value>\n"
                f"       (or configure canonical [authentication] via 'aikito auth mcp')"
            )
        else:
            sanitized[key] = value_text
    return sanitized, warnings


def add_mcp(
    aikito_dir: Path,
    home: Path,
    name: Optional[str] = None,
    transport: Optional[str] = None,
    command: Optional[str] = None,
    url: Optional[str] = None,
    agents: Optional[List[str]] = None,
    headers: Optional[Dict[str, str]] = None,
    from_source: Optional[Union[str, Path]] = None,
    sync: bool = False,
    force: bool = False,
) -> bool:
    """
    Create canonical MCP configuration (mcps/<name>.toml) or import from external source.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    if (
        force
        and from_source is None
        and url is None
        and command is None
        and transport is None
    ):
        print(
            "[ERROR] --force requires --from or server configuration arguments when updating an MCP server.",
            file=sys.stderr,
        )
        return False

    imported: Optional[ImportedMCP] = None
    if from_source is not None:
        try:
            imported = _resolve_mcp_source(
                from_source=from_source, name=name, home=home
            )
        except ValueError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return False

        if not name and imported.name:
            name = imported.name
        if not url and imported.url:
            url = imported.url
        if not transport:
            transport = "remote"
        if not headers and imported.headers:
            headers = imported.headers
        if agents is None and imported.agents:
            agents = imported.agents

    if not name or not name.strip():
        print(
            "[ERROR] MCP server name is required. Please specify a name or provide a source via --from.",
            file=sys.stderr,
        )
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
            "[ERROR] Cannot specify --url when --transport is 'stdio'.",
            file=sys.stderr,
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
            "[ERROR] --url is required when --transport is 'remote'.",
            file=sys.stderr,
        )
        return False

    mcps_dir = aikito_dir / "mcps"
    mcp_file = mcps_dir / f"{name_clean}.toml"

    file_already_exists = mcp_file.exists()
    if file_already_exists and not force:
        print(
            f"[ERROR] MCP server config already exists at {_display_path(mcp_file, home)}. Use --force to overwrite.",
            file=sys.stderr,
        )
        return False

    existing_data: Dict[str, Any] = {}
    if file_already_exists:
        try:
            existing_data = tomllib.loads(mcp_file.read_text(encoding="utf-8"))
        except Exception:
            existing_data = {}

    is_remote = transport == "remote" or (transport is None and url is not None)

    # Resolve target agents hierarchy:
    # 1. Explicit CLI --agents
    # 2. Imported agents from external source
    # 3. Existing canonical agents
    # 4. Fallback default agents
    if agents is not None and len(agents) > 0:
        target_agents = agents
    elif imported and imported.agents:
        target_agents = imported.agents
    elif (
        file_already_exists
        and isinstance(existing_data.get("agents"), list)
        and existing_data["agents"]
    ):
        target_agents = [str(a) for a in existing_data["agents"]]
    else:
        target_agents = DEFAULT_MCP_AGENTS

    # Preserve existing headers if not provided
    if headers is None and isinstance(existing_data.get("headers"), dict):
        headers = {str(k): str(v) for k, v in existing_data["headers"].items()}

    # Sanitize headers against plaintext credential leakage
    if headers:
        headers, sec_warnings = _sanitize_mcp_headers(headers, name_clean)
        for w in sec_warnings:
            print(f"[WARN] {w}", file=sys.stderr)

    agents_json = json.dumps(target_agents, ensure_ascii=False)

    lines: List[str] = []
    if is_remote:
        # Sanitize URL immediately before writing to canonical TOML (catches
        # credentials passed via --url directly, not only via --from).
        safe_url, url_warnings = _sanitize_mcp_url(url or "", name_clean)
        for w in url_warnings:
            print(f"[WARN] {w}", file=sys.stderr)
        lines.extend(
            [
                'transport = "remote"',
                f"url = {json.dumps(safe_url, ensure_ascii=False)}",
                f"agents = {agents_json}",
            ]
        )
        if headers:
            headers_parts = [
                f"{_format_toml_key(k)} = {_format_toml_value(v)}"
                for k, v in sorted(headers.items())
            ]
            lines.append(f"headers = {{ {', '.join(headers_parts)} }}")
    else:
        cmd_val = command if command else existing_data.get("command", "npx")
        lines.append(f"command = {json.dumps(cmd_val, ensure_ascii=False)}")
        existing_args = existing_data.get("args")
        args_val = existing_args if isinstance(existing_args, list) else []
        lines.append(f"args = {json.dumps(args_val, ensure_ascii=False)}")
        lines.append(f"agents = {agents_json}")
        if "env" in existing_data and isinstance(existing_data["env"], dict):
            env_parts = [
                f"{_format_toml_key(k)} = {_format_toml_value(str(v))}"
                for k, v in sorted(existing_data["env"].items())
            ]
            lines.append(f"env = {{ {', '.join(env_parts)} }}")

    # Preserve authentication table if present (common to remote and stdio)
    if "authentication" in existing_data and isinstance(
        existing_data["authentication"], dict
    ):
        lines.append("\n[authentication]")
        for k, v in sorted(existing_data["authentication"].items()):
            lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")

    # Preserve overrides table if present (common to remote and stdio)
    if "overrides" in existing_data and isinstance(existing_data["overrides"], dict):
        for agent_key, override_vals in sorted(existing_data["overrides"].items()):
            if isinstance(override_vals, dict):
                lines.append(f"\n[overrides.{_format_toml_key(agent_key)}]")
                for k, v in sorted(override_vals.items()):
                    lines.append(f"{_format_toml_key(k)} = {_format_toml_value(v)}")

    mcp_content = "\n".join(lines) + "\n"

    # Validate TOML syntax
    try:
        tomllib.loads(mcp_content)
    except Exception as exc:
        print(
            f"[ERROR] Failed to generate MCP configuration: {exc}",
            file=sys.stderr,
        )
        return False

    try:
        mcps_dir.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        print(
            f"[ERROR] Failed to create MCP config directory: {exc}",
            file=sys.stderr,
        )
        return False

    backup_dir = None
    staged_backup = None
    if file_already_exists:
        try:
            backup_dir = Path(
                tempfile.mkdtemp(
                    prefix=f".{name_clean}.add_backup.",
                    dir=mcps_dir,
                )
            )
            staged_backup = backup_dir / mcp_file.name
            shutil.copy2(mcp_file, staged_backup)
        except Exception as exc:
            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to backup existing MCP config file: {exc}",
                file=sys.stderr,
            )
            return False

    try:
        _atomic_write_text(mcp_file, mcp_content, encoding="utf-8")
    except Exception as exc:
        if staged_backup and staged_backup.exists():
            staged_backup.replace(mcp_file)
        elif mcp_file.exists() and not file_already_exists:
            mcp_file.unlink(missing_ok=True)
        if backup_dir:
            shutil.rmtree(backup_dir, ignore_errors=True)
        print(f"[ERROR] Failed to write MCP config: {exc}", file=sys.stderr)
        return False

    if sync:
        print(
            f"\n[SYNC] Synchronizing MCP server '{name_clean}' to target agent platforms..."
        )
        try:
            # Preflight dry-run check: ensure no agent runtime has a conflict before modifying any agent files
            preflight_buf = io.StringIO()
            dry_ok = mcp.sync_mcp_configs(
                aikito_dir=aikito_dir,
                home=home,
                dry_run=True,
                force=False,
                output=lambda msg: preflight_buf.write(msg + "\n"),
            )
            if not dry_ok:
                if staged_backup and staged_backup.exists():
                    staged_backup.replace(mcp_file)
                elif mcp_file.exists() and not file_already_exists:
                    mcp_file.unlink(missing_ok=True)
                if backup_dir:
                    shutil.rmtree(backup_dir, ignore_errors=True)
                preflight_msg = preflight_buf.getvalue().strip()
                if preflight_msg:
                    print(preflight_msg, file=sys.stderr)
                print(
                    f"[ERROR] Synchronization preflight failed due to conflict. Reverted changes to {_display_path(mcp_file, home)}.",
                    file=sys.stderr,
                )
                return False

            # Force is decoupled: add --force only overwrites canonical toml,
            # agent runtime configs still enforce conflict protections (force=False).
            sync_ok = mcp.sync_mcp_configs(
                aikito_dir=aikito_dir,
                home=home,
                force=False,
            )
            if not sync_ok:
                if staged_backup and staged_backup.exists():
                    staged_backup.replace(mcp_file)
                elif mcp_file.exists() and not file_already_exists:
                    mcp_file.unlink(missing_ok=True)
                if backup_dir:
                    shutil.rmtree(backup_dir, ignore_errors=True)
                print(
                    f"[ERROR] Synchronization failed. Reverted changes to {_display_path(mcp_file, home)}.",
                    file=sys.stderr,
                )
                return False
        except mcp.MCPConfigError as exc:
            if staged_backup and staged_backup.exists():
                staged_backup.replace(mcp_file)
            elif mcp_file.exists() and not file_already_exists:
                mcp_file.unlink(missing_ok=True)
            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
            print(f"[ERROR] {exc}", file=sys.stderr)
            print(
                f"[ERROR] Synchronization failed. Reverted changes to {_display_path(mcp_file, home)}.",
                file=sys.stderr,
            )
            return False
        except Exception as exc:
            if staged_backup and staged_backup.exists():
                staged_backup.replace(mcp_file)
            elif mcp_file.exists() and not file_already_exists:
                mcp_file.unlink(missing_ok=True)
            if backup_dir:
                shutil.rmtree(backup_dir, ignore_errors=True)
            print(
                f"[ERROR] Unexpected error during synchronization: {exc}. Reverted changes to {_display_path(mcp_file, home)}.",
                file=sys.stderr,
            )
            return False

    if backup_dir:
        shutil.rmtree(backup_dir, ignore_errors=True)

    if file_already_exists:
        print(f"[UPDATE FILE] {_display_path(mcp_file, home)}")
        print(f"\n[SUCCESS] Updated MCP server '{name_clean}'.")
    else:
        print(f"[CREATE FILE] {_display_path(mcp_file, home)}")
        print(f"\n[SUCCESS] Added MCP server '{name_clean}'.")

    if not sync:
        print("💡 Next steps:")
        print(
            f"  1. Configure server in {_display_path(mcp_file, home)} (or run 'aikito edit mcp {name_clean}')"
        )
        print("  2. Synchronize to agents: aikito sync mcp")
    return True
