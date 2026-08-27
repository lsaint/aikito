"""
Workspace initialization module for Aikito.
Creates workspace skeleton, configuration templates, .gitignore, and initializes git repo.
"""

import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

import tomllib

from aikito_mcp import collect_project_instruction_targets


CLI_SOURCE_ROOT = Path(__file__).resolve().parents[1]
BUNDLED_SKILL_NAMES = ("aikito", "durable-memory")

SOURCE_CHECKOUT_MARKERS = (
    Path("LICENSE"),
    Path("README.md"),
    Path("bin/aikito"),
)

WORKSPACE_FILE_MARKERS = (
    Path("agents.toml"),
    Path("skills.toml"),
    Path("subagents.toml"),
)

WORKSPACE_DIRECTORY_MARKERS = (
    Path("mcps"),
    Path("memory"),
    Path("projects"),
    Path("skills"),
    Path("global"),
)

AGENTS_TOML_TEMPLATE = """# Aikito Agents Config
# Defines the installed agent integration points. Add or remove sections as needed.
# project_instruction_path is relative to each registered project directory.

[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.codex.runner]
command = ["codex", "-C", "{workdir}", "{prompt}"]

# Optional per-runner environment overrides:
# [agents.codex.runner.env]
# HTTPS_PROXY = "http://127.0.0.1:1234"

[agents.codex.subagents]
config_path = ".codex/agents"
config_format = "codex_toml"

[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "underscore"
live_command = ["codex", "mcp", "list"]
auth_command = ["codex", "mcp", "login", "{target}"]

[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"
project_instruction_path = ".claude/CLAUDE.md"
skills_path = ".claude/skills"

[agents.claude-code.runner]
command = ["claude", "{prompt}"]

[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"

[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
live_command = ["claude", "mcp", "list"]
auth_command = ["claude", "mcp", "login", "{target}"]

[agents.agy]
display_name = "Antigravity CLI"
instruction_path = ".gemini/GEMINI.md"
project_instruction_path = "AGENTS.md"
skills_path = ".gemini/antigravity-cli/skills"

[agents.agy.runner]
command = ["agy", "--prompt-interactive", "{prompt}"]

[agents.agy.subagents]
config_path = ".gemini/config/agents"
config_format = "agy_markdown"

[agents.agy.mcp]
config_path = ".gemini/config/mcp_config.json"
config_format = "agy_json"
name_style = "verbatim"

[agents.opencode]
display_name = "OpenCode"
instruction_path = ".config/opencode/AGENTS.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.opencode.runner]
command = ["opencode", "{workdir}", "--prompt", "{prompt}"]

[agents.opencode.subagents]
config_path = ".config/opencode/agents"
config_format = "opencode_markdown"

[agents.opencode.mcp]
config_path = ".config/opencode/opencode.jsonc"
config_format = "jsonc"
name_style = "verbatim"
live_command = ["opencode", "mcp", "list"]
auth_command = ["opencode", "mcp", "auth", "{target}"]

[agents.github-copilot]
display_name = "GitHub Copilot CLI"
instruction_path = ".copilot/copilot-instructions.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.github-copilot.runner]
command = ["copilot", "-C", "{workdir}", "-i", "{prompt}"]

[agents.github-copilot.subagents]
config_path = ".copilot/agents"
config_format = "copilot_markdown"

[agents.github-copilot.mcp]
config_path = ".copilot/mcp-config.json"
config_format = "copilot_json"
name_style = "verbatim"
live_command = ["copilot", "mcp", "list"]

[agents.dsh]
display_name = "DeepSeek Harness"
instruction_path = ".dsh/AGENTS.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.dsh.runner]
command = ["dsh", "--profile", "headless", "{prompt}"]

[agents.dsh.subagents]
config_path = ".dsh/cordis.patch.yml"
config_format = "dsh_cordis_subagent"

[agents.dsh.mcp]
config_path = ".dsh/cordis.patch.yml"
config_format = "dsh_cordis"
name_style = "verbatim"

[agents.grok]
display_name = "Grok Build"
instruction_path = ".grok/rules/aikito.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.grok.runner]
command = ["grok", "--cwd", "{workdir}", "-p", "{prompt}"]

[agents.grok.subagents]
config_path = ".grok/agents"
config_format = "grok_markdown"

[agents.grok.mcp]
config_path = ".grok/config.toml"
config_format = "toml"
name_style = "verbatim"
live_command = ["grok", "mcp", "list"]
"""

MCPS_TOML_TEMPLATE = """# Aikito MCP Config
# Central MCP server definitions synchronized across agents.
# Add servers as [servers.<name>] tables when ready.

[servers]
"""

SUBAGENTS_TOML_TEMPLATE = """# Aikito Subagents Config
# Managed subagents definitions synchronized across agents.

[subagents]
"""

MEMORY_INDEX_TEMPLATE = """# Memory Index

Global atomic notes index across all workspaces.

## Notes
"""

SKILLS_TOML_TEMPLATE = """# Global skills enabled for all supported agents.
skills = ["aikito", "durable-memory"]
"""

CONFIG_TOML_TEMPLATE = """# Aikito Workspace Configuration

[memory]
# Number of days after which an untouched durable memory note is flagged as stale.
stale_days = 30

[inbox]
# Staging directory for incoming distilled notes.
path = "inbox"
"""

DEFAULT_MEMORY_INSTRUCTION = """## Persistent Memory

- All tasks must follow the `durable-memory` skill. Its rules are the sole authority for when to use Memory, task boundaries, retrieval, evaluation, persistence, and commits.
"""

GLOBAL_AGENTS_TEMPLATE = f"""# Global Agent Directives

{DEFAULT_MEMORY_INSTRUCTION}
"""

PROJECT_AGENTS_TEMPLATE = ""

PROJECT_MEMORY_INDEX_TEMPLATE = """# Project Memory Index

## Notes
"""

GITIGNORE_TEMPLATE = """# Aikito Git Ignore Rules
# Note: Rules use leading slashes to strictly match top-level workspace items.
/.DS_Store
/__pycache__/
/*.pyc
/.venv/
/.local/
"""


AGENT_INSTALL_MARKERS = {
    "codex": ("Codex", "codex", Path(".codex")),
    "claude-code": ("Claude Code", "claude", Path(".claude")),
    "agy": ("Antigravity CLI", "agy", Path(".gemini/config")),
    "opencode": ("OpenCode", "opencode", Path(".config/opencode")),
    "github-copilot": ("GitHub Copilot CLI", "copilot", Path(".copilot")),
    "dsh": ("DeepSeek Harness", "dsh", Path(".dsh")),
    "grok": ("Grok Build", "grok", Path(".grok")),
}


def _detect_existing_agents(home: Path) -> List[Tuple[str, Path]]:
    """Return installed registry agents in template order."""
    detected = []
    for display_name, binary, relative_marker in AGENT_INSTALL_MARKERS.values():
        marker = home / relative_marker
        executable = shutil.which(binary)
        if executable or marker.exists():
            detected.append((display_name, Path(executable) if executable else marker))
    return detected


def detected_agent_names(
    detected_agents: List[Tuple[str, Path]],
) -> tuple[str, ...]:
    detected_display_names = {name for name, _ in detected_agents}
    return tuple(
        name
        for name, (display_name, _binary, _marker) in AGENT_INSTALL_MARKERS.items()
        if display_name in detected_display_names
    )


def _filter_agents_template(agent_names: tuple[str, ...]) -> str:
    """Keep only detected top-level agent blocks from the bundled registry."""
    matches = list(re.finditer(r"(?m)^\[agents\.([^.\]]+)\]\s*$", AGENTS_TOML_TEMPLATE))
    preamble = AGENTS_TOML_TEMPLATE[: matches[0].start()].rstrip()
    selected = set(agent_names)
    blocks = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(
            AGENTS_TOML_TEMPLATE
        )
        if match.group(1) in selected:
            blocks.append(AGENTS_TOML_TEMPLATE[match.start() : end].strip())
    if not blocks:
        blocks.append("[agents]")
    return preamble + "\n\n" + "\n\n".join(blocks) + "\n"


def _bundled_skill_path(name: str) -> Path:
    return CLI_SOURCE_ROOT / "skills" / name


def _target_validation_error(target_dir: Path) -> Optional[str]:
    source_root = CLI_SOURCE_ROOT.resolve()
    if target_dir == source_root or source_root in target_dir.parents:
        return (
            "Refusing to initialize an Aikito workspace inside the CLI source "
            f"tree.\n  Source: {source_root}\n  Target: {target_dir}"
        )

    if not target_dir.exists():
        return None
    if not target_dir.is_dir():
        return f"Target path exists but is not a directory: {target_dir}"
    if not any(target_dir.iterdir()):
        return None

    if all((target_dir / marker).exists() for marker in SOURCE_CHECKOUT_MARKERS):
        return (
            "Target looks like an Aikito source checkout. Keep the CLI source "
            f"and workspace in separate directories: {target_dir}"
        )

    has_workspace_files = all(
        (target_dir / marker).is_file() for marker in WORKSPACE_FILE_MARKERS
    )
    has_workspace_directories = all(
        (target_dir / marker).is_dir() for marker in WORKSPACE_DIRECTORY_MARKERS
    )
    if has_workspace_files and has_workspace_directories:
        return None

    return (
        "Target directory is not empty and is not a recognized Aikito "
        f"workspace: {target_dir}"
    )


def init_workspace(target_dir: Path, home: Path, force: bool = False) -> bool:
    """
    Initializes an Aikito user data workspace in target_dir.
    Creates skeleton directories, template configs, leading-slash .gitignore, and git repo.
    """
    target_dir = target_dir.expanduser().resolve()

    validation_error = _target_validation_error(target_dir)
    if validation_error:
        print(f"[ERROR] {validation_error}", file=sys.stderr)
        return False

    missing_bundled_skills = [
        name
        for name in BUNDLED_SKILL_NAMES
        if not (_bundled_skill_path(name) / "SKILL.md").is_file()
    ]
    if missing_bundled_skills:
        print(
            "[ERROR] Bundled skill(s) not found: " + ", ".join(missing_bundled_skills),
            file=sys.stderr,
        )
        return False

    print(f"[INFO] Initializing Aikito workspace in: {target_dir}")

    # 1. Create skeleton directories
    dirs_to_create = [
        target_dir,
        target_dir / "global",
        target_dir / "projects",
        target_dir / "mcps",
        target_dir / "skills",
        target_dir / "subagents",
        target_dir / "memory" / "notes",
    ]

    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            print(f"[CREATE DIR] {d}")

    # 2. Write configuration templates & files
    detected_agents = _detect_existing_agents(home)
    installed_agent_names = detected_agent_names(detected_agents)
    files_to_create = [
        (target_dir / "config.toml", CONFIG_TOML_TEMPLATE, "Workspace config template"),
        (
            target_dir / "agents.toml",
            _filter_agents_template(installed_agent_names),
            "Detected agents config",
        ),
        (target_dir / "skills.toml", SKILLS_TOML_TEMPLATE, "Global skills config"),
        (
            target_dir / "subagents.toml",
            SUBAGENTS_TOML_TEMPLATE,
            "Subagents config template",
        ),
        (
            target_dir / "memory" / "index.md",
            MEMORY_INDEX_TEMPLATE,
            "Memory index file",
        ),
        (
            target_dir / "global" / "AGENTS.md",
            GLOBAL_AGENTS_TEMPLATE,
            "Global agent instructions",
        ),
        (
            target_dir / ".gitignore",
            GITIGNORE_TEMPLATE,
            "Workspace .gitignore with leading slashes",
        ),
    ]

    for file_path, content, desc in files_to_create:
        if not file_path.exists() or force:
            file_path.write_text(content, encoding="utf-8")
            status_tag = (
                "[FORCE WRITE]" if force and file_path.exists() else "[CREATE FILE]"
            )
            print(f"{status_tag} {file_path} ({desc})")
        else:
            print(f"[SKIP FILE] {file_path} (Already exists)")

    for skill_name in BUNDLED_SKILL_NAMES:
        bundled_skill_target = target_dir / "skills" / skill_name
        if bundled_skill_target.exists():
            print(f"[SKIP DIR] {bundled_skill_target} (Already exists)")
            continue
        shutil.copytree(_bundled_skill_path(skill_name), bundled_skill_target)
        print(f"[CREATE DIR] {bundled_skill_target} (Bundled {skill_name} skill)")

    # 3. Git Init
    git_dir = target_dir / ".git"
    if not git_dir.exists():
        try:
            subprocess.run(
                ["git", "init", str(target_dir)],
                capture_output=True,
                text=True,
                check=True,
            )
            print(f"[GIT INIT] Initialized Git repository in {target_dir}")
        except subprocess.CalledProcessError as e:
            print(f"[ERROR] Failed to run 'git init': {e.stderr.strip()}")
            return False
    else:
        print(f"[SKIP GIT] Git repository already exists in {target_dir}")

    # 4. Check existing agents & print next-step hints
    print("\n[SUCCESS] Aikito workspace initialization complete!")

    if detected_agents:
        print("\n[INFO] Detected existing local agent configuration(s):")
        for name, p in detected_agents:
            print(f"  - {name} ({p})")
        print(
            "\n💡 Hint: You can run 'aikito adopt' to preview adoption changes, or 'aikito adopt --apply' to apply."
        )

    return True


def _display_path(path: Path, home: Path) -> str:
    try:
        return f"~/{path.relative_to(home)}"
    except ValueError:
        return str(path)


def _validate_project_name(project_name: str) -> Optional[str]:
    if not project_name or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*", project_name
    ):
        return (
            "Project name must start with a letter or digit and contain only "
            "letters, digits, dots, underscores, or hyphens."
        )
    return None


def _project_validation_error(
    aikito_dir: Path,
    project_name: str,
    project_path: Path,
    *,
    reject_unexpected_entries: bool,
) -> Optional[str]:
    name_error = _validate_project_name(project_name)
    if name_error:
        return name_error

    if not project_path.exists():
        return f"Project path does not exist: {project_path}"
    if not project_path.is_dir():
        return f"Project path is not a directory: {project_path}"

    if not (aikito_dir / "agents.toml").is_file():
        return f"Aikito workspace is not initialized: {aikito_dir}"

    config_path = aikito_dir / "projects" / project_name / "agent.toml"
    config_data = None
    if config_path.exists():
        try:
            with open(config_path, "rb") as config_file:
                config_data = tomllib.load(config_file)
                saved_path = config_data.get("path")
        except (OSError, tomllib.TOMLDecodeError) as exc:
            return f"Failed to read existing project config {config_path}: {exc}"

        if saved_path:
            resolved_saved_path = Path(saved_path).expanduser().resolve()
            if resolved_saved_path != project_path:
                return (
                    f"Project '{project_name}' is already registered to "
                    f"{resolved_saved_path}, not {project_path}."
                )

    canonical_instructions = aikito_dir / "projects" / project_name / "AGENTS.md"
    for target, agent_names in collect_project_instruction_targets(
        aikito_dir, project_path, Path.home()
    ).items():
        if target.is_symlink():
            if target.resolve(strict=False) == canonical_instructions.resolve(
                strict=False
            ):
                continue
        elif not target.exists():
            continue
        return (
            f"Unmanaged project instructions for {', '.join(agent_names)} "
            f"already exist: {target}"
        )

    expected_entries = {"skills": set(), "memory": set()}
    if config_data is not None:
        expected_entries["skills"].update(config_data.get("skills", []))
        expected_entries["memory"].update(config_data.get("memory", []))
        project_memory = aikito_dir / "projects" / project_name / "memory"
        if project_memory.is_dir():
            expected_entries["memory"].update(
                item.name for item in project_memory.iterdir()
            )

    for managed_dir_name, allowed_entries in expected_entries.items():
        managed_dir = project_path / ".agents" / managed_dir_name
        if managed_dir.is_symlink() or not managed_dir.exists():
            continue
        if not managed_dir.is_dir():
            return f"Unmanaged project resources already exist: {managed_dir}"
        if not reject_unexpected_entries:
            continue
        unexpected_entries = {
            item.name for item in managed_dir.iterdir()
        } - allowed_entries
        if unexpected_entries:
            return (
                f"Unmanaged project resources already exist in {managed_dir}: "
                f"{', '.join(sorted(unexpected_entries))}"
            )

    return None


def project_validation_error(
    aikito_dir: Path, project_name: str, project_path: Path
) -> Optional[str]:
    """Validate registration, where every pre-existing runtime entry is foreign."""
    return _project_validation_error(
        aikito_dir,
        project_name,
        project_path,
        reject_unexpected_entries=True,
    )


def project_sync_validation_error(
    aikito_dir: Path, project_name: str, project_path: Path
) -> Optional[str]:
    """Validate repeat sync structure before managed-entry ownership is planned."""
    return _project_validation_error(
        aikito_dir,
        project_name,
        project_path,
        reject_unexpected_entries=False,
    )


def init_project(
    aikito_dir: Path, project_path: Path, project_name: Optional[str] = None
) -> Optional[str]:
    """Create an idempotent canonical project definition and return its name."""
    aikito_dir = aikito_dir.expanduser().resolve()
    project_path = project_path.expanduser().resolve()
    resolved_name = project_name or project_path.name

    validation_error = project_validation_error(aikito_dir, resolved_name, project_path)
    if validation_error:
        print(f"[ERROR] {validation_error}", file=sys.stderr)
        return None

    project_dir = aikito_dir / "projects" / resolved_name
    memory_dir = project_dir / "memory"
    notes_dir = memory_dir / "notes"
    notes_dir.mkdir(parents=True, exist_ok=True)

    config_path = project_dir / "agent.toml"
    if not config_path.exists():
        display_path = _display_path(project_path, Path.home())
        config_path.write_text(
            f'name = "{resolved_name}"\n'
            f'path = "{display_path}"\n'
            'sync_mode = "link"\n'
            "skills = []\n",
            encoding="utf-8",
        )
        print(f"[CREATE FILE] {config_path}")
    else:
        print(f"[SKIP FILE] {config_path} (Already exists)")

    files_to_create = (
        (project_dir / "AGENTS.md", PROJECT_AGENTS_TEMPLATE),
        (memory_dir / "index.md", PROJECT_MEMORY_INDEX_TEMPLATE),
    )
    for file_path, content in files_to_create:
        if file_path.exists():
            print(f"[SKIP FILE] {file_path} (Already exists)")
            continue
        file_path.write_text(content, encoding="utf-8")
        print(f"[CREATE FILE] {file_path}")

    print(f"[SUCCESS] Project '{resolved_name}' initialized in Aikito workspace.")
    return resolved_name
