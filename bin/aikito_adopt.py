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


def scan_instructions(aikito_dir: Path, home: Path) -> InstructionsAdoption:
    candidates = [
        ("codex", home / ".codex" / "AGENTS.md"),
        ("claude-code", home / ".claude" / "CLAUDE.md"),
        ("agy", home / ".gemini" / "config" / "AGENTS.md"),
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
        return InstructionsAdoption(
            sources=sources,
            has_conflict=False,
            merged_content=sources[0][2],
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

    return list(adopted_servers.values())


def _parse_markdown_frontmatter(content: str) -> Tuple[Dict[str, str], str]:
    content = content.strip()
    if not content.startswith("---"):
        return {}, content

    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content

    frontmatter_raw = parts[1].strip()
    body = parts[2].strip()

    meta = {}
    for line in frontmatter_raw.splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("#"):
            k, v = line.split(":", 1)
            meta[k.strip()] = v.strip().strip("\"'")

    return meta, body


def scan_subagents(aikito_dir: Path, home: Path) -> List[SubagentAdoption]:
    subagents: List[SubagentAdoption] = []

    # Claude Code Subagents
    claude_agents_dir = home / ".claude" / "agents"
    if claude_agents_dir.is_dir():
        for agent_file in sorted(claude_agents_dir.glob("*.md")):
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
                        description=desc,
                        role=s_name.capitalize(),
                        system_prompt=body,
                        target_agents=["claude-code"],
                        source_file=agent_file,
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
            print("[CONFLICT] Found different global instructions across local agents:")
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

    # 2. MCP Servers Adoption (Pre-render in memory)
    if plan.mcp_servers:
        print("\n--- MCP Servers Adoption ---")
        mcps_toml_path = plan.aikito_dir / "mcps.toml"
        existing_mcps = (
            mcps_toml_path.read_text(encoding="utf-8")
            if mcps_toml_path.exists()
            else ""
        )

        new_mcps_content, mcp_logs = render_mcp_servers_block(
            existing_mcps, plan.mcp_servers
        )
        for _, log_msg in mcp_logs:
            if dry_run and log_msg.startswith("[ADOPT MCP]"):
                log_msg = log_msg.replace("[ADOPT MCP]", "[DRY-RUN MCP] Would import")
            print(log_msg)

        if not dry_run and new_mcps_content != existing_mcps:
            mcps_toml_path.write_text(new_mcps_content, encoding="utf-8")
            print(f"[WRITE FILE] Updated {mcps_toml_path}")

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


def render_mcp_servers_block(
    existing_content: str, mcp_servers: List[MCPServerAdoption]
) -> Tuple[str, List[Tuple[str, str]]]:
    """
    Renders valid TOML appended block for MCP servers in memory.
    Returns (new_complete_toml_content, status_logs)
    """
    lines = []
    status_logs: List[Tuple[str, str]] = []

    for srv in mcp_servers:
        safe_key = _format_toml_key(srv.server_name)
        header_1 = f"[servers.{safe_key}]"
        header_2 = f"[mcp_servers.{safe_key}]"

        if header_1 in existing_content or header_2 in existing_content:
            status_logs.append(
                (
                    srv.server_name,
                    f"[SKIP MCP] Server '{srv.server_name}' already present",
                )
            )
            continue

        cfg = srv.config_data
        srv_lines = [f"\n{header_1}"]
        srv_lines.append(f"agents = {_format_toml_value(srv.agents)}")

        for key in ("command", "url", "args", "env", "transport", "headers"):
            if key in cfg and cfg[key] is not None:
                srv_lines.append(f"{key} = {_format_toml_value(cfg[key])}")

        srv_block = "\n".join(srv_lines) + "\n"

        # Validate syntax of this single block
        try:
            tomllib.loads("test = true\n" + srv_block)
            lines.append(srv_block)
            ag_str = ", ".join(srv.agents)
            status_logs.append(
                (
                    srv.server_name,
                    f"[ADOPT MCP] Server '{srv.server_name}' (agents: [{ag_str}])",
                )
            )
        except Exception as exc:
            status_logs.append(
                (
                    srv.server_name,
                    f"[SKIP MCP] Skipping invalid server name or config '{srv.server_name}': {exc}",
                )
            )

    new_content = existing_content
    if lines:
        if not new_content.endswith("\n"):
            new_content += "\n"
        new_content += "".join(lines)

    return new_content, status_logs


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
            f"role = {_format_toml_value(sub.role)}",
            f"target_agents = {_format_toml_value(sub.target_agents)}",
        ]
        if sub.system_prompt:
            sub_lines.append(f"system_prompt = {_format_toml_value(sub.system_prompt)}")

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
