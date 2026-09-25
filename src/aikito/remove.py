"""
Resource removal and unregistration module for Aikito.
Provides safe removal of skills, subagents, and MCP configurations from projects and workspace.
"""

import shutil
import sys
import tempfile
import tomllib
from pathlib import Path
from typing import Any, List, Optional, Tuple

from .add import (
    _atomic_write_text,
    _check_workspace_initialized,
    _display_path,
    _update_skills_in_toml,
    validate_resource_name,
)
from .compat import require_symlink_support
from .project_sync import sync_project
from .skill_state import WorkspaceWriterLock
from .skill_runtime import execute_selection_transaction
from .templating import BUNDLED_SKILL_NAMES
from .workspace_sync import sync_global_resources


def _remove_skill_from_projects(
    aikito_dir: Path,
    home: Path,
    skill_name: str,
    target_projects: List[str],
    sync: bool,
) -> bool:
    # 1. Verify target projects exist
    for proj in target_projects:
        agent_toml = aikito_dir / "projects" / proj / "agent.toml"
        if not agent_toml.is_file():
            print(f"[ERROR] Project '{proj}' not found.", file=sys.stderr)
            return False

    # 2. Plan updates
    planned_updates: List[Tuple[Path, str, str, str]] = []
    registered_projects: List[str] = []
    not_registered_projects: List[str] = []

    for proj in target_projects:
        agent_toml = aikito_dir / "projects" / proj / "agent.toml"
        try:
            original_text = agent_toml.read_text(encoding="utf-8")
            data = tomllib.loads(original_text)
            existing_skills = data.get("skills", [])
            if not isinstance(existing_skills, list):
                existing_skills = []
            existing_str_skills = [str(s) for s in existing_skills]

            if skill_name in existing_str_skills:
                registered_projects.append(proj)
                new_skills = [s for s in existing_str_skills if s != skill_name]
                new_content = _update_skills_in_toml(original_text, new_skills)

                # Preflight semantic verification
                new_data = tomllib.loads(new_content)
                for k, v in data.items():
                    if k != "skills" and new_data.get(k) != v:
                        raise ValueError(
                            f"Semantic integrity check failed for key '{k}'"
                        )
                if new_data.get("skills") != new_skills:
                    raise ValueError("Semantic integrity check failed for skills list")

                planned_updates.append((agent_toml, original_text, new_content, proj))
            else:
                not_registered_projects.append(proj)
        except Exception as exc:
            print(
                f"[ERROR] Failed to update configuration for project '{proj}': {exc}",
                file=sys.stderr,
            )
            return False

    if not registered_projects:
        if len(target_projects) == 1:
            print(
                f"[ERROR] Skill '{skill_name}' is not registered in project '{target_projects[0]}'.",
                file=sys.stderr,
            )
        else:
            print(
                f"[ERROR] Skill '{skill_name}' is not registered in any of the specified projects: {', '.join(target_projects)}.",
                file=sys.stderr,
            )
        return False

    for proj in not_registered_projects:
        print(f"[INFO] Skill '{skill_name}' was not registered in project '{proj}'.")

    # 3. Apply atomic updates with selection transaction
    file_updates = [
        (agent_toml, original_text, new_content)
        for agent_toml, original_text, new_content, _ in planned_updates
    ]
    ok, err = execute_selection_transaction(
        home,
        aikito_dir,
        registered_projects,
        file_updates,
        deactivate_skills=[skill_name],
        write_fn=_atomic_write_text,
    )
    if not ok:
        print(f"[ERROR] Failed to update project configuration: {err}", file=sys.stderr)
        return False

    for agent_toml, _, _, proj in planned_updates:
        print(
            f"[UPDATE FILE] {_display_path(agent_toml, home)} (unregistered skill from project '{proj}')"
        )

    proj_names_str = ", ".join(f"'{p}'" for p in registered_projects)
    print(
        f"\n[SUCCESS] Unregistered skill '{skill_name}' from project(s): {proj_names_str}."
    )

    # 4. Optional runtime synchronization
    if sync:
        for proj in registered_projects:
            if not sync_project(aikito_dir, home, proj):
                return False

    return True


def _remove_skill_globally(
    aikito_dir: Path,
    home: Path,
    skill_name: str,
    force: bool,
    sync: bool,
) -> bool:
    skill_dir = aikito_dir / "skills" / skill_name
    skills_toml = aikito_dir / "skills.toml"

    has_canonical_dir = skill_dir.is_dir()
    original_skills_toml_text = ""
    skills_toml_has_skill = False
    new_skills_toml_content = ""

    if skills_toml.is_file():
        try:
            original_skills_toml_text = skills_toml.read_text(encoding="utf-8")
            data = tomllib.loads(original_skills_toml_text)
            g_skills = data.get("skills", [])
            if isinstance(g_skills, list) and skill_name in g_skills:
                skills_toml_has_skill = True
                new_g_skills = [str(s) for s in g_skills if str(s) != skill_name]
                new_skills_toml_content = _update_skills_in_toml(
                    original_skills_toml_text, new_g_skills
                )
                # Preflight check
                chk = tomllib.loads(new_skills_toml_content)
                if chk.get("skills") != new_g_skills:
                    raise ValueError("Semantic check failed for skills.toml")
        except Exception as exc:
            print(
                f"[ERROR] Failed to read global skills configuration: {exc}",
                file=sys.stderr,
            )
            return False

    if not has_canonical_dir and not skills_toml_has_skill:
        print(
            f"[ERROR] Skill '{skill_name}' does not exist in workspace ({_display_path(aikito_dir, home)}).",
            file=sys.stderr,
        )
        return False

    # Check project references
    referencing_projects: List[str] = []
    planned_project_updates: List[Tuple[Path, str, str, str]] = []

    projects_dir = aikito_dir / "projects"
    if projects_dir.is_dir():
        for proj_folder in sorted(projects_dir.iterdir()):
            if proj_folder.is_dir():
                agent_toml = proj_folder / "agent.toml"
                if agent_toml.is_file():
                    try:
                        orig_text = agent_toml.read_text(encoding="utf-8")
                        p_data = tomllib.loads(orig_text)
                        p_skills = p_data.get("skills", [])
                        if isinstance(p_skills, list) and skill_name in p_skills:
                            referencing_projects.append(proj_folder.name)
                            new_skills = [
                                str(s) for s in p_skills if str(s) != skill_name
                            ]
                            new_content = _update_skills_in_toml(orig_text, new_skills)
                            # Preflight
                            chk = tomllib.loads(new_content)
                            if chk.get("skills") != new_skills:
                                raise ValueError(
                                    f"Semantic check failed for {proj_folder.name}"
                                )
                            planned_project_updates.append(
                                (
                                    agent_toml,
                                    orig_text,
                                    new_content,
                                    proj_folder.name,
                                )
                            )
                    except Exception as exc:
                        print(
                            f"[WARN] Failed to inspect configuration for project '{proj_folder.name}': {exc}",
                            file=sys.stderr,
                        )

    if referencing_projects and not force:
        proj_list_str = ", ".join(f"'{p}'" for p in referencing_projects)
        proj_arg_str = ",".join(referencing_projects)
        print(
            f"[ERROR] Skill '{skill_name}' is still registered in project(s): {proj_list_str}.\n"
            f"Unregister it first with 'aikito rm skill {skill_name} --project {proj_arg_str}', "
            f"or use --force to unregister from all projects and delete.",
            file=sys.stderr,
        )
        return False

    # Execute transactional removal
    file_updates = [
        (agent_toml, orig_text, new_content)
        for agent_toml, orig_text, new_content, _ in planned_project_updates
    ]
    skills_toml_update = None
    if skills_toml_has_skill:
        skills_toml_update = (
            skills_toml,
            original_skills_toml_text,
            new_skills_toml_content,
        )

    canonical_backup_update = None
    backup_dir = None
    if has_canonical_dir:
        backup_dir = Path(
            tempfile.mkdtemp(
                prefix=f".{skill_name}.rm_backup.",
                dir=skill_dir.parent,
            )
        )
        canonical_backup_update = (skill_dir, backup_dir / skill_name)

    ok, err = execute_selection_transaction(
        home=home,
        workspace_root=aikito_dir,
        project_names=referencing_projects,
        file_updates=file_updates,
        skills_toml_update=skills_toml_update,
        canonical_backup_update=canonical_backup_update,
        deactivate_skills=[skill_name],
        write_fn=_atomic_write_text,
    )
    if not ok:
        if backup_dir and backup_dir.exists():
            shutil.rmtree(backup_dir, ignore_errors=True)
        print(f"[ERROR] Failed during skill removal: {err}", file=sys.stderr)
        return False

    if backup_dir and backup_dir.exists():
        shutil.rmtree(backup_dir, ignore_errors=True)

    for agent_toml, _, _, proj in planned_project_updates:
        print(
            f"[UPDATE FILE] {_display_path(agent_toml, home)} (unregistered skill from project '{proj}')"
        )
    if skills_toml_has_skill:
        print(
            f"[UPDATE FILE] {_display_path(skills_toml, home)} (unregistered global skill)"
        )
    if has_canonical_dir:
        print(f"[REMOVE DIR] {_display_path(skill_dir, home)}")

    print(f"\n[SUCCESS] Removed skill '{skill_name}'.")

    # 4. Optional runtime synchronization
    if sync:
        for _, _, _, proj in planned_project_updates:
            if not sync_project(aikito_dir, home, proj):
                return False

        require_symlink_support()
        if not sync_global_resources(aikito_dir, home):
            return False

    return True


def remove_skill(
    aikito_dir: Path,
    home: Path,
    name: str,
    project_name: Optional[str] = None,
    projects: Optional[List[str]] = None,
    force: bool = False,
    sync: bool = False,
) -> bool:
    """
    Remove or unregister a skill from workspace or specific projects.

    - If projects are specified: unregister the skill from each project's agent.toml.
      The canonical skill directory in the workspace is preserved.
    - If no projects are specified: globally unregister from skills.toml and remove
      the canonical skill directory. If the skill is currently referenced by any
      project, removal is blocked unless force=True.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    if not name or not isinstance(name, str) or not name.strip():
        print("[ERROR] Skill name cannot be empty.", file=sys.stderr)
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "skill")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    # Collect target projects
    target_projects: List[str] = []
    if projects:
        for p in projects:
            if p:
                for part in p.split(","):
                    part_clean = part.strip()
                    if part_clean and part_clean not in target_projects:
                        target_projects.append(part_clean)
    if project_name:
        for part in project_name.split(","):
            part_clean = part.strip()
            if part_clean and part_clean not in target_projects:
                target_projects.append(part_clean)

    if not target_projects and name_clean in BUNDLED_SKILL_NAMES:
        print(
            f"[ERROR] Cannot remove bundled system skill '{name_clean}'.",
            file=sys.stderr,
        )
        return False

    if target_projects:
        return _remove_skill_from_projects(
            aikito_dir=aikito_dir,
            home=home,
            skill_name=name_clean,
            target_projects=target_projects,
            sync=sync,
        )

    return _remove_skill_globally(
        aikito_dir=aikito_dir,
        home=home,
        skill_name=name_clean,
        force=force,
        sync=sync,
    )


def remove_subagent(
    aikito_dir: Path,
    home: Path,
    name: str,
    sync: bool = False,
) -> bool:
    """Remove one canonical subagent file and optionally prune native configs."""
    from .workspace_layout import (
        WorkspaceLayoutError,
        parse_subagent_file,
        require_current_layout,
    )

    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()
    if not aikito_dir.is_dir():
        print(
            f"[ERROR] Aikito workspace directory not found: {aikito_dir}",
            file=sys.stderr,
        )
        return False
    try:
        require_current_layout(aikito_dir)
    except WorkspaceLayoutError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return False
    name_clean = name.strip() if isinstance(name, str) else ""
    name_error = validate_resource_name(name_clean, "subagent")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False
    subagent_file = aikito_dir / "subagents" / f"{name_clean}.md"
    if not subagent_file.exists():
        print(
            f"[ERROR] Subagent '{name_clean}' does not exist in workspace.",
            file=sys.stderr,
        )
        return False
    try:
        parse_subagent_file(subagent_file)
        with WorkspaceWriterLock(home):
            subagent_file.unlink()
    except (OSError, WorkspaceLayoutError) as exc:
        print(f"[ERROR] Failed to remove subagent: {exc}", file=sys.stderr)
        return False
    print(f"[DELETE FILE] {_display_path(subagent_file, home)}")
    print(f"[SUCCESS] Removed subagent '{name_clean}'.")
    if sync:
        from .subagent import SubagentConfigError, sync_subagent_configs

        try:
            return sync_subagent_configs(aikito_dir=aikito_dir, home=home, prune=True)
        except SubagentConfigError as exc:
            print(f"[ERROR] {exc}", file=sys.stderr)
            return False
    return True


def remove_mcp(
    aikito_dir: Path,
    home: Path,
    name: str,
    sync: bool = False,
    force: bool = False,
) -> bool:
    """
    Remove a canonical MCP server configuration from workspace.

    If sync=True, also remove the server from all configured and installed
    agent platforms and update runtime MCP state.
    """
    aikito_dir = aikito_dir.expanduser().resolve()
    home = home.expanduser().resolve()

    ws_error = _check_workspace_initialized(aikito_dir)
    if ws_error:
        print(f"[ERROR] {ws_error}", file=sys.stderr)
        return False

    if not name or not isinstance(name, str) or not name.strip():
        print("[ERROR] MCP server name cannot be empty.", file=sys.stderr)
        return False

    name_clean = name.strip()
    name_error = validate_resource_name(name_clean, "mcp")
    if name_error:
        print(f"[ERROR] {name_error}", file=sys.stderr)
        return False

    mcps_dir = aikito_dir / "mcps"
    mcp_file = mcps_dir / f"{name_clean}.toml"

    if not mcp_file.is_file():
        print(
            f"[ERROR] MCP server '{name_clean}' does not exist in workspace ({_display_path(aikito_dir, home)}).",
            file=sys.stderr,
        )
        return False

    specs_to_remove: List[Any] = []
    if sync:
        from .mcp import MCPConfigError, load_agent_specs, sync_remove_mcp_from_agents

        try:
            all_specs = load_agent_specs(aikito_dir, home)
            specs_to_remove = [s for s in all_specs if s.server == name_clean]
        except MCPConfigError as exc:
            print(
                f"[ERROR] Failed to inspect MCP configuration: {exc}",
                file=sys.stderr,
            )
            return False

    backup_dir = Path(
        tempfile.mkdtemp(
            prefix=f".{name_clean}.rm_backup.",
            dir=mcp_file.parent,
        )
    )
    staged_backup = backup_dir / mcp_file.name

    try:
        mcp_file.replace(staged_backup)
    except Exception as exc:
        shutil.rmtree(backup_dir, ignore_errors=True)
        print(
            f"[ERROR] Failed to remove MCP configuration file: {exc}",
            file=sys.stderr,
        )
        return False

    if sync and specs_to_remove:
        try:
            sync_ok = sync_remove_mcp_from_agents(
                specs=specs_to_remove,
                home=home,
                force=force,
            )
            if not sync_ok:
                staged_backup.replace(mcp_file)
                shutil.rmtree(backup_dir, ignore_errors=True)
                return False
        except Exception as exc:
            staged_backup.replace(mcp_file)
            shutil.rmtree(backup_dir, ignore_errors=True)
            print(
                f"[ERROR] Failed to synchronize MCP server removal: {exc}",
                file=sys.stderr,
            )
            return False

    shutil.rmtree(backup_dir, ignore_errors=True)
    print(f"[DELETE FILE] {_display_path(mcp_file, home)}")
    print(f"\n[SUCCESS] Removed MCP server '{name_clean}'.")
    return True
