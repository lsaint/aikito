"""
Adoption module for Aikito.
Scans existing local agent configurations (Instructions, MCP servers, Subagents),
creates timestamped backups, and adopts them into the Aikito workspace.
Supports --dry-run for previewing adoption changes without modifying workspace files.
"""

from __future__ import annotations

import json
import shutil
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from aikito_init import DEFAULT_MEMORY_INSTRUCTION, GLOBAL_AGENTS_TEMPLATE
from aikito_subagent import has_aikito_marker


def collect_source_files_for_backup(plan: AdoptPlan) -> List[Path]:
    files = set()

    if plan.instructions and plan.instructions.sources:
        for _, path, _ in plan.instructions.sources:
            if path.is_file():
                files.add(path)

    claude_json_candidates = [
        plan.home
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json",
        plan.home / ".claude" / "claude_desktop_config.json",
    ]
    for p in claude_json_candidates:
        if p.is_file():
            files.add(p)

    codex_toml = plan.home / ".codex" / "config.toml"
    if codex_toml.is_file():
        files.add(codex_toml)

    copilot_mcp = plan.home / ".copilot" / "mcp-config.json"
    if copilot_mcp.is_file():
        files.add(copilot_mcp)

    if plan.subagents:
        for sub in plan.subagents:
            if sub.source_file.is_file():
                files.add(sub.source_file)

    return sorted(files)


def create_adopt_backup(
    plan: AdoptPlan, backup_dir: Optional[Path] = None, dry_run: bool = False
) -> Optional[Path]:
    source_files = collect_source_files_for_backup(plan)
    if not source_files:
        return None

    if backup_dir is None:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_dir = plan.home / ".aikito" / "backups" / f"adopt_{ts}"

    if dry_run:
        print(
            f"\n[DRY-RUN BACKUP] Would backup {len(source_files)} local agent file(s) into: {backup_dir}"
        )
        return backup_dir

    backup_dir.mkdir(parents=True, exist_ok=True)
    for src in source_files:
        try:
            rel_path = src.relative_to(plan.home)
            dest = backup_dir / rel_path
        except ValueError:
            dest = backup_dir / src.name

        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)

    print(
        f"\n[BACKUP] Saved {len(source_files)} local agent config backup(s) to: {backup_dir}"
    )
    return backup_dir


@dataclass
class InstructionsAdoption:
    sources: List[Tuple[str, Path, str]]  # [(agent_name, file_path, content)]
    has_conflict: bool = False
    merged_content: Optional[str] = None
    target_path: Optional[Path] = None


@dataclass
class MCPServerAdoption:
    server_name: str
    agents: List[str]
    config_data: Dict[str, Any]
    source_agent: str


@dataclass
class SubagentAdoption:
    subagent_name: str
    description: str
    role: str
    system_prompt: str
    target_agents: List[str]
    source_file: Path
    platform_configs: Dict[str, Dict[str, Any]]


@dataclass
class AdoptPlan:
    aikito_dir: Path
    home: Path
    instructions: InstructionsAdoption
    mcp_servers: List[MCPServerAdoption]
    subagents: List[SubagentAdoption]


def _normalize_instructions_content(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines)


def _append_default_memory_instruction(content: str) -> str:
    normalized = _normalize_instructions_content(content)
    default_rule = _normalize_instructions_content(DEFAULT_MEMORY_INSTRUCTION)
    if default_rule in normalized:
        return normalized + "\n"
    if not normalized:
        return DEFAULT_MEMORY_INSTRUCTION
    return f"{normalized}\n\n{DEFAULT_MEMORY_INSTRUCTION}"


def _merge_adopted_instructions(
    imported_content: str, target_path: Path
) -> tuple[bool, str | None]:
    if not target_path.is_file():
        return False, imported_content

    canonical_content = target_path.read_text(encoding="utf-8")
    canonical = _normalize_instructions_content(canonical_content)
    imported = _normalize_instructions_content(imported_content)
    default_template = _normalize_instructions_content(GLOBAL_AGENTS_TEMPLATE)

    if canonical == imported:
        return False, canonical_content

    merged = _append_default_memory_instruction(imported_content)
    if canonical == default_template:
        return False, merged
    if canonical == _normalize_instructions_content(merged):
        return False, canonical_content

    return True, None


def scan_instructions(aikito_dir: Path, home: Path) -> InstructionsAdoption:
    candidates = [
        ("codex", home / ".codex" / "AGENTS.md"),
        ("claude-code", home / ".claude" / "CLAUDE.md"),
        ("agy", home / ".gemini" / "config" / "AGENTS.md"),
        ("github-copilot", home / ".copilot" / "copilot-instructions.md"),
    ]

    target_path = aikito_dir / "global" / "AGENTS.md"

    sources: List[Tuple[str, Path, str]] = []
    for agent_name, path in candidates:
        if path.is_file():
            try:
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    sources.append((agent_name, path, content))
            except (PermissionError, OSError) as e:
                print(
                    f"[WARN] Unable to read instructions from '{path}': {e}",
                    file=sys.stderr,
                )

    if not sources:
        return InstructionsAdoption(
            sources=[],
            has_conflict=False,
            merged_content=None,
            target_path=target_path,
        )

    # Check content consistency ignoring trailing spaces & empty line mismatches
    normalized = [_normalize_instructions_content(c) for _, _, c in sources]
    first_normalized = normalized[0]
    all_same = all(n == first_normalized for n in normalized)

    if all_same:
        has_conflict, merged_content = _merge_adopted_instructions(
            sources[0][2], target_path
        )
        return InstructionsAdoption(
            sources=sources,
            has_conflict=has_conflict,
            merged_content=merged_content,
            target_path=target_path,
        )
    else:
        return InstructionsAdoption(
            sources=sources,
            has_conflict=True,
            merged_content=None,
            target_path=target_path,
        )


def _sanitize_mcp_env(
    env_dict: Dict[str, Any],
) -> Tuple[Dict[str, str], List[str]]:
    sanitized = {}
    warnings = []
    for k, v in env_dict.items():
        val_str = str(v)
        if val_str.startswith("${") and val_str.endswith("}"):
            sanitized[k] = val_str
        elif val_str.startswith("$"):
            sanitized[k] = val_str
        else:
            sanitized[k] = f"${{{k}}}"
            warnings.append(
                f"[SECURITY] Converted plaintext secret in env key '{k}' to environment variable reference '${{{k}}}'"
            )
    return sanitized, warnings


def _sanitize_mcp_headers(headers: Dict[str, Any], server_name: str) -> Dict[str, str]:
    sanitized = {}
    sensitive_fragments = ("authorization", "api-key", "api_key", "token", "secret")
    safe_server_name = "".join(
        char if char.isalnum() else "_" for char in server_name.upper()
    )
    for key, value in headers.items():
        value_text = str(value)
        is_reference = "${" in value_text or value_text.startswith("$")
        is_sensitive = any(fragment in key.lower() for fragment in sensitive_fragments)
        if is_sensitive and not is_reference:
            safe_key = "".join(char if char.isalnum() else "_" for char in key.upper())
            value_text = f"${{AIKITO_{safe_server_name}_{safe_key}}}"
        sanitized[key] = value_text
    return sanitized


def scan_mcp_servers(aikito_dir: Path, home: Path) -> List[MCPServerAdoption]:
    adopted_servers: Dict[str, MCPServerAdoption] = {}

    # 1. Claude Code (~/.claude.json) & Claude Desktop JSON
    claude_json_candidates = [
        home / ".claude.json",
        home
        / "Library"
        / "Application Support"
        / "Claude"
        / "claude_desktop_config.json",
        home / ".claude" / "claude_desktop_config.json",
    ]

    for c_path in claude_json_candidates:
        if c_path.is_file():
            try:
                with open(c_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    mcp_servers = data.get("mcpServers", {})
                    if isinstance(mcp_servers, dict):
                        for s_name, s_cfg in mcp_servers.items():
                            if (
                                isinstance(s_cfg, dict)
                                and s_name not in adopted_servers
                            ):
                                s_cfg_copy = dict(s_cfg)
                                if "env" in s_cfg_copy and isinstance(
                                    s_cfg_copy["env"], dict
                                ):
                                    sanitized_env, _ = _sanitize_mcp_env(
                                        s_cfg_copy["env"]
                                    )
                                    s_cfg_copy["env"] = sanitized_env

                                adopted_servers[s_name] = MCPServerAdoption(
                                    server_name=s_name,
                                    agents=["claude-code"],
                                    config_data=s_cfg_copy,
                                    source_agent="claude-code",
                                )
            except json.JSONDecodeError as e:
                print(
                    f"[WARN] Failed to parse JSON in '{c_path}': {e}",
                    file=sys.stderr,
                )
            except (PermissionError, OSError) as e:
                print(
                    f"[WARN] Failed to read MCP config file '{c_path}': {e}",
                    file=sys.stderr,
                )

    # 2. Codex TOML
    codex_toml = home / ".codex" / "config.toml"
    if codex_toml.is_file():
        try:
            with open(codex_toml, "rb") as f:
                data = tomllib.load(f)
                mcp_servers = data.get("mcp_servers", {})
                if isinstance(mcp_servers, dict):
                    for s_name, s_cfg in mcp_servers.items():
                        if isinstance(s_cfg, dict):
                            s_cfg_copy = dict(s_cfg)
                            if "env" in s_cfg_copy and isinstance(
                                s_cfg_copy["env"], dict
                            ):
                                sanitized_env, _ = _sanitize_mcp_env(s_cfg_copy["env"])
                                s_cfg_copy["env"] = sanitized_env

                            if s_name in adopted_servers:
                                if "codex" not in adopted_servers[s_name].agents:
                                    adopted_servers[s_name].agents.append("codex")
                            else:
                                adopted_servers[s_name] = MCPServerAdoption(
                                    server_name=s_name,
                                    agents=["codex"],
                                    config_data=s_cfg_copy,
                                    source_agent="codex",
                                )
        except tomllib.TOMLDecodeError as e:
            print(
                f"[WARN] Failed to parse TOML in '{codex_toml}': {e}",
                file=sys.stderr,
            )
        except (PermissionError, OSError) as e:
            print(
                f"[WARN] Failed to read MCP config file '{codex_toml}': {e}",
                file=sys.stderr,
            )

    # 3. GitHub Copilot CLI (~/.copilot/mcp-config.json)
    copilot_mcp_config = home / ".copilot" / "mcp-config.json"
    if copilot_mcp_config.is_file():
        try:
            with open(copilot_mcp_config, "r", encoding="utf-8") as f:
                data = json.load(f)
                mcp_servers = data.get("mcpServers", {})
                if isinstance(mcp_servers, dict):
                    for s_name, s_cfg in mcp_servers.items():
                        if isinstance(s_cfg, dict):
                            s_cfg_copy = dict(s_cfg)
                            if "env" in s_cfg_copy and isinstance(
                                s_cfg_copy["env"], dict
                            ):
                                sanitized_env, _ = _sanitize_mcp_env(s_cfg_copy["env"])
                                s_cfg_copy["env"] = sanitized_env
                            if "headers" in s_cfg_copy and isinstance(
                                s_cfg_copy["headers"], dict
                            ):
                                sanitized_headers = _sanitize_mcp_headers(
                                    s_cfg_copy["headers"], s_name
                                )
                                s_cfg_copy["headers"] = sanitized_headers

                            server_type = s_cfg_copy.get("type", "http")
                            if server_type != "http" or not isinstance(
                                s_cfg_copy.get("url"), str
                            ):
                                print(
                                    f"[WARN] Skipping unsupported local Copilot MCP server '{s_name}'",
                                    file=sys.stderr,
                                )
                                continue
                            s_cfg_copy["transport"] = "remote"

                            if s_name in adopted_servers:
                                if (
                                    "github-copilot"
                                    not in adopted_servers[s_name].agents
                                ):
                                    adopted_servers[s_name].agents.append(
                                        "github-copilot"
                                    )
                            else:
                                adopted_servers[s_name] = MCPServerAdoption(
                                    server_name=s_name,
                                    agents=["github-copilot"],
                                    config_data=s_cfg_copy,
                                    source_agent="github-copilot",
                                )
        except json.JSONDecodeError as e:
            print(
                f"[WARN] Failed to parse JSON in '{copilot_mcp_config}': {e}",
                file=sys.stderr,
            )
        except (PermissionError, OSError) as e:
            print(
                f"[WARN] Failed to read MCP config file '{copilot_mcp_config}': {e}",
                file=sys.stderr,
            )

    return list(adopted_servers.values())


def _parse_markdown_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    content = content.strip()
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_raw = parts[1].strip()
    body = parts[2].strip()

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

    return meta, body


def scan_subagents(aikito_dir: Path, home: Path) -> List[SubagentAdoption]:
    subagents: List[SubagentAdoption] = []

    # Claude Code Subagents
    claude_agents_dir = home / ".claude" / "agents"
    if claude_agents_dir.is_dir():
        for agent_file in sorted(claude_agents_dir.glob("*.md")):
            if has_aikito_marker(agent_file):
                continue
            s_name = agent_file.stem
            try:
                raw_content = agent_file.read_text(encoding="utf-8")
                meta, body = _parse_markdown_frontmatter(raw_content)
                desc = meta.get(
                    "description", f"Adopted subagent {s_name} from Claude Code"
                )

                subagents.append(
                    SubagentAdoption(
                        subagent_name=s_name,
                        description=str(desc),
                        role=s_name.capitalize(),
                        system_prompt=body,
                        target_agents=["claude-code"],
                        source_file=agent_file,
                        platform_configs={},
                    )
                )
            except (PermissionError, OSError) as e:
                print(
                    f"[WARN] Failed to read subagent file '{agent_file}': {e}",
                    file=sys.stderr,
                )

    # GitHub Copilot CLI Subagents
    copilot_agents_dir = home / ".copilot" / "agents"
    if copilot_agents_dir.is_dir():
        for agent_file in sorted(copilot_agents_dir.glob("*.agent.md")):
            if has_aikito_marker(agent_file):
                continue
            s_name = (
                agent_file.name[:-9]
                if agent_file.name.endswith(".agent.md")
                else agent_file.stem
            )
            try:
                raw_content = agent_file.read_text(encoding="utf-8")
                meta, body = _parse_markdown_frontmatter(raw_content)
                desc = meta.get(
                    "description", f"Adopted subagent {s_name} from GitHub Copilot CLI"
                )
                platform_config = {
                    key: meta[key]
                    for key in (
                        "name",
                        "model",
                        "tools",
                        "target",
                        "disable-model-invocation",
                        "user-invocable",
                    )
                    if key in meta
                }

                existing = next(
                    (s for s in subagents if s.subagent_name == s_name), None
                )
                if existing:
                    if "github-copilot" not in existing.target_agents:
                        existing.target_agents.append("github-copilot")
                    existing.platform_configs["github-copilot"] = platform_config
                else:
                    subagents.append(
                        SubagentAdoption(
                            subagent_name=s_name,
                            description=str(desc),
                            role=s_name.capitalize(),
                            system_prompt=body,
                            target_agents=["github-copilot"],
                            source_file=agent_file,
                            platform_configs={"github-copilot": platform_config},
                        )
                    )
            except (PermissionError, OSError) as e:
                print(
                    f"[WARN] Failed to read subagent file '{agent_file}': {e}",
                    file=sys.stderr,
                )

    return subagents


def build_adopt_plan(aikito_dir: Path, home: Path) -> AdoptPlan:
    instructions = scan_instructions(aikito_dir, home)
    mcp_servers = scan_mcp_servers(aikito_dir, home)
    subagents = scan_subagents(aikito_dir, home)

    return AdoptPlan(
        aikito_dir=aikito_dir,
        home=home,
        instructions=instructions,
        mcp_servers=mcp_servers,
        subagents=subagents,
    )


def execute_adoption(
    plan: AdoptPlan, dry_run: bool = False, backup_dir: Optional[Path] = None
) -> bool:
    print(f"[INFO] {'Previewing' if dry_run else 'Executing'} Aikito adoption plan...")
    print(f"       Target workspace: {plan.aikito_dir}")

    # Create timestamped backup of local agent config files
    try:
        create_adopt_backup(plan, backup_dir=backup_dir, dry_run=dry_run)
    except Exception as exc:
        print(f"[ERROR] Failed during adoption backup: {exc}", file=sys.stderr)
        sys.exit(1)

    # 1. Instructions Adoption

    inst = plan.instructions
    if inst.sources:
        print("\n--- Global Instructions Adoption ---")
        if inst.has_conflict:
            print("[CONFLICT] Found conflicting global instructions:")
            if inst.target_path and inst.target_path.is_file():
                print(f"  - aikito: {inst.target_path}")
            for ag, p, _ in inst.sources:
                print(f"  - {ag}: {p}")
            print(
                "💡 Action required: Instructions conflict detected. Please manually review and merge into global/AGENTS.md"
            )
        else:
            ag_names = ", ".join(ag for ag, _, _ in inst.sources)
            print(f"[MERGE] Instructions from {ag_names} match perfectly.")
            if inst.target_path:
                if dry_run:
                    print(
                        f"[DRY-RUN WRITE] Would write merged instructions to {inst.target_path}"
                    )
                else:
                    inst.target_path.parent.mkdir(parents=True, exist_ok=True)
                    inst.target_path.write_text(inst.merged_content, encoding="utf-8")
                    print(f"[WRITE FILE] Updated {inst.target_path}")

    # 2. MCP Servers Adoption
    if plan.mcp_servers:
        print("\n--- MCP Servers Adoption ---")
        mcps_dir = plan.aikito_dir / "mcps"
        if not dry_run:
            mcps_dir.mkdir(parents=True, exist_ok=True)

        for srv in plan.mcp_servers:
            server_file = mcps_dir / f"{srv.server_name}.toml"
            if server_file.exists():
                print(f"[SKIP MCP] Server '{srv.server_name}' already present")
                continue
            content, log_msg = render_mcp_server_file(srv)
            if dry_run and log_msg.startswith("[ADOPT MCP]"):
                log_msg = log_msg.replace("[ADOPT MCP]", "[DRY-RUN MCP] Would import")
            print(log_msg)
            if not dry_run and content is not None:
                server_file.write_text(content, encoding="utf-8")
                print(f"[WRITE FILE] Created {server_file}")

    # 3. Subagents Adoption (Pre-render in memory)
    if plan.subagents:
        print("\n--- Subagents Adoption ---")
        sub_toml_path = plan.aikito_dir / "subagents.toml"
        existing_subs = (
            sub_toml_path.read_text(encoding="utf-8") if sub_toml_path.exists() else ""
        )

        new_subs_content, sub_logs = render_subagents_block(
            existing_subs, plan.subagents
        )
        for _, log_msg in sub_logs:
            if dry_run and log_msg.startswith("[ADOPT SUBAGENT]"):
                log_msg = log_msg.replace(
                    "[ADOPT SUBAGENT]", "[DRY-RUN SUBAGENT] Would import"
                )
            print(log_msg)

        if not dry_run and new_subs_content != existing_subs:
            sub_toml_path.write_text(new_subs_content, encoding="utf-8")
            print(f"[WRITE FILE] Updated {sub_toml_path}")
            instructions_dir = plan.aikito_dir / "subagents"
            instructions_dir.mkdir(parents=True, exist_ok=True)
            for sub in plan.subagents:
                instructions_path = instructions_dir / f"{sub.subagent_name}.md"
                if not instructions_path.exists():
                    instructions_path.write_text(
                        sub.system_prompt.rstrip() + "\n", encoding="utf-8"
                    )
                    print(f"[WRITE FILE] Created {instructions_path}")

    if dry_run:
        print("\n[DRY-RUN SUMMARY] Preview complete. No files were modified.")
        print(
            "💡 Run 'aikito adopt --apply' to apply these adoption changes to your workspace."
        )
    else:
        print("\n[SUCCESS] Adoption executed successfully!")
        print("💡 Run 'aikito status' to check workspace synchronization status.")

    return True


def _format_toml_key(key: str) -> str:
    # Bare key in TOML allows A-Z, a-z, 0-9, _, -
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


def render_mcp_server_file(
    srv: MCPServerAdoption,
) -> Tuple[Optional[str], str]:
    """
    Renders valid TOML content for an individual MCP server.
    Returns (toml_content_or_none, log_message)
    """
    cfg = srv.config_data
    lines = [f"agents = {_format_toml_value(srv.agents)}"]

    for key in ("command", "url", "args", "env", "transport", "headers"):
        if key in cfg and cfg[key] is not None:
            lines.append(f"{key} = {_format_toml_value(cfg[key])}")

    srv_block = "\n".join(lines) + "\n"

    try:
        tomllib.loads(srv_block)
        ag_str = ", ".join(srv.agents)
        return srv_block, f"[ADOPT MCP] Server '{srv.server_name}' (agents: [{ag_str}])"
    except Exception as exc:
        return (
            None,
            f"[SKIP MCP] Skipping invalid server name or config '{srv.server_name}': {exc}",
        )


def render_subagents_block(
    existing_content: str, subagents: List[SubagentAdoption]
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Renders valid TOML appended block for Subagents in memory.
    Returns (new_complete_toml_content, status_logs)
    """
    lines = []
    status_logs: List[Tuple[str, str]] = []

    for sub in subagents:
        safe_key = _format_toml_key(sub.subagent_name)
        header = f"[subagents.{safe_key}]"

        if header in existing_content:
            status_logs.append(
                (
                    sub.subagent_name,
                    f"[SKIP SUBAGENT] Subagent '{sub.subagent_name}' already present",
                )
            )
            continue

        sub_lines = [
            f"\n{header}",
            f"description = {_format_toml_value(sub.description)}",
            f"agents = {_format_toml_value(sub.target_agents)}",
        ]
        for agent_name, options in sorted(sub.platform_configs.items()):
            sub_lines.append(f"\n[{header[1:-1]}.{_format_toml_key(agent_name)}]")
            for key, value in sorted(options.items()):
                sub_lines.append(
                    f"{_format_toml_key(key)} = {_format_toml_value(value)}"
                )

        sub_block = "\n".join(sub_lines) + "\n"

        # Validate syntax
        try:
            tomllib.loads("test = true\n" + sub_block)
            lines.append(sub_block)
            status_logs.append(
                (
                    sub.subagent_name,
                    f"[ADOPT SUBAGENT] Subagent '{sub.subagent_name}' from {sub.source_file}",
                )
            )
        except Exception as exc:
            status_logs.append(
                (
                    sub.subagent_name,
                    f"[SKIP SUBAGENT] Skipping invalid subagent name/config '{sub.subagent_name}': {exc}",
                )
            )

    new_content = existing_content
    if lines:
        if not new_content.endswith("\n"):
            new_content += "\n"
        new_content += "".join(lines)

    return new_content, status_logs
