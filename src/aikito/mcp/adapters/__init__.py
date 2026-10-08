"""Configuration adapters for agent-native MCP configs."""

from dataclasses import dataclass
from collections.abc import Callable

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


def _build_toml(url, override, authentication, headers, server_name):
    desired = {"url": url}
    if authentication:
        desired["env_http_headers"] = {
            "Authorization": authentication.authorization_env
        }
    elif headers:
        static_headers = {}
        env_headers = {}
        for key, value in headers.items():
            env_ref = _environment_reference(value)
            if env_ref:
                env_headers[key] = env_ref
            else:
                static_headers[key] = value
        if static_headers:
            desired["headers"] = static_headers
        if env_headers:
            desired["env_http_headers"] = env_headers
    return desired, False, ""


def _build_grok_toml(url, override, authentication, headers, server_name):
    desired = {"url": url}
    if authentication:
        desired["headers"] = {
            "Authorization": f"${{{authentication.authorization_env}}}"
        }
    elif headers:
        desired["headers"] = headers
    return desired, False, ""


def _build_jsonc(url, override, authentication, headers, server_name):
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


def _build_agy_json(url, override, authentication, headers, server_name):
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


def _build_claude_json(url, override, authentication, headers, server_name):
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


def _build_copilot_json(url, override, authentication, headers, server_name):
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


def _build_dsh_cordis(url, override, authentication, headers, server_name):
    transport = override.get("transport", "streamable-http")
    desired = {
        "serverName": str(override.get("name", server_name)),
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


def _import_verbatim(entry: dict[str, Any]) -> dict[str, Any] | None:
    return dict(entry)


def _import_copilot_json(entry: dict[str, Any]) -> dict[str, Any] | None:
    # Copilot local servers have no canonical remote equivalent.
    if entry.get("type", "http") != "http" or not isinstance(entry.get("url"), str):
        return None
    return {**entry, "transport": "remote"}


@dataclass(frozen=True)
class MCPAdapter:
    build_desired: Callable
    read_entry: Callable
    update_entry: Callable
    remove_entry: Callable
    document_format: str
    server_collection: str
    syntax_name: str
    materializes_secrets: bool = False
    # Converts one native entry for adoption; None marks the adapter as not
    # adoptable, and a None result skips an unsupported entry.
    import_entry: Callable[[dict[str, Any]], dict[str, Any] | None] | None = None

    def read_all_entries(self, text: str) -> dict[str, dict[str, Any]]:
        document = _load_document(self.document_format, text)
        servers = document.get(self.server_collection, {})
        if not isinstance(servers, dict):
            raise MCPConfigError("Agent MCP server collection must be an object")
        return {
            name: entry for name, entry in servers.items() if isinstance(entry, dict)
        }


MCP_ADAPTERS: dict[str, MCPAdapter] = {
    "toml": MCPAdapter(
        _build_toml,
        get_toml_server,
        update_toml_server,
        remove_toml_server,
        "toml",
        "mcp_servers",
        "TOML",
        import_entry=_import_verbatim,
    ),
    "grok_toml": MCPAdapter(
        _build_grok_toml,
        get_toml_server,
        update_toml_server,
        remove_toml_server,
        "toml",
        "mcp_servers",
        "TOML",
    ),
    "jsonc": MCPAdapter(
        _build_jsonc,
        get_jsonc_server,
        update_jsonc_server,
        remove_jsonc_server,
        "jsonc",
        "mcp",
        "JSONC",
    ),
    "agy_json": MCPAdapter(
        _build_agy_json,
        get_agy_json_server,
        update_agy_json_server,
        remove_agy_json_server,
        "agy_json",
        "mcpServers",
        "JSON",
        True,
    ),
    "claude_json": MCPAdapter(
        _build_claude_json,
        get_claude_json_server,
        update_claude_json_server,
        remove_claude_json_server,
        "claude_json",
        "mcpServers",
        "JSON",
        True,
        import_entry=_import_verbatim,
    ),
    "copilot_json": MCPAdapter(
        _build_copilot_json,
        get_copilot_json_server,
        update_copilot_json_server,
        remove_copilot_json_server,
        "copilot_json",
        "mcpServers",
        "JSON",
        import_entry=_import_copilot_json,
    ),
    "dsh_cordis": MCPAdapter(
        _build_dsh_cordis,
        get_dsh_cordis_server,
        update_dsh_cordis_server,
        remove_dsh_cordis_server,
        "dsh_cordis",
        "mcpServers",
        "Cordis patch YAML",
    ),
}


def get_mcp_adapter(config_format: str) -> MCPAdapter:
    try:
        return MCP_ADAPTERS[config_format]
    except KeyError as exc:
        raise MCPConfigError(f"Unsupported config format: {config_format}") from exc


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
    return get_mcp_adapter(config_format).build_desired(
        url, override, authentication, headers, server_name
    )


def read_entry(spec: AgentSpec, text: str) -> dict[str, Any] | None:
    return get_mcp_adapter(spec.adapter).read_entry(text, spec.target_name)


def read_all_entries(adapter: str, text: str) -> dict[str, dict[str, Any]]:
    return get_mcp_adapter(adapter).read_all_entries(text)


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
    return get_mcp_adapter(spec.adapter).update_entry(
        text, spec.target_name, spec.desired
    )


def _remove_entry(spec: AgentSpec, text: str) -> str:
    return get_mcp_adapter(spec.adapter).remove_entry(text, spec.target_name)
