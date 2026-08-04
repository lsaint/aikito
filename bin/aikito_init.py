"""
Workspace initialization module for Aikito.
Creates workspace skeleton, configuration templates, .gitignore, and initializes git repo.
"""

import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple


CLI_SOURCE_ROOT = Path(__file__).resolve().parents[1]

SOURCE_CHECKOUT_MARKERS = (
    Path("LICENSE"),
    Path("README.md"),
    Path("bin/aikito"),
)

WORKSPACE_FILE_MARKERS = (
    Path("agents.toml"),
    Path("mcps.toml"),
    Path("skills.toml"),
    Path("subagents.toml"),
)

WORKSPACE_DIRECTORY_MARKERS = (
    Path("memory"),
    Path("projects"),
    Path("skills"),
    Path("global"),
)

AGENTS_TOML_TEMPLATE = """# Aikito Agents Config
# Defines the installed agent integration points. Add or remove sections as needed.

[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

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
skills_path = ".claude/skills"

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
display_name = "Antigravity CLI (agy)"
instruction_path = ".gemini/GEMINI.md"
skills_path = ".gemini/antigravity-cli/skills"

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

[agents.opencode.mcp]
config_path = ".config/opencode/opencode.jsonc"
config_format = "jsonc"
name_style = "verbatim"
live_command = ["opencode", "mcp", "list"]
auth_command = ["opencode", "mcp", "auth", "{target}"]
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
skills = []
"""

GLOBAL_AGENTS_TEMPLATE = """# Global Agent Directives

Add shared instructions for your coding agents here.
"""

GITIGNORE_TEMPLATE = """# Aikito Git Ignore Rules
# Note: Rules use leading slashes to strictly match top-level workspace items.
/.DS_Store
/__pycache__/
/*.pyc
/.venv/
"""


def _detect_existing_agents(home: Path) -> List[Tuple[str, Path]]:
    candidates = [
        ("Claude Code", home / ".claude"),
        ("Codex", home / ".codex"),
        ("Antigravity (AGY)", home / ".gemini" / "config"),
        ("Agents Runtime", home / ".agents"),
    ]
    detected = []
    for name, p in candidates:
        if p.exists():
            detected.append((name, p))
    return detected


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

    print(f"[INFO] Initializing Aikito workspace in: {target_dir}")

    # 1. Create skeleton directories
    dirs_to_create = [
        target_dir,
        target_dir / "memory",
        target_dir / "memory" / "notes",
        target_dir / "projects",
        target_dir / "skills",
        target_dir / "global",
    ]

    for d in dirs_to_create:
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            print(f"[CREATE DIR] {d}")

    # 2. Write configuration templates & files
    files_to_create = [
        (target_dir / "agents.toml", AGENTS_TOML_TEMPLATE, "Agents config template"),
        (target_dir / "mcps.toml", MCPS_TOML_TEMPLATE, "MCP config template"),
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
    detected_agents = _detect_existing_agents(home)
    print("\n[SUCCESS] Aikito workspace initialization complete!")

    if detected_agents:
        print("\n[INFO] Detected existing local agent configuration(s):")
        for name, p in detected_agents:
            print(f"  - {name} ({p})")
        print(
            "\n💡 Hint: You can run 'aikito adopt' to preview adoption changes, or 'aikito adopt --apply' to apply."
        )

    return True
