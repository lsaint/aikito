"""Synchronize canonical Aikito MCP definitions into supported agent configs."""

import base64
import hashlib
import json
import os
import queue
import re
import shutil
import stat
import subprocess
import tempfile
import threading
import tomllib
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit


STATE_VERSION = 1
DEFAULT_MCP_CONFIG = Path("mcps.toml")
DEFAULT_AGENTS_CONFIG = Path("agents.toml")
STATE_FILE = Path(".local/state/aikito/mcp-state.json")
BACKUP_DIR = Path(".local/state/aikito/backups")
URL_PATTERN = re.compile(r"https?://[^\s<>\"']+")
SENSITIVE_URL_PARAMETERS = {"code", "access_token", "refresh_token", "id_token"}
BROWSER_HELPER = """#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
from pathlib import Path


urls = [argument for argument in sys.argv[1:] if argument.startswith(("http://", "https://"))]
url_file = os.environ.get("AIKITO_AUTH_URL_FILE")
if url_file and urls:
    with Path(url_file).open("a", encoding="utf-8") as handle:
        handle.writelines(f"{url}\\n" for url in urls)

if os.environ.get("AIKITO_OPEN_BROWSER") == "1":
    browser_env = os.environ.copy()
    browser_env.pop("BROWSER", None)
    if sys.platform == "darwin":
        command = ["/usr/bin/open"]
    else:
        opener = shutil.which("xdg-open")
        command = [opener] if opener else []
    for url in urls:
        if command:
            subprocess.Popen(
                [*command, url],
                env=browser_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
"""


class MCPConfigError(RuntimeError):
    """Raised when an MCP definition or target config cannot be safely managed."""


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int

    @property
    def value(self) -> Any:
        return json.loads(self.text) if self.kind == "string" else self.text


@dataclass(frozen=True)
class AgentSpec:
    agent: str
    server: str
    config_path: Path
    config_format: str
    target_name: str
    desired: dict[str, Any]
    enabled: bool = True
    reason: str = ""
    live_command: tuple[str, ...] = ()
    auth_command: tuple[str, ...] = ()
    contains_secret: bool = False

    @property
    def state_key(self) -> str:
        return f"{self.agent}:{self.server}"


@dataclass(frozen=True)
class AgentDefinition:
    """Static identity and paths for one agent, loaded from agents.toml."""

    name: str
    display_name: str
    instruction_path: Path | None
    skills_path: Path | None
    mcp_config_path: Path | None
    mcp_config_format: str
    mcp_name_style: str
    mcp_reason: str
    mcp_live_command: tuple[str, ...]
    mcp_auth_command: tuple[str, ...]

    @property
    def supports_mcp(self) -> bool:
        return self.mcp_config_path is not None


@dataclass(frozen=True)
class BasicTokenAuth:
    """Keeps credential policy canonical while resolving secrets only at runtime."""

    account_email: str
    token_env: str
    authorization_env: str

    def authorization_header(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise MCPConfigError(
                f"Required MCP credential environment variable is missing: {self.token_env}"
            )
        credentials = f"{self.account_email}:{token}".encode()
        return f"Basic {base64.b64encode(credentials).decode()}"


@dataclass(frozen=True)
class LiveMCPResult:
    """Result of one agent CLI's live MCP status command."""

    agent: str
    command: tuple[str, ...]
    status: str
    returncode: int | None
    output: str = ""


def _resolve_home_path(home: Path, value: Any, field: str, agent: str) -> Path:
    if not isinstance(value, str) or not value:
        raise MCPConfigError(f"Agent '{agent}' requires a string '{field}'")
    return home / value


def load_agents(aikito_dir: Path, home: Path) -> dict[str, AgentDefinition]:
    """Load the agent registry from agents.toml (the single source of truth)."""
    config_path = aikito_dir / DEFAULT_AGENTS_CONFIG
    if not config_path.exists():
        raise MCPConfigError(f"Agents config not found: {config_path}")
    try:
        document = tomllib.loads(config_path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, OSError) as exc:
        raise MCPConfigError(f"Invalid agents config {config_path}: {exc}") from exc

    agents = document.get("agents")
    if not isinstance(agents, dict) or not agents:
        raise MCPConfigError(f"'agents' must be a non-empty table in {config_path}")

    definitions: dict[str, AgentDefinition] = {}
    for name, spec in agents.items():
        if not isinstance(spec, dict):
            raise MCPConfigError(f"Agent '{name}' must be a table")

        instruction_value = spec.get("instruction_path")
        instruction_path = (
            _resolve_home_path(home, instruction_value, "instruction_path", name)
            if instruction_value is not None
            else None
        )
        skills_value = spec.get("skills_path")
        skills_path = (
            _resolve_home_path(home, skills_value, "skills_path", name)
            if skills_value is not None
            else None
        )

        mcp = spec.get("mcp")
        if mcp is None:
            mcp_config_path = None
            mcp_config_format = "unsupported"
            mcp_name_style = "verbatim"
            mcp_reason = ""
            mcp_live_command: tuple[str, ...] = ()
            mcp_auth_command: tuple[str, ...] = ()
        else:
            if not isinstance(mcp, dict):
                raise MCPConfigError(f"Agent '{name}' mcp section must be a table")
            mcp_config_path = _resolve_home_path(
                home, mcp.get("config_path"), "mcp.config_path", name
            )
            mcp_config_format = str(mcp.get("config_format", "unsupported"))
            mcp_name_style = str(mcp.get("name_style", "verbatim"))
            mcp_reason = str(mcp.get("reason", ""))
            mcp_live_command = tuple(mcp.get("live_command", ()) or ())
            mcp_auth_command = tuple(mcp.get("auth_command", ()) or ())

        definitions[name] = AgentDefinition(
            name=name,
            display_name=str(spec.get("display_name", name)),
            instruction_path=instruction_path,
            skills_path=skills_path,
            mcp_config_path=mcp_config_path,
            mcp_config_format=mcp_config_format,
            mcp_name_style=mcp_name_style,
            mcp_reason=mcp_reason,
            mcp_live_command=mcp_live_command,
            mcp_auth_command=mcp_auth_command,
        )

    return definitions


def _target_name(name_style: str, server_name: str) -> str:
    if name_style == "underscore":
        return server_name.replace("-", "_")
    return server_name


def _render_command(template: tuple[str, ...], target: str) -> tuple[str, ...]:
    return tuple(part.replace("{target}", target) for part in template)


def _tokenize_jsonc(text: str) -> list[Token]:
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise MCPConfigError("Unterminated JSONC block comment")
            index = end + 2
            continue
        if char == '"':
            start = index
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            else:
                raise MCPConfigError("Unterminated JSONC string")
            tokens.append(Token("string", text[start:index], start, index))
            continue
        if char in "{}[]:,":
            tokens.append(Token(char, char, index, index + 1))
            index += 1
            continue

        start = index
        while (
            index < len(text)
            and not text[index].isspace()
            and text[index] not in "{}[]:,/"
        ):
            index += 1
        if start == index:
            raise MCPConfigError(f"Unexpected JSONC character at offset {index}")
        tokens.append(Token("literal", text[start:index], start, index))
    return tokens


def _parse_json_value(tokens: list[Token], index: int) -> tuple[Any, int]:
    if index >= len(tokens):
        raise MCPConfigError("Unexpected end of JSONC input")
    token = tokens[index]
    if token.kind == "string":
        return token.value, index + 1
    if token.kind == "{":
        result = {}
        index += 1
        while index < len(tokens) and tokens[index].kind != "}":
            key_token = tokens[index]
            if key_token.kind != "string":
                raise MCPConfigError("JSONC object keys must be strings")
            if index + 1 >= len(tokens) or tokens[index + 1].kind != ":":
                raise MCPConfigError("JSONC object key is missing ':'")
            value, index = _parse_json_value(tokens, index + 2)
            result[key_token.value] = value
            if index < len(tokens) and tokens[index].kind == ",":
                index += 1
        if index >= len(tokens) or tokens[index].kind != "}":
            raise MCPConfigError("Unterminated JSONC object")
        return result, index + 1
    if token.kind == "[":
        result = []
        index += 1
        while index < len(tokens) and tokens[index].kind != "]":
            value, index = _parse_json_value(tokens, index)
            result.append(value)
            if index < len(tokens) and tokens[index].kind == ",":
                index += 1
        if index >= len(tokens) or tokens[index].kind != "]":
            raise MCPConfigError("Unterminated JSONC array")
        return result, index + 1

    literals = {"true": True, "false": False, "null": None}
    if token.text in literals:
        return literals[token.text], index + 1
    try:
        return json.loads(token.text), index + 1
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"Invalid JSONC literal: {token.text}") from exc


def _parse_jsonc(text: str) -> Any:
    tokens = _tokenize_jsonc(text)
    if not tokens:
        return {}
    value, next_index = _parse_json_value(tokens, 0)
    if next_index != len(tokens):
        raise MCPConfigError("Unexpected content after JSONC document")
    return value


def _object_members(
    tokens: list[Token], object_index: int
) -> tuple[dict[str, tuple[int, int, int]], int]:
    if tokens[object_index].kind != "{":
        raise MCPConfigError("Expected a JSONC object")

    members = {}
    index = object_index + 1
    while index < len(tokens) and tokens[index].kind != "}":
        key_index = index
        if tokens[key_index].kind != "string":
            raise MCPConfigError("JSONC object keys must be strings")
        if key_index + 1 >= len(tokens) or tokens[key_index + 1].kind != ":":
            raise MCPConfigError("JSONC object key is missing ':'")
        value_index = key_index + 2
        _, next_index = _parse_json_value(tokens, value_index)
        members[tokens[key_index].value] = (key_index, value_index, next_index - 1)
        index = next_index
        if index < len(tokens) and tokens[index].kind == ",":
            index += 1

    if index >= len(tokens) or tokens[index].kind != "}":
        raise MCPConfigError("Unterminated JSONC object")
    return members, index


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    return text[line_start:offset]


def _format_json_value(value: dict[str, Any], indent: str) -> str:
    lines = json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    return lines[0] + "".join(f"\n{indent}{line}" for line in lines[1:])


def get_jsonc_server(text: str, server_name: str) -> dict[str, Any] | None:
    document = _parse_jsonc(text)
    if not isinstance(document, dict):
        raise MCPConfigError("OpenCode config root must be an object")
    mcp = document.get("mcp")
    if mcp is None:
        return None
    if not isinstance(mcp, dict):
        raise MCPConfigError("OpenCode 'mcp' must be an object")
    server = mcp.get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(f"OpenCode MCP server '{server_name}' must be an object")
    return server


def get_agy_json_server(text: str, server_name: str) -> dict[str, Any] | None:
    return get_mcp_json_server(text, server_name, "agy")


def get_claude_json_server(text: str, server_name: str) -> dict[str, Any] | None:
    return get_mcp_json_server(text, server_name, "Claude Code")


def get_mcp_json_server(
    text: str, server_name: str, config_name: str
) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        document = json.loads(text)
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"Invalid {config_name} JSON config: {exc}") from exc
    if not isinstance(document, dict):
        raise MCPConfigError(f"{config_name} config root must be an object")
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{config_name} 'mcpServers' must be an object")
    server = servers.get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(
            f"{config_name} MCP server '{server_name}' must be an object"
        )
    return server


def update_agy_json_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    return update_mcp_json_server(text, server_name, desired, "agy")


def update_claude_json_server(
    text: str, server_name: str, desired: dict[str, Any]
) -> str:
    return update_mcp_json_server(text, server_name, desired, "Claude Code")


def update_mcp_json_server(
    text: str,
    server_name: str,
    desired: dict[str, Any],
    config_name: str,
) -> str:
    if text.strip():
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPConfigError(f"Invalid {config_name} JSON config: {exc}") from exc
    else:
        document = {}
    if not isinstance(document, dict):
        raise MCPConfigError(f"{config_name} config root must be an object")
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{config_name} 'mcpServers' must be an object")
    servers[server_name] = desired
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def update_jsonc_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    if not text.strip():
        text = "{}\n"
    tokens = _tokenize_jsonc(text)
    if not tokens or tokens[0].kind != "{":
        raise MCPConfigError("OpenCode config root must be an object")

    root_members, root_close = _object_members(tokens, 0)
    mcp_member = root_members.get("mcp")
    if mcp_member is None:
        root_indent = _line_indent(text, tokens[0].start)
        child_indent = root_indent + "  "
        server_indent = child_indent + "  "
        formatted = _format_json_value(desired, server_indent)
        property_text = (
            f'{child_indent}"mcp": {{\n'
            f'{server_indent}"{server_name}": {formatted}\n'
            f"{child_indent}}}"
        )
        previous_token = tokens[root_close - 1]
        prefix = "\n" if previous_token.kind == "," or not root_members else ",\n"
        suffix = f"\n{root_indent}"
        close_offset = tokens[root_close].start
        return (
            text[:close_offset] + prefix + property_text + suffix + text[close_offset:]
        )

    _, mcp_value_index, _ = mcp_member
    if tokens[mcp_value_index].kind != "{":
        raise MCPConfigError("OpenCode 'mcp' must be an object")
    mcp_members, mcp_close = _object_members(tokens, mcp_value_index)
    server_member = mcp_members.get(server_name)
    if server_member is not None:
        key_index, value_start_index, value_end_index = server_member
        indent = _line_indent(text, tokens[key_index].start)
        formatted = _format_json_value(desired, indent)
        start = tokens[value_start_index].start
        end = tokens[value_end_index].end
        return text[:start] + formatted + text[end:]

    mcp_key_index, _, _ = mcp_member
    mcp_indent = _line_indent(text, tokens[mcp_key_index].start)
    child_indent = mcp_indent + "  "
    formatted = _format_json_value(desired, child_indent)
    property_text = f'{child_indent}"{server_name}": {formatted}'
    close_offset = tokens[mcp_close].start
    previous_token = tokens[mcp_close - 1]
    separator = "\n" if previous_token.kind == "," or not mcp_members else ",\n"
    return (
        text[:close_offset]
        + separator
        + property_text
        + f"\n{mcp_indent}"
        + text[close_offset:]
    )


def get_toml_server(text: str, server_name: str) -> dict[str, Any] | None:
    if not text.strip():
        return None
    try:
        document = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise MCPConfigError(f"Invalid Codex TOML config: {exc}") from exc
    server = document.get("mcp_servers", {}).get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(f"Codex MCP server '{server_name}' must be a table")
    return server


def _toml_value(value: Any) -> str:
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, dict):
        entries = ", ".join(
            f"{json.dumps(str(key))} = {_toml_value(item)}"
            for key, item in value.items()
        )
        return f"{{ {entries} }}"
    raise MCPConfigError(f"Unsupported managed TOML value: {value!r}")


def update_toml_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    header = f"[mcp_servers.{server_name}]"
    body = "\n".join(f"{key} = {_toml_value(value)}" for key, value in desired.items())
    section = f"{header}\n{body}\n"
    header_pattern = re.compile(rf"(?m)^[ \t]*{re.escape(header)}[ \t]*(?:#.*)?$")
    match = header_pattern.search(text)
    if match is None:
        separator = "" if not text else ("\n" if text.endswith("\n") else "\n\n")
        if text and not text.endswith("\n\n"):
            separator += "\n"
        return text + separator + section

    next_header = re.search(
        r"(?m)^[ \t]*\[[^\]]+\][ \t]*(?:#.*)?$", text[match.end() :]
    )
    end = len(text) if next_header is None else match.end() + next_header.start()
    return text[: match.start()] + section + text[end:]


def _fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _load_state(home: Path) -> dict[str, Any]:
    path = home / STATE_FILE
    if not path.exists():
        return {"version": STATE_VERSION, "entries": {}}
    try:
        state = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        raise MCPConfigError(f"Cannot read MCP state file {path}: {exc}") from exc
    if state.get("version") != STATE_VERSION or not isinstance(
        state.get("entries"), dict
    ):
        raise MCPConfigError(f"Unsupported MCP state file: {path}")
    return state


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    if mode is not None:
        temp_path.chmod(mode)
    os.replace(temp_path, path)


def _save_state(home: Path, state: dict[str, Any]) -> None:
    path = home / STATE_FILE
    content = json.dumps(state, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _atomic_write(path, content)


def _backup_config(home: Path, spec: AgentSpec) -> Path | None:
    if not spec.config_path.exists():
        return None
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / BACKUP_DIR / spec.agent / f"{timestamp}-{spec.config_path.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(spec.config_path, backup)
    return backup


def _load_basic_token_auth(
    server_name: str, server: dict[str, Any]
) -> BasicTokenAuth | None:
    authentication = server.get("authentication")
    if authentication is None:
        return None
    if not isinstance(authentication, dict):
        raise MCPConfigError(f"Server '{server_name}' authentication must be a table")
    method = authentication.get("method")
    if method != "basic_api_token":
        raise MCPConfigError(
            f"Server '{server_name}' has unsupported authentication method: {method!r}"
        )

    fields = {}
    for field in ("account_email", "token_env", "authorization_env"):
        value = authentication.get(field)
        if not isinstance(value, str) or not value:
            raise MCPConfigError(
                f"Server '{server_name}' authentication requires '{field}'"
            )
        fields[field] = value
    return BasicTokenAuth(**fields)


def _build_desired(
    config_format: str,
    url: str,
    override: dict[str, Any],
    authentication: BasicTokenAuth | None,
) -> tuple[dict[str, Any], bool]:
    """Construct the format-bound MCP payload for a target config."""
    if config_format == "toml":
        desired: dict[str, Any] = {"url": url}
        if authentication:
            desired["auth"] = "oauth"
            desired["env_http_headers"] = {
                "Authorization": authentication.authorization_env
            }
        return desired, False
    if config_format == "jsonc":
        desired = {
            "type": "remote",
            "url": url,
            "enabled": True,
            "timeout": override.get("timeout", 30000),
        }
        if authentication:
            desired["oauth"] = False
            desired["headers"] = {
                "Authorization": f"{{env:{authentication.authorization_env}}}"
            }
        return desired, False
    if config_format == "agy_json":
        desired = {"serverUrl": url}
        if authentication:
            # agy 1.1.8 accepts headers but does not document environment
            # interpolation, so only its generated runtime config contains this.
            desired["headers"] = {
                "Authorization": authentication.authorization_header()
            }
        return desired, authentication is not None
    if config_format == "claude_json":
        desired = {"type": "http", "url": url}
        if authentication:
            desired["headers"] = {
                "Authorization": f"${{{authentication.authorization_env}}}"
            }
        return desired, False
    # Unsupported formats never get written; payload is informational only.
    return {}, False


def load_agent_specs(aikito_dir: Path, home: Path) -> list[AgentSpec]:
    config_path = aikito_dir / DEFAULT_MCP_CONFIG
    if not config_path.exists():
        raise MCPConfigError(f"MCP config not found: {config_path}")
    try:
        document = tomllib.loads(config_path.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise MCPConfigError(f"Invalid MCP config {config_path}: {exc}") from exc

    servers = document.get("servers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError(f"'servers' must be a table in {config_path}")

    registry = load_agents(aikito_dir, home)

    specs = []
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            raise MCPConfigError(f"Server '{server_name}' must be a table")
        if server.get("transport") != "remote":
            raise MCPConfigError(f"Server '{server_name}' must use remote transport")
        url = server.get("url")
        agents = server.get("agents")
        if not isinstance(url, str) or not url:
            raise MCPConfigError(f"Server '{server_name}' requires a URL")
        if not isinstance(agents, list) or not all(
            isinstance(agent, str) for agent in agents
        ):
            raise MCPConfigError(f"Server '{server_name}' requires an agents list")
        overrides = server.get("overrides", {})
        if not isinstance(overrides, dict):
            raise MCPConfigError(f"Server '{server_name}' overrides must be a table")
        authentication = _load_basic_token_auth(server_name, server)

        for agent in agents:
            definition = registry.get(agent)
            if definition is None:
                raise MCPConfigError(
                    f"Server '{server_name}' references unknown agent '{agent}'; "
                    f"define it in {DEFAULT_AGENTS_CONFIG}"
                )

            override = overrides.get(agent, {})
            if not isinstance(override, dict):
                raise MCPConfigError(
                    f"Server '{server_name}' override for {agent} must be a table"
                )
            enabled = override.get("enabled", True)
            reason = str(override.get("reason", ""))

            if (
                not definition.supports_mcp
                or definition.mcp_config_format == "unsupported"
            ):
                specs.append(
                    AgentSpec(
                        agent=agent,
                        server=server_name,
                        config_path=definition.mcp_config_path or Path(),
                        config_format="unsupported",
                        target_name=server_name,
                        desired={},
                        enabled=False,
                        reason=(
                            reason
                            or definition.mcp_reason
                            or f"MCP synchronization is not supported for agent '{agent}'"
                        ),
                    )
                )
                continue

            name_style = definition.mcp_name_style
            default_target = _target_name(name_style, server_name)
            target_name = str(override.get("name", default_target))
            desired, contains_secret = _build_desired(
                definition.mcp_config_format,
                url,
                override,
                authentication,
            )
            specs.append(
                AgentSpec(
                    agent=agent,
                    server=server_name,
                    config_path=definition.mcp_config_path,
                    config_format=definition.mcp_config_format,
                    target_name=target_name,
                    desired=desired,
                    enabled=bool(enabled),
                    reason=reason,
                    live_command=_render_command(
                        definition.mcp_live_command, target_name
                    ),
                    auth_command=(
                        ()
                        if authentication
                        else _render_command(definition.mcp_auth_command, target_name)
                    ),
                    contains_secret=contains_secret,
                )
            )
    return specs


def run_live_mcp_commands(
    commands: dict[str, tuple[str, ...]], timeout: int = 45
) -> list[LiveMCPResult]:
    """Run one live MCP status command per agent and normalize its outcome."""
    results = []
    for agent, command in commands.items():
        if shutil.which(command[0]) is None:
            results.append(LiveMCPResult(agent, command, "SKIP", None))
            continue
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            results.append(LiveMCPResult(agent, command, "TIMEOUT", None))
            continue

        output = "\n".join(
            part.strip() for part in (result.stdout, result.stderr) if part.strip()
        )
        status = "OK" if result.returncode == 0 else "ERROR"
        results.append(LiveMCPResult(agent, command, status, result.returncode, output))
    return results


def read_entry(spec: AgentSpec, text: str) -> dict[str, Any] | None:
    if spec.config_format == "toml":
        return get_toml_server(text, spec.target_name)
    if spec.config_format == "jsonc":
        return get_jsonc_server(text, spec.target_name)
    if spec.config_format == "agy_json":
        return get_agy_json_server(text, spec.target_name)
    if spec.config_format == "claude_json":
        return get_claude_json_server(text, spec.target_name)
    raise MCPConfigError(f"Unsupported config format: {spec.config_format}")


_read_entry = read_entry


def evaluate_spec_status(spec: AgentSpec) -> str:
    """
    Evaluates synchronization status for a single AgentSpec.
    Returns one of: 'OK', 'MISSING', 'DRIFT', 'ERROR', 'SKIP'.
    """
    if not spec.enabled or not _agent_detected(spec):
        return "SKIP"
    if not spec.config_path.exists():
        return "MISSING"

    try:
        content = spec.config_path.read_text(encoding="utf-8")
        current = read_entry(spec, content)
        if current == spec.desired:
            return "OK"
        if current is None:
            return "MISSING"
        return "DRIFT"
    except (tomllib.TOMLDecodeError, UnicodeDecodeError, PermissionError, ValueError, OSError):
        return "ERROR"
    except Exception:
        return "ERROR"


def _update_entry(spec: AgentSpec, text: str) -> str:
    if spec.config_format == "toml":
        return update_toml_server(text, spec.target_name, spec.desired)
    if spec.config_format == "jsonc":
        return update_jsonc_server(text, spec.target_name, spec.desired)
    if spec.config_format == "agy_json":
        return update_agy_json_server(text, spec.target_name, spec.desired)
    if spec.config_format == "claude_json":
        return update_claude_json_server(text, spec.target_name, spec.desired)
    raise MCPConfigError(f"Unsupported config format: {spec.config_format}")


def _agent_detected(spec: AgentSpec) -> bool:
    return spec.config_path.parent.exists()


def _urls_in_text(text: str) -> list[str]:
    return [match.rstrip(").,;]") for match in URL_PATTERN.findall(text)]


def _has_sensitive_parameters(url: str) -> bool:
    parameters = {key.lower() for key in parse_qs(urlsplit(url).query)}
    return bool(parameters & SENSITIVE_URL_PARAMETERS)


def _is_authorization_url(url: str) -> bool:
    if _has_sensitive_parameters(url):
        return False
    parsed = urlsplit(url)
    location = f"{parsed.netloc}{parsed.path}".lower()
    parameters = {key.lower() for key in parse_qs(parsed.query)}
    return (
        "authorize" in location
        or "oauth" in location
        or {"client_id", "redirect_uri"} <= parameters
    )


def _redact_sensitive_urls(text: str) -> str:
    return URL_PATTERN.sub(
        lambda match: (
            "[REDACTED CALLBACK URL]"
            if _has_sensitive_parameters(match.group())
            else match.group()
        ),
        text,
    )


def _write_browser_helper(directory: Path) -> Path:
    helper = directory / "aikito-browser"
    helper.write_text(BROWSER_HELPER)
    helper.chmod(0o700)
    return helper


def _find_agent_spec(specs: list[AgentSpec], agent: str, server: str) -> AgentSpec:
    try:
        return next(
            spec for spec in specs if spec.agent == agent and spec.server == server
        )
    except StopIteration as exc:
        raise MCPConfigError(
            f"MCP server '{server}' is not configured for agent '{agent}'"
        ) from exc


def authenticate_mcp(
    *,
    aikito_dir: Path,
    home: Path,
    agent: str,
    server: str,
    output: Callable[[str], None] = print,
    open_browser: bool = True,
) -> bool:
    spec = _find_agent_spec(load_agent_specs(aikito_dir, home), agent, server)
    if not spec.enabled:
        raise MCPConfigError(
            f"{agent}/{server} authentication is disabled: {spec.reason}"
        )
    if not _agent_detected(spec) or not spec.config_path.exists():
        raise MCPConfigError(f"{agent} is not configured; run 'aikito sync mcp' first")
    current = _read_entry(spec, spec.config_path.read_text())
    if current != spec.desired:
        raise MCPConfigError(
            f"{agent}/{server} config is missing or has drifted; "
            "run 'aikito sync mcp' first"
        )
    if not spec.auth_command:
        raise MCPConfigError(f"{agent}/{server} has no authentication command")
    if shutil.which(spec.auth_command[0]) is None:
        raise MCPConfigError(f"Agent CLI not found: {spec.auth_command[0]}")

    output(f"[AUTH] {' '.join(spec.auth_command)}")
    with tempfile.TemporaryDirectory(prefix="aikito-mcp-auth-") as temporary_dir:
        temporary_path = Path(temporary_dir)
        url_file = temporary_path / "authorization-urls"
        browser_helper = _write_browser_helper(temporary_path)
        environment = os.environ.copy()
        environment.update(
            {
                "AIKITO_AUTH_URL_FILE": str(url_file),
                "AIKITO_OPEN_BROWSER": "1" if open_browser else "0",
                "BROWSER": str(browser_helper),
            }
        )
        process = subprocess.Popen(
            spec.auth_command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            env=environment,
        )
        if process.stdout is None:
            raise MCPConfigError("Authentication command output is unavailable")

        lines: queue.Queue[str | None] = queue.Queue()

        def read_output() -> None:
            for line in process.stdout:
                lines.put(line.rstrip())
            lines.put(None)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        reader_finished = False
        seen_urls: set[str] = set()

        while process.poll() is None or not reader_finished:
            try:
                line = lines.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line is None:
                reader_finished = True
            elif line:
                output(_redact_sensitive_urls(line))
                for url in _urls_in_text(line):
                    if _is_authorization_url(url) and url not in seen_urls:
                        seen_urls.add(url)
                        output(f"[AUTH URL] {url}")

            if url_file.exists():
                captured_urls = url_file.read_text()
                if captured_urls.endswith("\n"):
                    for url in captured_urls.splitlines():
                        if _is_authorization_url(url) and url not in seen_urls:
                            seen_urls.add(url)
                            output(f"[AUTH URL] {url}")

        reader.join(timeout=1)
        return_code = process.wait()
        process.stdout.close()

    if not seen_urls:
        output(
            "[ERROR] Authentication command did not expose an authorization URL. "
            "No credential values were logged."
        )
        return False
    if return_code != 0:
        output(f"[ERROR] Authentication command exited with status {return_code}")
        return False
    output(f"[SUCCESS] {agent}/{server} authentication completed")
    return True


def sync_mcp_configs(
    *,
    aikito_dir: Path,
    home: Path,
    dry_run: bool = False,
    force: bool = False,
    output: Callable[[str], None] = print,
) -> bool:
    specs = load_agent_specs(aikito_dir, home)
    state = _load_state(home)
    entries = state["entries"]
    success = True

    for spec in specs:
        if not spec.enabled:
            output(f"[SKIP] {spec.agent}/{spec.server}: {spec.reason}")
            continue
        if not _agent_detected(spec):
            output(f"[SKIP] {spec.agent} not detected: {spec.config_path.parent}")
            continue

        text = spec.config_path.read_text() if spec.config_path.exists() else ""
        current = _read_entry(spec, text)
        previous = entries.get(spec.state_key, {})
        managed_fingerprint = previous.get("fingerprint")
        current_fingerprint = _fingerprint(current) if current is not None else None
        desired_fingerprint = _fingerprint(spec.desired)

        if current == spec.desired:
            output(f"[OK] {spec.agent}/{spec.server}: already synchronized")
            if not dry_run:
                entries[spec.state_key] = {
                    "fingerprint": desired_fingerprint,
                    "config_path": str(spec.config_path),
                    "target_name": spec.target_name,
                }
            continue

        safe_to_update = (
            current is None
            or force
            or (
                managed_fingerprint is not None
                and current_fingerprint == managed_fingerprint
            )
        )
        if not safe_to_update:
            output(
                f"[CONFLICT] {spec.agent}/{spec.server}: existing config was not "
                "last written by aikito; review it or rerun with --force"
            )
            success = False
            continue

        action = "create" if current is None else "update"
        if dry_run:
            output(f"[DRY-RUN] {spec.agent}/{spec.server}: would {action} entry")
            continue

        # ~/.claude.json is mixed application state and can contain unmanaged
        # private MCP data, so never duplicate the whole file into backups.
        backup = (
            None
            if spec.contains_secret or spec.config_format == "claude_json"
            else _backup_config(home, spec)
        )
        updated = _update_entry(spec, text)
        _atomic_write(spec.config_path, updated)
        if spec.contains_secret:
            spec.config_path.chmod(0o600)
        entries[spec.state_key] = {
            "fingerprint": desired_fingerprint,
            "config_path": str(spec.config_path),
            "target_name": spec.target_name,
        }
        output(f"[SYNC] {spec.agent}/{spec.server}: {action}d {spec.config_path}")
        if backup:
            output(f"[BACKUP] {backup}")
        if spec.auth_command:
            output(
                f"[AUTH] {aikito_dir / 'bin/aikito'} auth mcp "
                f"{spec.agent} {spec.server}"
            )

    if not dry_run:
        _save_state(home, state)
    return success


def status_mcp_configs(
    *,
    aikito_dir: Path,
    home: Path,
    live: bool = False,
    output: Callable[[str], None] = print,
) -> bool:
    specs = load_agent_specs(aikito_dir, home)
    success = True
    live_commands = {}

    for spec in specs:
        if not spec.enabled:
            output(f"[SKIP] {spec.agent}/{spec.server}: {spec.reason}")
            continue
        if not _agent_detected(spec):
            output(f"[SKIP] {spec.agent} not detected: {spec.config_path.parent}")
            continue
        if not spec.config_path.exists():
            output(f"[MISSING] {spec.agent}/{spec.server}: {spec.config_path}")
            success = False
            continue

        current = _read_entry(spec, spec.config_path.read_text())
        if current == spec.desired:
            output(f"[OK] {spec.agent}/{spec.server}: config matches")
            if live and spec.live_command:
                live_commands[spec.agent] = spec.live_command
        elif current is None:
            output(f"[MISSING] {spec.agent}/{spec.server}: entry not found")
            success = False
        else:
            output(f"[DRIFT] {spec.agent}/{spec.server}: config differs")
            success = False

    for result in run_live_mcp_commands(live_commands):
        if result.status == "SKIP":
            output(f"[SKIP] {result.agent} CLI not found: {result.command[0]}")
            continue
        if result.status == "TIMEOUT":
            output(f"[TIMEOUT] {result.agent}: {' '.join(result.command)}")
            success = False
            continue
        output(f"[LIVE] {result.agent}: exit={result.returncode}")
        if result.output:
            output(result.output)
        success = result.status == "OK" and success
    return success
