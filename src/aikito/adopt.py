"""
Adoption module for Aikito.
Scans existing local agent configurations (Instructions, MCP servers, Subagents),
creates timestamped backups, and adopts them into the Aikito workspace.
Supports --dry-run for previewing adoption changes without modifying workspace files.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .add import _parse_markdown_frontmatter
from .diagnostics import Finding, FindingAction
from .mcp import AgentDefinition, MCPConfigError, load_agents
from .render import render_finding_lines
from .subagent import has_aikito_marker
from .templating import (
    load_default_memory_instruction,
    load_global_agents_template,
)


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
    source_file: Optional[Path] = None


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
    errors: tuple[Finding, ...] = ()
    skipped: tuple[str, ...] = ()
    builtin_mcps: tuple[Tuple[str, str], ...] = ()

    @property
    def has_conflicts(self) -> bool:
        return self.instructions.has_conflict


@dataclass(frozen=True)
class AdoptSummary:
    instruction_updates: int
    mcp_imports: int
    subagent_imports: int
    conflicts: int
    errors: int
    skipped: int

    @property
    def total_changes(self) -> int:
        return self.instruction_updates + self.mcp_imports + self.subagent_imports


def _normalize_instructions_content(text: str) -> str:
    lines = [line.rstrip() for line in text.strip().splitlines()]
    return "\n".join(lines)


def _append_default_memory_instruction(content: str) -> str:
    default_instruction = load_default_memory_instruction()
    normalized = _normalize_instructions_content(content)
    default_rule = _normalize_instructions_content(default_instruction)
    if default_rule in normalized:
        return normalized + "\n"
    if not normalized:
        return default_instruction
    return f"{normalized}\n\n{default_instruction}"


def _merge_adopted_instructions(
    imported_content: str, target_path: Path
) -> tuple[bool, str | None]:
    if not target_path.is_file():
        return False, imported_content

    canonical_content = target_path.read_text(encoding="utf-8")
    canonical = _normalize_instructions_content(canonical_content)
    imported = _normalize_instructions_content(imported_content)
    default_template = _normalize_instructions_content(load_global_agents_template())

    if canonical == imported:
        return False, canonical_content

    merged = _append_default_memory_instruction(imported_content)
    if canonical == default_template:
        return False, merged
    if canonical == _normalize_instructions_content(merged):
        return False, canonical_content

    return True, None


class AdoptSkipError(ValueError):
    """Raised when an explicit adoption skip target is unknown."""


def _record_scan_error(
    errors: list[Finding] | None,
    message: str,
    *,
    source: Path,
    resource: str,
) -> None:
    if errors is None:
        print(f"[WARN] {message}", file=sys.stderr)
    else:
        errors.append(
            Finding(
                status="FAIL",
                code="adopt.source_invalid",
                resource=resource,
                source=str(source),
                message=f"Cannot inspect adoption source '{resource}'",
                reason=message,
                fix_hint=f"Repair or remove the invalid source: {source}",
            )
        )


def _record_mcp_url_conflict(
    errors: list[Finding] | None,
    server: MCPServerAdoption,
    incoming_agent: str,
    incoming_source: Path,
) -> None:
    message = (
        f"MCP server '{server.server_name}' has different URLs in "
        f"{server.source_agent} and {incoming_agent}"
    )
    if errors is None:
        print(f"[WARN] {message}", file=sys.stderr)
        return
    errors.append(
        Finding(
            status="FAIL",
            code="adopt.mcp_conflict",
            resource=f"mcp/{server.server_name}",
            source=f"{server.source_file}, {incoming_source}",
            message=f"MCP server '{server.server_name}' cannot be merged",
            reason=message,
            fix_hint="Align the Agent configurations or rename one MCP server",
        )
    )


def scan_instructions(
    aikito_dir: Path, home: Path, *, errors: list[Finding] | None = None
) -> InstructionsAdoption:
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
                _record_scan_error(
                    errors,
                    f"Unable to read instructions: {e}",
                    source=path,
                    resource="instructions",
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


def scan_mcp_servers(
    aikito_dir: Path,
    home: Path,
    *,
    errors: list[Finding] | None = None,
    builtin_mcps: list[Tuple[str, str]] | None = None,
) -> List[MCPServerAdoption]:
    adopted_servers: Dict[str, MCPServerAdoption] = {}

    agent_definitions: dict[str, AgentDefinition] = {}
    agent_builtin_mcps: Dict[str, set[str]] = {}
    agents_path = aikito_dir / "agents.toml"
    if agents_path.is_file():
        try:
            agent_definitions = load_agents(aikito_dir, home)
        except MCPConfigError as exc:
            _record_scan_error(
                errors,
                str(exc),
                source=agents_path,
                resource="agents",
            )
        else:
            for ag_name, ag_def in agent_definitions.items():
                if ag_def.mcp_builtin_servers:
                    agent_builtin_mcps[ag_name] = set(ag_def.mcp_builtin_servers)

    existing_mcps: set[str] = set()
    mcps_dir = aikito_dir / "mcps"
    if mcps_dir.is_dir():
        for p in mcps_dir.glob("*.toml"):
            if p.is_file():
                existing_mcps.add(p.stem)
    mcps_toml = aikito_dir / "mcps.toml"
    if mcps_toml.is_file():
        try:
            doc = tomllib.loads(mcps_toml.read_text(encoding="utf-8"))
            if "servers" in doc and isinstance(doc["servers"], dict):
                existing_mcps.update(doc["servers"].keys())
        except Exception:
            pass

    def _target_name(agent: str, canonical_name: str) -> str:
        definition = agent_definitions.get(agent)
        if definition and definition.mcp_name_style == "underscore":
            return canonical_name.replace("-", "_")
        return canonical_name

    def _resolve_canonical_name(name: str, agent: str) -> str:
        if name in existing_mcps:
            return name
        for canon in sorted(existing_mcps):
            if _target_name(agent, canon) == name:
                return canon

        if name in adopted_servers:
            return name
        for canon in adopted_servers:
            if _target_name(agent, canon) == name:
                return canon

        return name

    def _canonical_config(config: Dict[str, Any]) -> Dict[str, Any]:
        canonical = {
            key: config[key]
            for key in ("command", "url", "args", "env", "transport", "headers")
            if key in config and config[key] is not None
        }
        if "url" in canonical and "transport" not in canonical:
            canonical["transport"] = "remote"
        return canonical

    def _register_server(
        raw_name: str,
        agent: str,
        config: Dict[str, Any],
        source_file: Path,
    ) -> None:
        canon_name = _resolve_canonical_name(raw_name, agent)
        canonical_config = _canonical_config(config)
        if canon_name in adopted_servers:
            server = adopted_servers[canon_name]
            if server.config_data.get("url") != canonical_config.get("url"):
                _record_mcp_url_conflict(errors, server, agent, source_file)
                return
            if agent not in server.agents:
                server.agents.append(agent)
        else:
            adopted_servers[canon_name] = MCPServerAdoption(
                server_name=canon_name,
                agents=[agent],
                config_data=canonical_config,
                source_agent=agent,
                source_file=source_file,
            )

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
                            if isinstance(s_cfg, dict):
                                s_cfg_copy = dict(s_cfg)
                                if "env" in s_cfg_copy and isinstance(
                                    s_cfg_copy["env"], dict
                                ):
                                    sanitized_env, _ = _sanitize_mcp_env(
                                        s_cfg_copy["env"]
                                    )
                                    s_cfg_copy["env"] = sanitized_env

                                _register_server(
                                    s_name,
                                    "claude-code",
                                    s_cfg_copy,
                                    c_path,
                                )
            except json.JSONDecodeError as e:
                _record_scan_error(
                    errors,
                    f"Failed to parse JSON: {e}",
                    source=c_path,
                    resource="mcp",
                )
            except (PermissionError, OSError) as e:
                _record_scan_error(
                    errors,
                    f"Failed to read MCP config file: {e}",
                    source=c_path,
                    resource="mcp",
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

                            _register_server(
                                s_name,
                                "codex",
                                s_cfg_copy,
                                codex_toml,
                            )
        except tomllib.TOMLDecodeError as e:
            _record_scan_error(
                errors,
                f"Failed to parse TOML: {e}",
                source=codex_toml,
                resource="mcp",
            )
        except (PermissionError, OSError) as e:
            _record_scan_error(
                errors,
                f"Failed to read MCP config file: {e}",
                source=codex_toml,
                resource="mcp",
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

                            _register_server(
                                s_name,
                                "github-copilot",
                                s_cfg_copy,
                                copilot_mcp_config,
                            )
        except json.JSONDecodeError as e:
            _record_scan_error(
                errors,
                f"Failed to parse JSON: {e}",
                source=copilot_mcp_config,
                resource="mcp",
            )
        except (PermissionError, OSError) as e:
            _record_scan_error(
                errors,
                f"Failed to read MCP config file: {e}",
                source=copilot_mcp_config,
                resource="mcp",
            )

    result_servers: List[MCPServerAdoption] = []
    for srv in adopted_servers.values():
        is_builtin = all(
            _target_name(ag, srv.server_name) in agent_builtin_mcps.get(ag, set())
            for ag in srv.agents
        )
        if is_builtin:
            if builtin_mcps is not None:
                for ag in srv.agents:
                    builtin_mcps.append((srv.server_name, ag))
            continue
        result_servers.append(srv)

    return result_servers


def scan_subagents(
    aikito_dir: Path, home: Path, *, errors: list[Finding] | None = None
) -> List[SubagentAdoption]:
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
                _record_scan_error(
                    errors,
                    f"Failed to read subagent file: {e}",
                    source=agent_file,
                    resource=f"subagent/{s_name}",
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
                _record_scan_error(
                    errors,
                    f"Failed to read subagent file: {e}",
                    source=agent_file,
                    resource=f"subagent/{s_name}",
                )

    return subagents


def build_adopt_plan(aikito_dir: Path, home: Path) -> AdoptPlan:
    errors: list[Finding] = []
    builtin_mcps: list[Tuple[str, str]] = []
    instructions = scan_instructions(aikito_dir, home, errors=errors)
    mcp_servers = scan_mcp_servers(
        aikito_dir, home, errors=errors, builtin_mcps=builtin_mcps
    )
    subagents = scan_subagents(aikito_dir, home, errors=errors)

    return AdoptPlan(
        aikito_dir=aikito_dir,
        home=home,
        instructions=instructions,
        mcp_servers=mcp_servers,
        subagents=subagents,
        errors=tuple(errors),
        builtin_mcps=tuple(builtin_mcps),
    )


def apply_adopt_skips(plan: AdoptPlan, requested: list[str] | None) -> AdoptPlan:
    """Return a plan with explicitly named resources removed before validation."""
    requested_set = set(requested or [])
    if not requested_set:
        return plan

    available: set[str] = set()
    if plan.instructions.sources:
        available.add("instructions")
    available.update(f"mcp/{server.server_name}" for server in plan.mcp_servers)
    available.update(f"subagent/{sub.subagent_name}" for sub in plan.subagents)
    unknown = sorted(requested_set - available)
    if unknown:
        available_text = ", ".join(sorted(available)) or "none"
        raise AdoptSkipError(
            f"Unknown adoption skip target(s): {', '.join(unknown)}. "
            f"Available targets: {available_text}"
        )

    instructions = plan.instructions
    if "instructions" in requested_set:
        instructions = InstructionsAdoption(
            sources=[],
            target_path=plan.instructions.target_path,
        )
    return replace(
        plan,
        instructions=instructions,
        mcp_servers=[
            server
            for server in plan.mcp_servers
            if f"mcp/{server.server_name}" not in requested_set
        ],
        subagents=[
            sub
            for sub in plan.subagents
            if f"subagent/{sub.subagent_name}" not in requested_set
        ],
        skipped=tuple(sorted(requested_set)),
    )


def summarize_adopt_plan(plan: AdoptPlan) -> AdoptSummary:
    instruction_updates = 0
    inst = plan.instructions
    if inst.sources and not inst.has_conflict and inst.merged_content is not None:
        current = ""
        if inst.target_path and inst.target_path.is_file():
            current = inst.target_path.read_text(encoding="utf-8")
        if _normalize_instructions_content(current) != _normalize_instructions_content(
            inst.merged_content
        ):
            instruction_updates = 1

    mcp_imports = 0
    for server in plan.mcp_servers:
        target = plan.aikito_dir / "mcps" / f"{server.server_name}.toml"
        content, _ = render_mcp_server_file(server)
        if not target.exists() and content is not None:
            mcp_imports += 1

    subagents_path = plan.aikito_dir / "subagents.toml"
    existing_subagents = (
        subagents_path.read_text(encoding="utf-8") if subagents_path.is_file() else ""
    )
    _, subagent_logs = render_subagents_block(existing_subagents, plan.subagents)
    subagent_imports = sum(
        log.startswith("[ADOPT SUBAGENT]") for _, log in subagent_logs
    )

    return AdoptSummary(
        instruction_updates=instruction_updates,
        mcp_imports=mcp_imports,
        subagent_imports=subagent_imports,
        conflicts=1 if plan.has_conflicts else 0,
        errors=len(collect_adopt_findings(plan)) - (1 if plan.has_conflicts else 0),
        skipped=len(plan.skipped),
    )


def collect_adopt_findings(plan: AdoptPlan) -> tuple[Finding, ...]:
    """Return every actionable issue that blocks the current adoption plan."""
    findings = list(plan.errors)
    if plan.has_conflicts:
        source_paths = ", ".join(str(path) for _, path, _ in plan.instructions.sources)
        target = (
            plan.instructions.target_path or plan.aikito_dir / "global" / "AGENTS.md"
        )
        findings.append(
            Finding(
                status="FAIL",
                code="adopt.instructions_conflict",
                resource="instructions",
                source=source_paths,
                message="Global instructions cannot be adopted automatically",
                reason="Detected instruction sources do not match",
                fix_hint=f"Review and merge the sources into {target}",
                actions=(
                    FindingAction("Review", "aikito adopt --dry-run --verbose"),
                    FindingAction("Skip", "aikito adopt --skip instructions"),
                ),
            )
        )
    for server in plan.mcp_servers:
        content, log_message = render_mcp_server_file(server)
        if content is None:
            resource = f"mcp/{server.server_name}"
            findings.append(
                Finding(
                    status="FAIL",
                    code="adopt.invalid_mcp",
                    resource=resource,
                    source=str(server.source_file or server.source_agent),
                    message=f"MCP server '{server.server_name}' cannot be adopted",
                    reason=log_message,
                    fix_hint="Repair or remove the definition in the source file",
                    actions=(FindingAction("Skip", f"aikito adopt --skip {resource}"),),
                )
            )

    subagents_path = plan.aikito_dir / "subagents.toml"
    existing_subagents = (
        subagents_path.read_text(encoding="utf-8") if subagents_path.is_file() else ""
    )
    _, subagent_logs = render_subagents_block(existing_subagents, plan.subagents)
    subagents_by_name = {sub.subagent_name: sub for sub in plan.subagents}
    for subagent_name, log_message in subagent_logs:
        if "Skipping invalid" not in log_message:
            continue
        resource = f"subagent/{subagent_name}"
        source = subagents_by_name[subagent_name].source_file
        findings.append(
            Finding(
                status="FAIL",
                code="adopt.invalid_subagent",
                resource=resource,
                source=str(source),
                message=f"Subagent '{subagent_name}' cannot be adopted",
                reason=log_message,
                fix_hint="Repair or remove the definition in the source file",
                actions=(FindingAction("Skip", f"aikito adopt --skip {resource}"),),
            )
        )
    return tuple(findings)


def _print_adopt_summary(summary: AdoptSummary, skipped: tuple[str, ...]) -> None:
    print("Adoption plan")
    print()
    print(f"  Instructions: {summary.instruction_updates} update(s)")
    print(f"  MCP servers:  {summary.mcp_imports} import(s)")
    print(f"  Subagents:    {summary.subagent_imports} import(s)")
    print(f"  Conflicts:    {summary.conflicts}")
    print(f"  Errors:       {summary.errors}")
    print(f"  Skipped:      {summary.skipped}")
    for resource in skipped:
        print(f"  [SKIP] {resource} (explicitly requested)")


def execute_adoption(
    plan: AdoptPlan,
    dry_run: bool = False,
    backup_dir: Optional[Path] = None,
    *,
    verbose: bool = True,
) -> bool:
    summary = summarize_adopt_plan(plan)
    _print_adopt_summary(summary, plan.skipped)

    if summary.total_changes == 0 and not plan.has_conflicts and not summary.errors:
        if verbose and plan.builtin_mcps:
            print("\n--- MCP Servers Adoption ---")
            for server_name, agent_name in plan.builtin_mcps:
                print(f"[SKIP MCP] Server '{server_name}' is built-in to {agent_name}")
        print("\n[OK] No adoptable Agent configuration found. No files were modified.")
        return True

    findings = collect_adopt_findings(plan)
    if findings:
        print("", file=sys.stderr)
        for finding in findings:
            for line in render_finding_lines(finding):
                print(line, file=sys.stderr)
        print(
            "[ERROR] Adoption blocked; no files were modified. Resolve the "
            "problems above and rerun 'aikito adopt'.",
            file=sys.stderr,
        )
        return False

    print("\nSafe to apply")
    if dry_run and not verbose:
        print("[DRY-RUN] No files were modified.")
        return True

    # Create timestamped backup of local agent config files
    try:
        create_adopt_backup(plan, backup_dir=backup_dir, dry_run=dry_run)
    except Exception as exc:
        print(f"[ERROR] Failed during adoption backup: {exc}", file=sys.stderr)
        sys.exit(1)

    # 1. Instructions Adoption

    inst = plan.instructions
    if inst.sources:
        if verbose:
            print("\n--- Global Instructions Adoption ---")
            ag_names = ", ".join(ag for ag, _, _ in inst.sources)
            print(f"[MERGE] Instructions from {ag_names} match perfectly.")
            for agent_name, source_path, _ in inst.sources:
                print(f"[SOURCE] {agent_name}: {source_path}")
        if inst.target_path and summary.instruction_updates:
            if dry_run:
                if verbose:
                    print(
                        f"[DRY-RUN WRITE] Would write merged instructions to {inst.target_path}"
                    )
            else:
                _write_text_atomic(inst.target_path, inst.merged_content or "")
                if verbose:
                    print(f"[WRITE FILE] Updated {inst.target_path}")

    # 2. MCP Servers Adoption
    if plan.mcp_servers or plan.builtin_mcps:
        if verbose:
            print("\n--- MCP Servers Adoption ---")
        if verbose and plan.builtin_mcps:
            for s_name, ag_name in plan.builtin_mcps:
                print(f"[SKIP MCP] Server '{s_name}' is built-in to {ag_name}")
        mcps_dir = plan.aikito_dir / "mcps"
        if not dry_run and plan.mcp_servers:
            mcps_dir.mkdir(parents=True, exist_ok=True)

        for srv in plan.mcp_servers:
            server_file = mcps_dir / f"{srv.server_name}.toml"
            if server_file.exists():
                if verbose:
                    print(f"[SKIP MCP] Server '{srv.server_name}' already present")
                continue
            content, log_msg = render_mcp_server_file(srv)
            if dry_run and log_msg.startswith("[ADOPT MCP]"):
                log_msg = log_msg.replace("[ADOPT MCP]", "[DRY-RUN MCP] Would import")
            if verbose:
                print(log_msg)
            if not dry_run and content is not None:
                _write_text_atomic(server_file, content)
                if verbose:
                    print(f"[WRITE FILE] Created {server_file}")

    # 3. Subagents Adoption (Pre-render in memory)
    if plan.subagents:
        if verbose:
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
            if verbose:
                print(log_msg)

        if not dry_run and new_subs_content != existing_subs:
            _write_text_atomic(sub_toml_path, new_subs_content)
            if verbose:
                print(f"[WRITE FILE] Updated {sub_toml_path}")
            instructions_dir = plan.aikito_dir / "subagents"
            for sub in plan.subagents:
                instructions_path = instructions_dir / f"{sub.subagent_name}.md"
                if not instructions_path.exists():
                    _write_text_atomic(
                        instructions_path, sub.system_prompt.rstrip() + "\n"
                    )
                    if verbose:
                        print(f"[WRITE FILE] Created {instructions_path}")

    if dry_run:
        print("\n[DRY-RUN] No files were modified.")
    else:
        print("\n[SUCCESS] Adoption executed successfully!")
        print("Next step: Run 'aikito sync' to check and apply runtime changes.")

    return True


def _write_text_atomic(target: Path, content: str) -> None:
    """Replace one workspace text file without exposing partial contents."""
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.tmp.{os.getpid()}")
    temporary.write_text(content, encoding="utf-8")
    temporary.replace(target)


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
    if (
        not srv.server_name
        or Path(srv.server_name).name != srv.server_name
        or "\\" in srv.server_name
    ):
        return None, f"[ERROR] Invalid MCP server name: {srv.server_name!r}"
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
