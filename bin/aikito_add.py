"""
Resource addition module for Aikito.
Provides lightweight canonical skeleton creation and registration for skills, subagents, and MCP servers.
"""

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path
from typing import List, Optional

from aikito_platform import safe_relative_path



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


def add_skill(
    aikito_dir: Path,
    home: Path,
    name: str,
    description: Optional[str] = None,
    project_name: Optional[str] = None,
) -> bool:
    """
    Create canonical Skill skeleton (skills/<name>/SKILL.md) and register it in skills.toml or project agent.toml.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "skill")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    skills_root = aikito_dir / "skills"
    skill_dir = skills_root / name_clean
    skill_file = skill_dir / "SKILL.md"

    if skill_dir.exists():
        print(
            f"[ERROR] Skill '{name_clean}' already exists at {_display_path(skill_dir, home)}",
            file=sys.stderr,
        )
        return False

    desc_val = (description or f"Description for {name_clean} skill.").strip()
    title_val = _titleize(name_clean)

    skill_content = f"""---
name: {name_clean}
description: {desc_val}
---

# {title_val}

## Overview

Describe what this skill does and when agents should use it.
"""

    if project_name:
        proj_dir = aikito_dir / "projects" / project_name
        agent_toml = proj_dir / "agent.toml"
        if not agent_toml.is_file():
            print(
                f"[ERROR] Project '{project_name}' not found at {_display_path(agent_toml, home)}",
                file=sys.stderr,
            )
            return False

        try:
            original_text = agent_toml.read_text(encoding="utf-8")
            proj_data = tomllib.loads(original_text)
        except Exception as exc:
            print(f"[ERROR] Failed to parse {agent_toml}: {exc}", file=sys.stderr)
            return False

        existing_skills = proj_data.get("skills", [])
        if not isinstance(existing_skills, list):
            existing_skills = []

        if name_clean in existing_skills:
            print(
                f"[ERROR] Skill '{name_clean}' is already registered in project '{project_name}'",
                file=sys.stderr,
            )
            return False

        new_skills = [str(s) for s in existing_skills] + [name_clean]
        new_agent_toml_content = _update_skills_in_toml(original_text, new_skills)

        # Pre-validate TOML syntax before touching disk
        try:
            new_proj_data = tomllib.loads(new_agent_toml_content)
        except Exception as exc:
            print(
                f"[ERROR] Generated project agent.toml is invalid TOML: {exc}",
                file=sys.stderr,
            )
            return False

        # Semantic integrity check: ensure existing tables and other keys are preserved
        for k, v in proj_data.items():
            if k != "skills" and new_proj_data.get(k) != v:
                print(
                    f"[ERROR] Semantic integrity check failed for agent.toml key '{k}'",
                    file=sys.stderr,
                )
                return False

        if new_proj_data.get("skills") != new_skills:
            print(
                "[ERROR] Semantic integrity check failed for skills in agent.toml",
                file=sys.stderr,
            )
            return False

        try:
            skill_dir.mkdir(parents=True, exist_ok=True)
            skill_file.write_text(skill_content, encoding="utf-8")
            agent_toml.write_text(new_agent_toml_content, encoding="utf-8")
        except Exception as exc:
            shutil.rmtree(skill_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to write skill or project config: {exc}",
                file=sys.stderr,
            )
            return False

        print(f"[CREATE DIR] {_display_path(skill_dir, home)}")
        print(f"[CREATE FILE] {_display_path(skill_file, home)}")
        print(
            f"[UPDATE FILE] {_display_path(agent_toml, home)} (registered skill for project '{project_name}')"
        )
        print(f"\n[SUCCESS] Added skill '{name_clean}' to project '{project_name}'.")
        print("💡 Next steps:")
        print(
            f"  1. Update instructions in {_display_path(skill_file, home)} (or run 'aikito edit skill {name_clean}')"
        )
        print(f"  2. Synchronize project:   aikito sync project {project_name}")
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
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(skill_content, encoding="utf-8")
        skills_toml.write_text(skills_toml_content, encoding="utf-8")
    except Exception as exc:
        shutil.rmtree(skill_dir, ignore_errors=True)
        print(f"[ERROR] Failed to write global skill: {exc}", file=sys.stderr)
        return False

    print(f"[CREATE DIR] {_display_path(skill_dir, home)}")
    print(f"[CREATE FILE] {_display_path(skill_file, home)}")
    print(f"[UPDATE FILE] {_display_path(skills_toml, home)} (registered global skill)")
    print(f"\n[SUCCESS] Added global skill '{name_clean}'.")
    print("💡 Next steps:")
    print(
        f"  1. Update instructions in {_display_path(skill_file, home)} (or run 'aikito edit skill {name_clean}')"
    )
    print("  2. Synchronize to agents: aikito sync global")
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
        subagent_file.write_text(subagent_instructions, encoding="utf-8")
        subagents_toml.write_text(new_toml_content, encoding="utf-8")
    except Exception as exc:
        if subagent_file.exists():
            subagent_file.unlink(missing_ok=True)
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
        mcp_file.write_text(mcp_content, encoding="utf-8")
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
