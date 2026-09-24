"""Configuration adapters for agent-native MCP configs."""

import base64
import hashlib
import json
import os
from typing import Any

from ..model import AgentSpec, BasicTokenAuth, LEGACY_PLACEHOLDER_TOKEN, MCPConfigError
from ..redact import _environment_reference
from .cordis import (
    _format_dsh_cordis_entry,
    _parse_cordis_plugin_item,
    _parse_dsh_cordis_entries,
    _split_cordis_patch_items,
    get_dsh_cordis_server,
    remove_dsh_cordis_server,
    update_dsh_cordis_server,
)
from .jsonc import (
    _format_json_value,
    _line_indent,
    _load_document,
    _object_members,
    _parse_json_value,
    _parse_jsonc,
    _tokenize_jsonc,
    get_agy_json_server,
    get_claude_json_server,
    get_copilot_json_server,
    get_jsonc_server,
    get_mcp_json_server,
    parse_jsonc,
    remove_agy_json_server,
    remove_claude_json_server,
    remove_copilot_json_server,
    remove_jsonc_server,
    remove_mcp_json_server,
    update_agy_json_server,
    update_claude_json_server,
    update_copilot_json_server,
    update_jsonc_server,
    update_mcp_json_server,
)
from .toml import (
    _toml_value,
    get_toml_server,
    remove_toml_server,
    update_toml_server,
)

__all__ = [
    # jsonc
    "_format_json_value",
    "_line_indent",
    "_load_document",
    "_object_members",
    "_parse_json_value",
    "_parse_jsonc",
    "_tokenize_jsonc",
    "get_agy_json_server",
    "get_claude_json_server",
    "get_copilot_json_server",
    "get_jsonc_server",
    "get_mcp_json_server",
    "parse_jsonc",
    "remove_agy_json_server",
    "remove_claude_json_server",
    "remove_copilot_json_server",
    "remove_jsonc_server",
    "remove_mcp_json_server",
    "update_agy_json_server",
    "update_claude_json_server",
    "update_copilot_json_server",
    "update_jsonc_server",
    "update_mcp_json_server",
    # toml
    "_toml_value",
    "get_toml_server",
    "remove_toml_server",
    "update_toml_server",
    # cordis
    "_format_dsh_cordis_entry",
    "_parse_cordis_plugin_item",
    "_parse_dsh_cordis_entries",
    "_split_cordis_patch_items",
    "get_dsh_cordis_server",
    "remove_dsh_cordis_server",
    "update_dsh_cordis_server",
    # dispatch & helpers
    "_build_desired",
    "_entry_matches_desired",
    "_fingerprint",
    "_read_entry",
    "_remove_entry",
    "_update_entry",
    "read_all_entries",
    "read_entry",
]


def _fingerprint(value: dict[str, Any]) -> str:
    canonical = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode()).hexdigest()


def _build_desired(
    config_format: str,
    url: str,
    override: dict[str, Any],
    authentication: BasicTokenAuth | None,
    headers: dict[str, str] | None = None,
    *,
    agent: str = "",
    server_name: str = "",
) -> tuple[dict[str, Any], bool, str]:
    """Construct the format-bound MCP payload for a target config."""
    if config_format == "toml":
        desired: dict[str, Any] = {"url": url}
        if authentication:
            if agent == "grok":
                # Grok interpolates ${ENV} in headers; Codex uses env_http_headers.
                desired["headers"] = {
                    "Authorization": f"${{{authentication.authorization_env}}}"
                }
            else:
                desired["env_http_headers"] = {
                    "Authorization": authentication.authorization_env
                }
        elif headers:
            if agent == "grok":
                desired["headers"] = headers
            else:
                static_headers: dict[str, str] = {}
                env_headers: dict[str, str] = {}
                for k, v in headers.items():
                    env_ref = _environment_reference(v)
                    if env_ref:
                        env_headers[k] = env_ref
                    else:
                        static_headers[k] = v
                if static_headers:
                    desired["headers"] = static_headers
                if env_headers:
                    desired["env_http_headers"] = env_headers
        return desired, False, ""
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
        elif headers:
            jsonc_headers: dict[str, str] = {}
            for k, v in headers.items():
                env_ref = _environment_reference(v)
                if env_ref:
                    jsonc_headers[k] = f"{{env:{env_ref}}}"
                else:
                    jsonc_headers[k] = v
            desired["headers"] = jsonc_headers
        return desired, False, ""
    if config_format == "agy_json":
        desired = {"serverUrl": url}
        if authentication:
            # agy 1.1.8 accepts headers but does not document environment
            # interpolation, so only its generated runtime config contains this.
            if os.environ.get(authentication.token_env):
                desired["headers"] = {
                    "Authorization": authentication.authorization_header()
                }
            else:
                return desired, True, authentication.token_env
            return desired, True, ""
        elif headers:
            resolved_headers: dict[str, str] = {}
            missing_env = ""
            has_secret = False
            for k, v in headers.items():
                env_ref = _environment_reference(v)
                if env_ref:
                    has_secret = True
                    env_val = os.environ.get(env_ref)
                    if env_val:
                        resolved_headers[k] = env_val
                    else:
                        missing_env = env_ref
                else:
                    resolved_headers[k] = v
            if missing_env:
                return desired, True, missing_env
            desired["headers"] = resolved_headers
            return desired, has_secret, ""
        return desired, False, ""
    if config_format == "claude_json":
        desired = {"type": "http", "url": url}
        if authentication:
            desired["headers"] = {
                "Authorization": f"${{{authentication.authorization_env}}}"
            }
        elif headers:
            claude_headers: dict[str, str] = {}
            for k, v in headers.items():
                env_ref = _environment_reference(v)
                if env_ref:
                    claude_headers[k] = f"${{{env_ref}}}"
                else:
                    claude_headers[k] = v
            desired["headers"] = claude_headers
        return desired, False, ""
    if config_format == "copilot_json":
        desired = {
            "type": "http",
            "url": url,
            "tools": ["*"],
        }
        if headers:
            copilot_headers: dict[str, str] = {}
            for k, v in headers.items():
                env_ref = _environment_reference(v)
                if env_ref:
                    copilot_headers[k] = f"${{{env_ref}}}"
                else:
                    copilot_headers[k] = v
            desired["headers"] = copilot_headers
        elif authentication:
            desired["headers"] = {
                "Authorization": f"${{{authentication.authorization_env}}}"
            }
        else:
            desired["headers"] = {}
        return desired, False, ""
    if config_format == "dsh_cordis":
        transport = override.get("transport", "streamable-http")
        desired = {
            "serverName": str(override.get("name", "")),
            "transport": transport,
            "url": url,
        }
        if headers:
            dsh_headers: dict[str, str] = {}
            for k, v in headers.items():
                env_ref = _environment_reference(v)
                if env_ref:
                    dsh_headers[k] = f"!!js process.env.{env_ref}"
                else:
                    dsh_headers[k] = v
            desired["headers"] = dsh_headers
        elif authentication:
            desired["headers"] = {
                "Authorization": f"!!js process.env.{authentication.authorization_env}"
            }
        if "timeout" in override:
            desired["toolCallTimeoutMs"] = override["timeout"]
        return desired, False, ""
    # Unsupported formats never get written; payload is informational only.
    return {}, False, ""


def read_entry(spec: AgentSpec, text: str) -> dict[str, Any] | None:
    if spec.config_format == "toml":
        return get_toml_server(text, spec.target_name)
    if spec.config_format == "jsonc":
        return get_jsonc_server(text, spec.target_name)
    if spec.config_format == "agy_json":
        return get_agy_json_server(text, spec.target_name)
    if spec.config_format == "claude_json":
        return get_claude_json_server(text, spec.target_name)
    if spec.config_format == "copilot_json":
        return get_copilot_json_server(text, spec.target_name)
    if spec.config_format == "dsh_cordis":
        return get_dsh_cordis_server(text, spec.target_name)
    raise MCPConfigError(f"Unsupported config format: {spec.config_format}")


def read_all_entries(config_format: str, text: str) -> dict[str, dict[str, Any]]:
    """Read only the MCP server map from an Agent-native configuration."""
    document = _load_document(config_format, text)
    if config_format == "toml":
        servers = document.get("mcp_servers", {})
    elif config_format == "jsonc":
        servers = document.get("mcp", {})
    else:
        servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError("Agent MCP server collection must be an object")
    return {name: entry for name, entry in servers.items() if isinstance(entry, dict)}


_read_entry = read_entry


def _entry_matches_desired(spec: AgentSpec, current: dict[str, Any] | None) -> bool:
    if current is None:
        return False
    if not spec.missing_credential_env:
        return current == spec.desired

    headers = current.get("headers")
    authorization = headers.get("Authorization") if isinstance(headers, dict) else None
    if not isinstance(authorization, str) or not authorization.startswith("Basic "):
        return False

    try:
        decoded_authorization = base64.b64decode(
            authorization.removeprefix("Basic "), validate=True
        ).decode()
    except (ValueError, UnicodeDecodeError):
        return False
    if decoded_authorization.endswith(f":{LEGACY_PLACEHOLDER_TOKEN}"):
        return False

    without_headers = dict(current)
    without_headers.pop("headers", None)
    return without_headers == spec.desired


def _update_entry(spec: AgentSpec, text: str) -> str:
    if spec.config_format == "toml":
        return update_toml_server(text, spec.target_name, spec.desired)
    if spec.config_format == "jsonc":
        return update_jsonc_server(text, spec.target_name, spec.desired)
    if spec.config_format == "agy_json":
        return update_agy_json_server(text, spec.target_name, spec.desired)
    if spec.config_format == "claude_json":
        return update_claude_json_server(text, spec.target_name, spec.desired)
    if spec.config_format == "copilot_json":
        return update_copilot_json_server(text, spec.target_name, spec.desired)
    if spec.config_format == "dsh_cordis":
        return update_dsh_cordis_server(text, spec.target_name, spec.desired)
    raise MCPConfigError(f"Unsupported config format: {spec.config_format}")


def _remove_entry(spec: AgentSpec, text: str) -> str:
    if spec.config_format == "toml":
        return remove_toml_server(text, spec.target_name)
    if spec.config_format == "jsonc":
        return remove_jsonc_server(text, spec.target_name)
    if spec.config_format == "agy_json":
        return remove_agy_json_server(text, spec.target_name)
    if spec.config_format == "claude_json":
        return remove_claude_json_server(text, spec.target_name)
    if spec.config_format == "copilot_json":
        return remove_copilot_json_server(text, spec.target_name)
    if spec.config_format == "dsh_cordis":
        return remove_dsh_cordis_server(text, spec.target_name)
    raise MCPConfigError(f"Unsupported config format: {spec.config_format}")
