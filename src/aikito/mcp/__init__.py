"""Synchronize canonical Aikito MCP definitions into supported agent configs."""

import json
import os
import queue
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
from collections import defaultdict
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from ..agents import (
    AgentDefinition,
    AgentRegistryError,
    is_agent_installed,
    load_agent_definitions,
)
from ..compat import resolve_executable, secure_file_permissions
from ..config_runtime import (
    ConfigCollisionError,
    StaleConfigPlanError,
    capture_file_snapshot,
    resolve_physical_identity,
)
from ..diagnostics import Finding
from ..plan_observation import (
    OperationEffect,
    PlanOperationView,
    UnknownPlanActionError,
)
from .model import (
    BACKUP_DIR,
    BROWSER_HELPER,
    DEFAULT_AGENTS_CONFIG,
    DEFAULT_MCPS_DIR,
    LEGACY_PLACEHOLDER_TOKEN,
    STATE_FILE,
    STATE_VERSION,
    AgentSpec,
    BasicTokenAuth,
    LiveMCPResult,
    MCPConfigError,
    MCPConfigTarget,
    MCPDesiredEntry,
    MCPExecutionResult,
    MCPFilePlan,
    MCPObservedEntry,
    MCPOperation,
    MCPPlan,
    MCPToolProbeResult,
    Token,
    _MCPProbeError,
    _RejectRedirects,
    _state_file_hash,
)
from .redact import (
    SENSITIVE_URL_PARAMETERS,
    URL_PATTERN,
    _environment_reference,
    _headers_contain_credentials,
    _is_authorization_url,
    _is_loopback_url,
    _redact_probe_error,
    _redact_sensitive_urls,
    _urls_in_text,
    environment_reference,
    is_credential_header,
    is_sensitive_url_parameter,
    redact_mcp_entry,
)
from .adapters import (
    _build_desired,
    _entry_matches_desired,
    _fingerprint,
    _load_document,
    _parse_jsonc,
    _read_entry,
    _remove_entry,
    _update_entry,
    get_agy_json_server,
    get_claude_json_server,
    get_copilot_json_server,
    get_dsh_cordis_server,
    get_jsonc_server,
    get_toml_server,
    parse_jsonc,
    read_all_entries,
    read_entry,
    remove_claude_json_server,
    remove_dsh_cordis_server,
    remove_jsonc_server,
    remove_toml_server,
    update_agy_json_server,
    update_claude_json_server,
    update_copilot_json_server,
    update_dsh_cordis_server,
    update_jsonc_server,
    update_toml_server,
)

__all__ = [
    # Models & Exceptions
    "AgentSpec",
    "BasicTokenAuth",
    "LiveMCPResult",
    "MCPConfigError",
    "MCPConfigTarget",
    "MCPDesiredEntry",
    "MCPExecutionResult",
    "MCPFilePlan",
    "MCPObservedEntry",
    "MCPOperation",
    "MCPPlan",
    "MCPToolProbeResult",
    "Token",
    # Constants
    "BACKUP_DIR",
    "BROWSER_HELPER",
    "DEFAULT_AGENTS_CONFIG",
    "DEFAULT_MCPS_DIR",
    "LEGACY_PLACEHOLDER_TOKEN",
    "SENSITIVE_URL_PARAMETERS",
    "STATE_FILE",
    "STATE_VERSION",
    "URL_PATTERN",
    # Loader
    "load_agent_specs",
    # Adapters (formats)
    "get_agy_json_server",
    "get_claude_json_server",
    "get_copilot_json_server",
    "get_dsh_cordis_server",
    "get_jsonc_server",
    "get_toml_server",
    "parse_jsonc",
    "read_all_entries",
    "read_entry",
    "remove_claude_json_server",
    "remove_dsh_cordis_server",
    "remove_jsonc_server",
    "remove_toml_server",
    "update_agy_json_server",
    "update_claude_json_server",
    "update_copilot_json_server",
    "update_dsh_cordis_server",
    "update_jsonc_server",
    "update_toml_server",
    # Redaction
    "environment_reference",
    "is_credential_header",
    "is_sensitive_url_parameter",
    "redact_mcp_entry",
    # Planner
    "build_mcp_plan",
    "evaluate_spec_status",
    "mcp_operation_effect",
    "mcp_operation_finding",
    "observe_mcp_operation",
    # Executor & State
    "execute_mcp_plan",
    "sync_mcp_configs",
    "sync_remove_mcp_from_agents",
    # Auth & Probe
    "authenticate_mcp",
    "describe_mcp_auth",
    "probe_mcp_tools",
    "probe_mcp_tools_for_specs",
    "run_live_mcp_commands",
    # Backward compatibility / test access
    "_LiveLoadingIndicator",
    "_MCPProbeError",
    "_RejectRedirects",
    "_agent_detected",
    "_atomic_write",
    "_list_remote_mcp_tools",
    "_load_basic_token_auth",
    "_load_document",
    "_load_state",
    "_parse_jsonc",
    "_post_mcp_message",
    "_read_entry",
    "_redact_probe_error",
    "_response_message",
]


def _load_agent_definitions(aikito_dir: Path, home: Path) -> dict[str, AgentDefinition]:
    """MCP boundary: expose Agent registry failures as MCPConfigError."""
    try:
        return load_agent_definitions(aikito_dir, home)
    except AgentRegistryError as exc:
        raise MCPConfigError(str(exc)) from exc


def _target_name(name_style: str, server_name: str) -> str:
    if name_style == "underscore":
        return server_name.replace("-", "_")
    return server_name


def _render_command(template: tuple[str, ...], target: str) -> tuple[str, ...]:
    return tuple(part.replace("{target}", target) for part in template)


def _load_state(home: Path) -> dict[str, Any]:
    path = home / STATE_FILE
    if not path.exists():
        return {"version": STATE_VERSION, "entries": {}}
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        raise MCPConfigError(f"Cannot read MCP state file {path}: {exc}") from exc
    if state.get("version") != STATE_VERSION or not isinstance(
        state.get("entries"), dict
    ):
        raise MCPConfigError(f"Unsupported MCP state file: {path}")
    return state


def _atomic_write(path: Path, content: str, secure_permissions: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(path.stat().st_mode) if path.exists() else None
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, delete=False, encoding="utf-8"
    ) as handle:
        handle.write(content)
        temp_path = Path(handle.name)
    if secure_permissions:
        if not secure_file_permissions(temp_path):
            print(
                f"[WARN] Could not secure file permissions on credential file: {path}",
                file=sys.stderr,
            )
    elif mode is not None:
        try:
            temp_path.chmod(mode)
        except OSError:
            pass
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
    for auth_field in ("account_email", "token_env", "authorization_env"):
        value = authentication.get(auth_field)
        if not isinstance(value, str) or not value:
            raise MCPConfigError(
                f"Server '{server_name}' authentication requires '{auth_field}'"
            )
        fields[auth_field] = value
    return BasicTokenAuth(**fields)


def load_agent_specs(aikito_dir: Path, home: Path) -> list[AgentSpec]:
    mcps_dir = aikito_dir / DEFAULT_MCPS_DIR
    if not mcps_dir.exists():
        raise MCPConfigError(f"MCP config directory not found: {mcps_dir}")
    if not mcps_dir.is_dir():
        raise MCPConfigError(f"MCP config path is not a directory: {mcps_dir}")

    servers: dict[str, dict[str, Any]] = {}
    for config_path in sorted(mcps_dir.glob("*.toml")):
        server_name = config_path.stem
        try:
            document = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise MCPConfigError(f"Invalid MCP config {config_path}: {exc}") from exc

        if "servers" in document and isinstance(document["servers"], dict):
            for s_name, s_val in document["servers"].items():
                if isinstance(s_val, dict):
                    servers[s_name] = s_val
                else:
                    raise MCPConfigError(
                        f"Server '{s_name}' in {config_path} must be a table"
                    )
        else:
            servers[server_name] = document

    registry = _load_agent_definitions(aikito_dir, home)

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
        headers = server.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise MCPConfigError(
                f"Server '{server_name}' headers must be a string-to-string table"
            )

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

            capability = definition.mcp
            if capability is None or not capability.is_supported:
                specs.append(
                    AgentSpec(
                        agent=agent,
                        server=server_name,
                        config_path=capability.config_path if capability else Path(),
                        config_format="unsupported",
                        target_name=server_name,
                        desired={},
                        enabled=False,
                        reason=(
                            reason
                            or (capability.reason if capability else "")
                            or f"MCP synchronization is not supported for agent '{agent}'"
                        ),
                        home=home,
                    )
                )
                continue

            name_style = capability.name_style
            default_target = _target_name(name_style, server_name)
            target_name = str(override.get("name", default_target))
            desired, contains_secret, missing_credential_env = _build_desired(
                capability.config_format,
                url,
                override,
                authentication,
                headers,
                agent=agent,
            )
            if capability.config_format == "dsh_cordis" and not desired.get(
                "serverName"
            ):
                desired = dict(desired)
                desired["serverName"] = target_name
            specs.append(
                AgentSpec(
                    agent=agent,
                    server=server_name,
                    config_path=capability.config_path,
                    config_format=capability.config_format,
                    target_name=target_name,
                    desired=desired,
                    enabled=bool(enabled),
                    reason=reason,
                    live_command=_render_command(capability.live_command, target_name),
                    auth_command=(
                        ()
                        if authentication
                        else _render_command(capability.auth_command, target_name)
                    ),
                    contains_secret=contains_secret,
                    missing_credential_env=missing_credential_env,
                    home=home,
                )
            )
    return specs


class _LiveLoadingIndicator:
    """Animated loading indicator on stderr for live operations."""

    def __init__(
        self,
        *,
        stream: Any = None,
        animate: bool | None = None,
        use_color: bool | None = None,
        interval: float = 0.25,
    ) -> None:
        self._stream = stream if stream is not None else sys.stderr
        self._animate = (
            animate
            if animate is not None
            else getattr(self._stream, "isatty", lambda: False)()
        )
        self._use_color = (
            use_color if use_color is not None else not bool(os.environ.get("NO_COLOR"))
        )
        self._interval = interval
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._wake_event = threading.Event()
        self._active_counts: dict[str, int] = {}
        self._thread: threading.Thread | None = None

    def add(self, label: str) -> None:
        with self._lock:
            self._active_counts[label] = self._active_counts.get(label, 0) + 1
        self._wake_event.set()

    def remove(self, label: str) -> None:
        with self._lock:
            if label in self._active_counts:
                self._active_counts[label] -= 1
                if self._active_counts[label] <= 0:
                    del self._active_counts[label]
        self._wake_event.set()

    def __enter__(self) -> "_LiveLoadingIndicator":
        if self._animate:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        if self._animate and self._thread is not None:
            self._stop_event.set()
            self._wake_event.set()
            self._thread.join(timeout=1.0)
            try:
                self._stream.write("\r\033[K")
                self._stream.flush()
            except Exception:
                pass

    def _loop(self) -> None:
        frame = 0
        while not self._stop_event.is_set():
            self._wake_event.clear()
            with self._lock:
                labels = sorted(self._active_counts.keys())
            if labels:
                label_str = ", ".join(labels)
                dots = "." * ((frame % 3) + 1)
                text = f"{label_str} loading {dots}"
                styled = f"\033[2m{text}\033[0m" if self._use_color else text
                try:
                    self._stream.write(f"\r{styled}\033[K")
                    self._stream.flush()
                except Exception:
                    break
                frame += 1
                if self._stop_event.wait(timeout=self._interval):
                    break
            else:
                if self._stop_event.is_set():
                    break
                self._wake_event.wait(timeout=self._interval)


def run_live_mcp_commands(
    commands: dict[str, tuple[str, ...]],
    timeout: int = 45,
    *,
    animate: bool | None = None,
    stream: Any = None,
    use_color: bool | None = None,
) -> list[LiveMCPResult]:
    """Run one live MCP status command per agent and normalize its outcome."""
    results = []
    with _LiveLoadingIndicator(
        stream=stream, animate=animate, use_color=use_color
    ) as indicator:
        for agent, command in commands.items():
            indicator.add(agent)
            try:
                resolved_cmd = resolve_executable(command)
                if shutil.which(resolved_cmd[0]) is None:
                    results.append(LiveMCPResult(agent, command, "SKIP", None))
                    continue
                try:
                    result = subprocess.run(
                        resolved_cmd,
                        capture_output=True,
                        text=True,
                        encoding="utf-8",
                        errors="replace",
                        timeout=timeout,
                        check=False,
                    )

                except subprocess.TimeoutExpired:
                    results.append(LiveMCPResult(agent, command, "TIMEOUT", None))
                    continue

                output = "\n".join(
                    part.strip()
                    for part in (result.stdout, result.stderr)
                    if part.strip()
                )
                status = "OK" if result.returncode == 0 else "ERROR"
                results.append(
                    LiveMCPResult(agent, command, status, result.returncode, output)
                )
            finally:
                indicator.remove(agent)
    return results


_MCP_PROTOCOL_VERSION = "2025-11-25"
_MCP_USER_AGENT = "aikito"
_MAX_MCP_RESPONSE_BYTES = 8 * 1024 * 1024


def _authorization_label(value: str | None, source: str) -> str:
    scheme = value.split(None, 1)[0].title() if value and value.strip() else ""
    if scheme not in {"Basic", "Bearer"}:
        return "Environment header" if source == "env" else "Unknown"
    suffix = "env header" if source == "env" else "inline header"
    return f"{scheme} · {suffix}"


def describe_mcp_auth(entry: dict[str, Any]) -> str:
    """Describe configured authentication without exposing credential values."""
    env_headers = entry.get("env_http_headers")
    if isinstance(env_headers, dict):
        for name, env_name in env_headers.items():
            if str(name).lower() != "authorization" or not isinstance(env_name, str):
                continue
            return _authorization_label(os.environ.get(env_name), "env")

    for container_name in ("headers", "http_headers"):
        headers = entry.get(container_name)
        if not isinstance(headers, dict):
            continue
        for name, raw_value in headers.items():
            if str(name).lower() != "authorization" or not isinstance(raw_value, str):
                continue
            env_name = _environment_reference(raw_value)
            value = os.environ.get(env_name) if env_name else raw_value
            return _authorization_label(value, "env" if env_name else "inline")

    bearer_env = entry.get("bearer_token_env_var")
    if isinstance(bearer_env, str) and bearer_env:
        return "Bearer · env token"
    if entry.get("auth") == "oauth" or entry.get("oauth") is True:
        return "OAuth"
    return "None"


def _resolve_mcp_headers(entry: dict[str, Any]) -> dict[str, str]:
    resolved: dict[str, str] = {}

    env_headers = entry.get("env_http_headers")
    if isinstance(env_headers, dict):
        for name, env_name in env_headers.items():
            if not isinstance(name, str) or not isinstance(env_name, str):
                continue
            value = os.environ.get(env_name)
            if value is None:
                raise _MCPProbeError(
                    f"credential environment variable '{env_name}' is unavailable"
                )
            resolved[name] = value

    for container_name in ("headers", "http_headers"):
        headers = entry.get(container_name)
        if not isinstance(headers, dict):
            continue
        for name, raw_value in headers.items():
            if not isinstance(name, str) or not isinstance(raw_value, str):
                continue
            env_name = _environment_reference(raw_value)
            if env_name:
                value = os.environ.get(env_name)
                if value is None:
                    raise _MCPProbeError(
                        f"credential environment variable '{env_name}' is unavailable"
                    )
                resolved[name] = value
            else:
                resolved[name] = raw_value

    bearer_env = entry.get("bearer_token_env_var")
    if isinstance(bearer_env, str) and bearer_env:
        token = os.environ.get(bearer_env)
        if token is None:
            raise _MCPProbeError(
                f"credential environment variable '{bearer_env}' is unavailable"
            )
        resolved["Authorization"] = f"Bearer {token}"
    return resolved


def _response_message(body: bytes) -> str:
    text = body.decode("utf-8", errors="replace").strip()
    if not text:
        return ""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        message = text
    else:
        if not isinstance(payload, dict):
            return ""
        error = payload.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            message = error["message"]
        elif isinstance(payload.get("detail"), str):
            message = payload["detail"]
        elif isinstance(payload.get("message"), str):
            message = payload["message"]
        elif isinstance(payload.get("title"), str):
            message = payload["title"]
        else:
            return ""

    return message


def _decode_mcp_response(body: bytes, request_id: int) -> dict[str, Any]:
    text = body.decode("utf-8", errors="strict").strip()
    candidates: list[str] = []
    if text.startswith("data:") or "\ndata:" in text:
        for event in re.split(r"\r?\n\r?\n", text):
            data = "\n".join(
                line[5:].lstrip()
                for line in event.splitlines()
                if line.startswith("data:")
            )
            if data:
                candidates.append(data)
    elif text:
        candidates.append(text)

    for candidate in candidates:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and payload.get("id") == request_id:
            error = payload.get("error")
            if isinstance(error, dict):
                message = str(error.get("message", "MCP request failed"))
                raise _MCPProbeError(message)
            result = payload.get("result")
            if isinstance(result, dict):
                return result
            raise _MCPProbeError("MCP response has no result object")
    raise _MCPProbeError("MCP response did not contain the requested JSON-RPC result")


def _post_mcp_message(
    url: str,
    payload: dict[str, Any],
    headers: dict[str, str],
    *,
    timeout: int,
    session_id: str = "",
    protocol_version: str = "",
    retries: int = 2,
) -> tuple[bytes, str]:
    method = str(payload.get("method", ""))
    request_headers = dict(headers)
    request_headers.setdefault("User-Agent", _MCP_USER_AGENT)
    request_headers.update(
        {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Mcp-Method": method,
        }
    )
    if session_id:
        request_headers["Mcp-Session-Id"] = session_id
    if protocol_version:
        request_headers["MCP-Protocol-Version"] = protocol_version

    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    for attempt in range(retries + 1):
        request = Request(
            url,
            data=data,
            headers=request_headers,
            method="POST",
        )
        try:
            with build_opener(_RejectRedirects()).open(
                request, timeout=timeout
            ) as response:
                body = response.read(_MAX_MCP_RESPONSE_BYTES + 1)
                if len(body) > _MAX_MCP_RESPONSE_BYTES:
                    raise _MCPProbeError("MCP response exceeded the 8 MiB safety limit")
                return body, response.headers.get("Mcp-Session-Id", "")
        except HTTPError as exc:
            body = exc.read(4096)
            detail = _response_message(body)
            suffix = f": {detail}" if detail else ""
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(0.3 * (2**attempt))
                continue
            raise _MCPProbeError(f"HTTP {exc.code}{suffix}") from exc
        except (URLError, TimeoutError, OSError) as exc:
            if attempt < retries:
                time.sleep(0.3 * (2**attempt))
                continue
            if isinstance(exc, TimeoutError):
                raise _MCPProbeError("connection timed out") from exc
            if isinstance(exc, URLError):
                raise _MCPProbeError(f"connection failed: {exc.reason}") from exc
            raise _MCPProbeError(f"connection failed: {exc}") from exc
    raise _MCPProbeError("MCP request failed after retries")


def _list_remote_mcp_tools(
    url: str, headers: dict[str, str], timeout: int
) -> tuple[str, ...]:
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": _MCP_PROTOCOL_VERSION,
            "capabilities": {},
            "clientInfo": {"name": "aikito", "version": "1"},
        },
    }
    body, session_id = _post_mcp_message(url, initialize, headers, timeout=timeout)
    initialized = _decode_mcp_response(body, 1)
    protocol_version = initialized.get("protocolVersion")
    if not isinstance(protocol_version, str) or not protocol_version:
        raise _MCPProbeError("MCP initialize response has no protocol version")

    _post_mcp_message(
        url,
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        headers,
        timeout=timeout,
        session_id=session_id,
        protocol_version=protocol_version,
    )

    names: list[str] = []
    cursor: str | None = None
    for request_id in range(2, 102):
        params = {"cursor": cursor} if cursor else {}
        body, _ = _post_mcp_message(
            url,
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": "tools/list",
                "params": params,
            },
            headers,
            timeout=timeout,
            session_id=session_id,
            protocol_version=protocol_version,
        )
        result = _decode_mcp_response(body, request_id)
        tools = result.get("tools")
        if not isinstance(tools, list):
            raise _MCPProbeError("MCP tools/list response has no tools array")
        for tool in tools:
            if isinstance(tool, dict) and isinstance(tool.get("name"), str):
                names.append(tool["name"])
        cursor = result.get("nextCursor")
        if not isinstance(cursor, str) or not cursor:
            return tuple(names)
    raise _MCPProbeError("MCP tools/list pagination exceeded 100 pages")


def probe_mcp_tools(spec: AgentSpec, timeout: int = 15) -> MCPToolProbeResult:
    """Discover tools through one Agent-native remote MCP configuration."""
    if not spec.config_path.is_file():
        return MCPToolProbeResult(
            spec.agent, "ERROR", "Unknown", error="config missing"
        )
    auth_method = "Unknown"
    headers: dict[str, str] = {}
    try:
        entry = read_entry(spec, spec.config_path.read_text(encoding="utf-8"))
        if entry is None:
            return MCPToolProbeResult(
                spec.agent, "ERROR", "Unknown", error="managed entry missing"
            )
        auth_method = describe_mcp_auth(entry)
        if auth_method == "OAuth":
            return MCPToolProbeResult(
                spec.agent,
                "SKIP",
                auth_method,
                error="OAuth credentials are managed by the Agent runtime",
            )
        url = entry.get("url") or entry.get("serverUrl")
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            return MCPToolProbeResult(
                spec.agent,
                "SKIP",
                auth_method,
                error="only remote HTTP MCP servers are supported",
            )
        headers = _resolve_mcp_headers(entry)
        parsed_url = urlsplit(url)
        has_url_credentials = bool(parsed_url.username or parsed_url.password)
        if (
            parsed_url.scheme == "http"
            and not _is_loopback_url(url)
            and (_headers_contain_credentials(headers) or has_url_credentials)
        ):
            return MCPToolProbeResult(
                spec.agent,
                "SKIP",
                auth_method,
                error="refusing to send MCP credentials over non-loopback HTTP",
            )
        tool_names = _list_remote_mcp_tools(url, headers, timeout)
        return MCPToolProbeResult(spec.agent, "OK", auth_method, tool_names)
    except (MCPConfigError, OSError, UnicodeError, _MCPProbeError) as exc:
        return MCPToolProbeResult(
            spec.agent,
            "ERROR",
            auth_method,
            error=_redact_probe_error(str(exc), headers),
        )


def probe_mcp_tools_for_specs(
    specs: list[AgentSpec],
    timeout: int = 15,
    *,
    animate: bool | None = None,
    stream: Any = None,
    use_color: bool | None = None,
) -> list[MCPToolProbeResult]:
    """Run independent read-only probes concurrently while preserving Agent order."""
    if not specs:
        return []
    with _LiveLoadingIndicator(
        stream=stream, animate=animate, use_color=use_color
    ) as indicator:
        for spec in specs:
            indicator.add(spec.agent)

        def _probe_worker(spec: AgentSpec) -> MCPToolProbeResult:
            try:
                return probe_mcp_tools(spec, timeout)
            finally:
                indicator.remove(spec.agent)

        with ThreadPoolExecutor(max_workers=min(8, len(specs))) as executor:
            return list(executor.map(_probe_worker, specs))


def _agent_detected(spec: AgentSpec) -> bool:
    if spec.home is not None:
        installed = is_agent_installed(spec.agent, spec.home)
        if installed is not None:
            return installed
    return spec.config_path.parent.exists()


def _write_browser_helper(directory: Path) -> Path:
    if sys.platform == "win32":
        py_helper = directory / "aikito-browser.py"
        py_helper.write_text(BROWSER_HELPER, encoding="utf-8")
        cmd_helper = directory / "aikito-browser.cmd"
        cmd_helper.write_text(
            f'@echo off\r\n"{sys.executable}" "{py_helper}" %*\r\n', encoding="utf-8"
        )
        return cmd_helper
    helper = directory / "aikito-browser"
    helper.write_text(BROWSER_HELPER, encoding="utf-8")
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
    current = _read_entry(spec, spec.config_path.read_text(encoding="utf-8"))
    if not _entry_matches_desired(spec, current):
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
            resolve_executable(spec.auth_command),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
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
                captured_urls = url_file.read_text(encoding="utf-8")
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


def mcp_operation_effect(op: MCPOperation) -> OperationEffect:
    """Map MCP operation action to canonical OperationEffect."""
    if op.action in ("CREATE", "UPDATE", "REMOVE") and not op.is_authorized:
        return OperationEffect.NONE
    match op.action:
        case "CREATE":
            return OperationEffect.CREATE
        case "UPDATE":
            return OperationEffect.UPDATE
        case "REMOVE":
            return OperationEffect.REMOVE
        case "NOOP":
            return OperationEffect.NOOP
        case "SKIP":
            return OperationEffect.SKIP
        case "CONFLICT":
            if op.is_authorized:
                raise UnknownPlanActionError(
                    f"Authorized CONFLICT is invalid for MCP: {op.target.logical_identity}"
                )
            return OperationEffect.NONE
        case "ERROR":
            return OperationEffect.NONE
        case _:
            raise UnknownPlanActionError(f"Unhandled MCP action: {op.action}")


def mcp_operation_finding(op: MCPOperation) -> Finding | None:
    """Produce a Finding for MCP conflict or error conditions."""
    if (op.action == "CONFLICT" and not op.is_authorized) or (
        op.action in ("CREATE", "UPDATE", "REMOVE") and not op.is_authorized
    ):
        return Finding(
            status="CONFLICT",
            code="MCP_CONFLICT",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    if op.action == "ERROR":
        return Finding(
            status="ERROR",
            code="MCP_ERROR",
            message=f"{op.target.agent}/{op.target.logical_identity}: {op.reason}",
            resource=str(op.target.path),
        )
    return None


def observe_mcp_operation(
    op: MCPOperation,
) -> tuple[PlanOperationView, Finding | None]:
    """Project an MCPOperation into a PlanOperationView and optional Finding."""
    try:
        effect = mcp_operation_effect(op)
        finding = mcp_operation_finding(op)
    except UnknownPlanActionError as err:
        effect = OperationEffect.NONE
        finding = Finding(
            status="ERROR",
            code="UNKNOWN_PLAN_ACTION",
            message=str(err),
            resource=str(op.target.path),
        )

    view = PlanOperationView(
        resource_type="mcp",
        resource_name=op.target.logical_identity,
        effect=effect,
        scope="global",
        agent=op.target.agent,
        target=str(op.target.path),
        reason=op.reason,
        domain_action=op.action,
        authorized=op.is_authorized,
    )
    return view, finding


def build_mcp_plan(
    aikito_dir: Path,
    home: Path,
    *,
    specs: Sequence[AgentSpec] | None = None,
    force: bool = False,
    force_targets: set[str] | Sequence[str] | None = None,
    desired_absent_servers: set[str] | Sequence[str] | None = None,
) -> MCPPlan:
    """Build a pure, immutable MCP synchronization plan without modifying any files or state.

    Enforces INV-MCP-01, INV-MCP-02, INV-MCP-04, INV-MCP-05, INV-CFG-01, INV-CFG-02, INV-CFG-03.
    """
    if specs is not None:
        raw_specs = list(specs)
    else:
        raw_specs = load_agent_specs(aikito_dir, home)

    absent_servers = set(desired_absent_servers or ())
    force_targets_set = set(force_targets or ())

    state = _load_state(home)
    entries = state.get("entries", {})
    state_snapshot_hash = _state_file_hash(home / STATE_FILE)

    groups: dict[str, list[AgentSpec]] = defaultdict(list)
    canonical_paths: dict[str, Path] = {}
    unsupported_operations: list[MCPOperation] = []

    for s in raw_specs:
        if s.config_format == "unsupported":
            # Unsupported agents never own a config file: skip without reading,
            # grouping, or collision-checking their (possibly empty) path.
            unsupported_operations.append(
                MCPOperation(
                    target=MCPConfigTarget(
                        path=s.config_path,
                        logical_identity=s.server,
                        key_path=("mcpServers", s.target_name),
                        format=s.config_format,
                        agent=s.agent,
                        target_name=s.target_name,
                    ),
                    action="SKIP",
                    reason=s.reason or "MCP synchronization is not supported",
                    spec=s,
                    is_authorized=True,
                )
            )
            continue
        phys_id = resolve_physical_identity(s.config_path)
        groups[phys_id].append(s)
        if phys_id not in canonical_paths:
            canonical_paths[phys_id] = s.config_path

    # Check collisions across specs
    for phys_id, g_specs in groups.items():
        canonical_path = canonical_paths[phys_id]
        formats = {s.config_format for s in g_specs if s.config_format}
        if len(formats) > 1:
            raise ConfigCollisionError(
                f"Conflicting formats declared for physical file '{canonical_path}': {sorted(formats)}"
            )

        seen_target_names: dict[str, AgentSpec] = {}
        for s in g_specs:
            t_name = s.target_name
            is_absent = (s.server in absent_servers) or (s.desired is None)
            if t_name in seen_target_names:
                prev_s = seen_target_names[t_name]
                prev_is_absent = (prev_s.server in absent_servers) or (
                    prev_s.desired is None
                )
                if prev_s.server != s.server:
                    raise ConfigCollisionError(
                        f"Colliding MCP server names: '{prev_s.server}' and '{s.server}' both map to target name '{t_name}' in '{canonical_path}'"
                    )
                elif prev_is_absent != is_absent:
                    raise ConfigCollisionError(
                        f"Conflicting operations on MCP server '{t_name}' in '{canonical_path}': conflicting REMOVE and UPDATE"
                    )
            else:
                seen_target_names[t_name] = s

    all_operations: list[MCPOperation] = []
    file_plans: list[MCPFilePlan] = []

    for phys_id, g_specs in groups.items():
        canonical_path = canonical_paths[phys_id]
        resolved_format = g_specs[0].config_format if g_specs else ""
        file_sensitive = any(
            s.contains_secret or s.config_format in ("claude_json", "agy_json")
            for s in g_specs
        )
        file_snapshot = capture_file_snapshot(
            canonical_path, format=resolved_format, sensitive=file_sensitive
        )
        file_existed = file_snapshot.exists
        orig_text = canonical_path.read_text(encoding="utf-8") if file_existed else ""
        current_text = orig_text
        group_ops: list[MCPOperation] = []

        for spec in g_specs:
            target_key = f"{spec.agent}/{spec.server}"
            is_authorized = (
                force
                or (target_key in force_targets_set)
                or (spec.server in force_targets_set)
            )
            is_absent = (spec.server in absent_servers) or (spec.desired is None)
            config_target = MCPConfigTarget(
                path=canonical_path,
                logical_identity=spec.server,
                key_path=("mcpServers", spec.target_name),
                format=spec.config_format,
                agent=spec.agent,
                sensitive=spec.contains_secret
                or spec.config_format in ("claude_json", "agy_json"),
                target_name=spec.target_name,
            )

            if is_absent:
                if not file_existed:
                    observed = MCPObservedEntry(
                        target=config_target,
                        exists=False,
                        fingerprint=None,
                        managed_fingerprint=None,
                        is_managed=False,
                        raw_entry=None,
                    )
                    op = MCPOperation(
                        target=config_target,
                        action="NOOP",
                        reason="Target file does not exist",
                        observed=observed,
                        desired=None,
                        spec=spec,
                        requires_force=False,
                        force_identity=target_key,
                        is_authorized=True,
                        state_transition=(spec.state_key, None),
                    )
                    group_ops.append(op)
                    all_operations.append(op)
                    continue

                try:
                    current = _read_entry(spec, current_text)
                except Exception as exc:
                    op = MCPOperation(
                        target=config_target,
                        action="ERROR",
                        reason=f"Failed to read entry: {exc}",
                        spec=spec,
                        is_authorized=False,
                    )
                    group_ops.append(op)
                    all_operations.append(op)
                    continue

                previous = entries.get(spec.state_key, {})
                managed_fp = previous.get("fingerprint")
                current_fp = _fingerprint(current) if current is not None else None
                observed = MCPObservedEntry(
                    target=config_target,
                    exists=current is not None,
                    fingerprint=current_fp,
                    managed_fingerprint=managed_fp,
                    is_managed=managed_fp is not None,
                    raw_entry=current,
                )

                if current is None:
                    op = MCPOperation(
                        target=config_target,
                        action="NOOP",
                        reason="Already absent",
                        observed=observed,
                        desired=None,
                        spec=spec,
                        requires_force=False,
                        force_identity=target_key,
                        is_authorized=True,
                        state_transition=(spec.state_key, None),
                    )
                else:
                    safe_to_remove = is_authorized or (
                        managed_fp is not None and current_fp == managed_fp
                    )
                    if not safe_to_remove:
                        op = MCPOperation(
                            target=config_target,
                            action="CONFLICT",
                            reason="Existing config was not last written by aikito; review it or rerun with --force",
                            observed=observed,
                            desired=None,
                            spec=spec,
                            requires_force=True,
                            force_identity=target_key,
                            is_authorized=False,
                        )
                    else:
                        op = MCPOperation(
                            target=config_target,
                            action="REMOVE",
                            reason="Removed from agent configuration",
                            observed=observed,
                            desired=None,
                            spec=spec,
                            requires_force=(
                                managed_fp is None or current_fp != managed_fp
                            ),
                            force_identity=target_key,
                            is_authorized=True,
                            state_transition=(spec.state_key, None),
                        )
                        try:
                            current_text = _remove_entry(spec, current_text)
                        except Exception as exc:
                            op = MCPOperation(
                                target=config_target,
                                action="ERROR",
                                reason=f"Failed to remove entry: {exc}",
                                spec=spec,
                                is_authorized=False,
                            )
                group_ops.append(op)
                all_operations.append(op)
                continue

            # Desired Present
            if not spec.enabled:
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=spec.reason or "Agent or server disabled",
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            if not _agent_detected(spec):
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=f"Agent '{spec.agent}' is not installed or detected",
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            try:
                current = _read_entry(spec, current_text) if file_existed else None
            except Exception as exc:
                op = MCPOperation(
                    target=config_target,
                    action="ERROR",
                    reason=f"Failed to read entry: {exc}",
                    spec=spec,
                    is_authorized=False,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            previous = entries.get(spec.state_key, {})
            managed_fp = previous.get("fingerprint")
            current_fp = _fingerprint(current) if current is not None else None
            desired_fp = _fingerprint(spec.desired)
            observed = MCPObservedEntry(
                target=config_target,
                exists=current is not None,
                fingerprint=current_fp,
                managed_fingerprint=managed_fp,
                is_managed=managed_fp is not None,
                raw_entry=current,
            )
            desired_entry = MCPDesiredEntry(
                target=config_target,
                fingerprint=desired_fp,
                contains_secret=spec.contains_secret,
                missing_credential_env=spec.missing_credential_env,
                live_command=spec.live_command,
                auth_command=spec.auth_command,
                raw_desired=spec.desired,
            )

            if spec.missing_credential_env:
                op = MCPOperation(
                    target=config_target,
                    action="SKIP",
                    reason=f"Requires missing environment variable: {spec.missing_credential_env}",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    is_authorized=True,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            if _entry_matches_desired(spec, current):
                op = MCPOperation(
                    target=config_target,
                    action="NOOP",
                    reason="Already synchronized",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    requires_force=False,
                    force_identity=target_key,
                    is_authorized=True,
                    state_transition=(spec.state_key, desired_fp),
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            safe_to_update = (
                current is None
                or is_authorized
                or (managed_fp is not None and current_fp == managed_fp)
            )
            if not safe_to_update:
                op = MCPOperation(
                    target=config_target,
                    action="CONFLICT",
                    reason="Existing config was not last written by aikito; review it or rerun with --force",
                    observed=observed,
                    desired=desired_entry,
                    spec=spec,
                    requires_force=True,
                    force_identity=target_key,
                    is_authorized=False,
                )
                group_ops.append(op)
                all_operations.append(op)
                continue

            action = "CREATE" if current is None else "UPDATE"
            reason = "New server entry" if current is None else "Configuration updated"
            requires_force_flag = current is not None and (
                managed_fp is None or managed_fp != current_fp
            )
            op = MCPOperation(
                target=config_target,
                action=action,
                reason=reason,
                observed=observed,
                desired=desired_entry,
                spec=spec,
                requires_force=requires_force_flag,
                force_identity=target_key,
                is_authorized=True,
                state_transition=(spec.state_key, desired_fp),
            )
            group_ops.append(op)
            all_operations.append(op)

            try:
                current_text = _update_entry(spec, current_text)
            except Exception as exc:
                err_op = MCPOperation(
                    target=config_target,
                    action="ERROR",
                    reason=f"Failed to update entry: {exc}",
                    spec=spec,
                    is_authorized=False,
                )
                group_ops[-1] = err_op
                all_operations[-1] = err_op

        file_mutating = current_text != orig_text
        file_plan = MCPFilePlan(
            path=canonical_path,
            physical_identity=phys_id,
            format=resolved_format,
            sensitive=file_sensitive,
            pre_image=file_snapshot,
            operations=tuple(group_ops),
            orig_content=orig_text if file_existed else None,
            final_content=current_text if file_mutating else orig_text,
        )
        file_plans.append(file_plan)

    all_operations.extend(unsupported_operations)
    return MCPPlan(
        operations=tuple(all_operations),
        file_plans=tuple(file_plans),
        state_snapshot_hash=state_snapshot_hash,
        specs=tuple(raw_specs),
    )


def _map_operation_to_status(op: MCPOperation) -> str:
    """Map pure MCPOperation action to user-facing inspection status."""
    if op.action == "NOOP":
        return "OK"
    elif op.action == "CREATE":
        return "MISSING"
    elif op.action == "UPDATE":
        return "UPDATE"
    elif op.action == "CONFLICT":
        return "DRIFT"
    elif op.action == "SKIP":
        if op.spec and op.spec.missing_credential_env:
            if op.observed and op.observed.exists and op.observed.raw_entry is not None:
                if _entry_matches_desired(op.spec, op.observed.raw_entry):
                    return "OK"
                return "DRIFT"
        return "SKIP"
    elif op.action == "ERROR":
        return "ERROR"
    return op.action


def evaluate_spec_status(
    spec: AgentSpec,
    state: dict[str, Any] | None = None,
    home: Path | None = None,
    plan: MCPPlan | None = None,
) -> str:
    """Evaluates synchronization status for a single AgentSpec via pure MCP planning.

    Guarantees status, diff, and Doctor share the exact same decision engine as sync.
    Returns one of: 'OK', 'MISSING', 'UPDATE', 'DRIFT', 'ERROR', 'SKIP'.
    """
    if plan is not None:
        for op in plan.operations:
            if (
                op.target.agent == spec.agent
                and op.target.logical_identity == spec.server
            ):
                return _map_operation_to_status(op)

    effective_home = home or spec.home
    if effective_home is None:
        effective_home = Path.home()

    try:
        single_plan = build_mcp_plan(
            aikito_dir=effective_home,
            home=effective_home,
            specs=[spec],
        )
        if single_plan.operations:
            return _map_operation_to_status(single_plan.operations[0])
        return "SKIP"
    except Exception:
        return "ERROR"


def _backup_file_plan(home: Path, file_plan: MCPFilePlan) -> Path | None:
    if not file_plan.path.exists():
        return None
    agent = file_plan.operations[0].target.agent if file_plan.operations else "common"
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = home / BACKUP_DIR / agent / f"{timestamp}-{file_plan.path.name}"
    backup.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(file_plan.path, backup)
    return backup


def execute_mcp_plan(
    plan: MCPPlan,
    home: Path,
    *,
    output: Callable[[str], None] = print,
) -> MCPExecutionResult:
    """Apply an MCPPlan transactionally with atomic write once, backup, rollback, and state commit.

    Enforces INV-MCP-03, INV-MCP-06, INV-MCP-08.
    """
    # 1. Validate preconditions
    try:
        plan.validate_preconditions(home)
    except StaleConfigPlanError as exc:
        output(f"[ERROR] MCP plan is stale: {exc}")
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=0,
            skipped_count=0,
            conflict_count=0,
            failed_count=len(plan.operations),
            error_message=f"Plan is stale: {exc}",
        )

    # 2. Check conflicts / authorization
    if not plan.can_apply:
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=plan.conflicts_count,
            failed_count=0,
            error_message="Plan contains unauthorized conflicts",
        )

    # 3. Prepare new state in memory
    state = _load_state(home)
    new_entries = dict(state.get("entries", {}))

    for op in plan.operations:
        if not op.is_authorized:
            continue
        if op.action in ("NOOP", "CREATE", "UPDATE") and op.state_transition:
            state_key, fp = op.state_transition
            new_entries[state_key] = {
                "fingerprint": fp,
                "config_path": str(op.target.path),
                "target_name": op.target.target_name,
            }
        elif op.action == "REMOVE":
            if op.state_transition:
                new_entries.pop(op.state_transition[0], None)
            if op.spec:
                new_entries.pop(op.spec.state_key, None)
            srv_suffix = f":{op.target.logical_identity}"
            to_del = [k for k in new_entries if k.endswith(srv_suffix)]
            for k in to_del:
                new_entries.pop(k, None)

    new_state = dict(state, entries=new_entries)
    state_path = home / STATE_FILE
    state_tmp: Path | None = None

    try:
        state_path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=state_path.parent,
            delete=False,
            encoding="utf-8",
            suffix=".tmp",
        ) as sf:
            sf.write(
                json.dumps(new_state, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n"
            )
            state_tmp = Path(sf.name)
    except Exception as exc:
        output(f"[ERROR] Failed to prepare state file for atomic save: {exc}")
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=len(plan.operations),
            error_message=f"Failed to prepare state file: {exc}",
        )

    mutating_files = [fp for fp in plan.file_plans if fp.will_mutate]
    if not mutating_files:
        try:
            os.replace(state_tmp, state_path)
        except Exception as exc:
            if state_tmp and state_tmp.exists():
                try:
                    state_tmp.unlink()
                except Exception:
                    pass
            output(f"[ERROR] Failed to update state file: {exc}")
            return MCPExecutionResult(
                success=False,
                applied_count=0,
                noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
                skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
                conflict_count=0,
                failed_count=1,
                error_message=f"Failed to update state: {exc}",
            )
        return MCPExecutionResult(
            success=True,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=0,
        )

    # 4. Take backups for all eligible files BEFORE modifying any runtime files
    backups_created: list[tuple[MCPFilePlan, Path]] = []
    backup_error: Exception | None = None
    failed_fp: MCPFilePlan | None = None

    for fp in mutating_files:
        if not fp.should_backup:
            continue
        try:
            first_spec = (
                fp.operations[0].spec
                if fp.operations and fp.operations[0].spec
                else None
            )
            bk = (
                _backup_config(home, first_spec)
                if first_spec
                else _backup_file_plan(home, fp)
            )
            if bk:
                backups_created.append((fp, bk))
        except Exception as exc:
            backup_error = exc
            failed_fp = fp
            first_op = fp.operations[0] if fp.operations else None
            agent_srv = (
                f"{first_op.target.agent}/{first_op.target.logical_identity}"
                if first_op
                else str(fp.path)
            )
            output(
                f"[ERROR] {agent_srv}: backup failed ({exc}); "
                "aborting before modifying runtime files"
            )
            break

    if backup_error is not None:
        for _f, bk in backups_created:
            try:
                bk.unlink(missing_ok=True)
            except Exception:
                pass
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            failed_files=(failed_fp.path,) if failed_fp else (),
            error_message=f"Backup failed: {backup_error}",
        )

    # 5. Atomic write of each mutating file
    committed: list[tuple[MCPFilePlan, Path | None]] = []
    write_error: Exception | None = None
    failed_write_fp: MCPFilePlan | None = None

    for fp in mutating_files:
        backup_path = next((b for f, b in backups_created if f.path == fp.path), None)
        try:
            _atomic_write(
                fp.path, fp.final_content or "", secure_permissions=fp.sensitive
            )
            committed.append((fp, backup_path))
        except Exception as exc:
            write_error = exc
            failed_write_fp = fp
            first_op = fp.operations[0] if fp.operations else None
            agent_srv = (
                f"{first_op.target.agent}/{first_op.target.logical_identity}"
                if first_op
                else str(fp.path)
            )
            output(
                f"[ERROR] {agent_srv}: write failed ({exc}); "
                "rolling back all committed agent configs"
            )
            break

    def _rollback() -> tuple[bool, set[Path], list[str]]:
        all_succeeded = True
        retained_backups: set[Path] = set()
        warnings: list[str] = []
        for c_fp, c_bk in committed:
            rb_ok = False
            try:
                if c_fp.pre_image.exists:
                    _atomic_write(
                        c_fp.path,
                        c_fp.orig_content if c_fp.orig_content is not None else "",
                        secure_permissions=c_fp.sensitive,
                    )
                elif c_fp.path.exists():
                    c_fp.path.unlink()
                rb_ok = True
            except Exception as rb_exc:
                all_succeeded = False
                recovery_hint = (
                    f"; backup retained at {c_bk}" if c_bk is not None else ""
                )
                first_op = c_fp.operations[0] if c_fp.operations else None
                agent_srv = (
                    f"{first_op.target.agent}/{first_op.target.logical_identity}"
                    if first_op
                    else str(c_fp.path)
                )
                msg = f"{agent_srv}: rollback failed ({rb_exc}); manual inspection required{recovery_hint}"
                output(f"[WARN] {msg}")
                warnings.append(msg)

            if c_bk is not None:
                if not rb_ok:
                    retained_backups.add(c_bk)
                else:
                    try:
                        c_bk.unlink(missing_ok=True)
                    except Exception:
                        pass
        return all_succeeded, retained_backups, warnings

    if write_error is not None:
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        rb_success, retained_bks, rb_warnings = _rollback()
        for _f, bk in backups_created:
            if bk not in retained_bks:
                try:
                    bk.unlink(missing_ok=True)
                except Exception:
                    pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            failed_files=(failed_write_fp.path,) if failed_write_fp else (),
            backups_created=tuple(retained_bks),
            backup_warnings=tuple(rb_warnings),
            error_message=f"Write failed: {write_error}",
            recovery_required=not rb_success,
            recovery_guidance="; ".join(rb_warnings) if not rb_success else None,
        )

    # 6. Promote state temp file
    try:
        os.replace(state_tmp, state_path)
    except Exception as exc:
        output(
            f"[ERROR] All agent configs written but state save failed: {exc}; "
            "rolling back runtime changes to keep state consistent"
        )
        if state_tmp and state_tmp.exists():
            try:
                state_tmp.unlink()
            except Exception:
                pass
        rb_success, retained_bks, rb_warnings = _rollback()
        for _f, bk in backups_created:
            if bk not in retained_bks:
                try:
                    bk.unlink(missing_ok=True)
                except Exception:
                    pass
        return MCPExecutionResult(
            success=False,
            applied_count=0,
            noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
            skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
            conflict_count=0,
            failed_count=1,
            backups_created=tuple(retained_bks),
            backup_warnings=tuple(rb_warnings),
            error_message=f"State promotion failed: {exc}",
            recovery_required=not rb_success,
            recovery_guidance="; ".join(rb_warnings) if not rb_success else None,
        )

    # 7. Success! Output sync logs
    for fp, bk in committed:
        first_spec = True
        for op in fp.operations:
            if op.is_authorized and op.action in ("CREATE", "UPDATE", "REMOVE"):
                action_str = (
                    f"{op.action.lower()}d" if op.action != "REMOVE" else "removed from"
                )
                output(
                    f"[SYNC] {op.target.agent}/{op.target.logical_identity}: {action_str} {fp.path}"
                )
                if first_spec and bk:
                    output(f"[BACKUP] {bk}")
                    first_spec = False
                if op.spec and op.spec.auth_command:
                    output(
                        f"[AUTH] aikito auth mcp {op.target.agent} {op.target.logical_identity}"
                    )

    all_backups = tuple(b for _f, b in backups_created)
    return MCPExecutionResult(
        success=True,
        applied_count=plan.changes_count,
        noop_count=sum(1 for op in plan.operations if op.action == "NOOP"),
        skipped_count=sum(1 for op in plan.operations if op.action == "SKIP"),
        conflict_count=0,
        failed_count=0,
        backups_created=all_backups,
    )


def sync_mcp_configs(
    *,
    aikito_dir: Path,
    home: Path,
    dry_run: bool = False,
    force: bool = False,
    plan: MCPPlan | None = None,
    output: Callable[[str], None] = print,
) -> bool:
    if plan is None:
        plan = build_mcp_plan(aikito_dir, home, force=force)

    # Output inspection results
    for op in plan.operations:
        target_key = f"{op.target.agent}/{op.target.logical_identity}"
        if op.action == "SKIP":
            if op.spec and op.spec.missing_credential_env:
                output(
                    f"[WARN] {target_key}: skipped due to missing credential "
                    f"environment variable: {op.spec.missing_credential_env}"
                )
            elif op.spec and not op.spec.enabled:
                output(f"[SKIP] {target_key}: {op.reason}")
            else:
                output(
                    f"[SKIP] {op.target.agent} not detected: {op.target.path.parent}"
                )
        elif op.action == "NOOP":
            output(f"[OK] {target_key}: already synchronized")
        elif op.action == "CONFLICT":
            output(
                f"[CONFLICT] {target_key}: existing config was not "
                "last written by aikito; review it or rerun with --force"
            )

    if dry_run:
        for op in plan.operations:
            if op.action in ("CREATE", "UPDATE") and op.is_authorized:
                action_name = "create" if op.action == "CREATE" else "update"
                output(
                    f"[DRY-RUN] {op.target.agent}/{op.target.logical_identity}: would {action_name} entry"
                )
        if not plan.can_apply:
            return False
        # In dry run, converge state for already OK entries just like legacy sync
        state = _load_state(home)
        entries = state.get("entries", {})
        for op in plan.operations:
            if op.action == "NOOP" and op.state_transition:
                state_key, fp = op.state_transition
                if entries.get(state_key, {}).get("fingerprint") != fp:
                    entries[state_key] = {
                        "fingerprint": fp,
                        "config_path": str(op.target.path),
                        "target_name": op.target.target_name,
                    }
        return plan.can_apply

    if not plan.can_apply:
        return False

    result = execute_mcp_plan(plan, home, output=output)
    return result.success


def sync_remove_mcp_from_agents(
    *,
    specs: list[AgentSpec],
    home: Path,
    output: Callable[[str], None] = print,
    force: bool = False,
) -> bool:
    server_names = {s.server for s in specs}
    plan = build_mcp_plan(
        aikito_dir=home,
        home=home,
        specs=specs,
        desired_absent_servers=server_names,
        force=force,
    )
    if not plan.can_apply:
        for op in plan.operations:
            if op.action == "CONFLICT" and not op.is_authorized:
                output(
                    f"[CONFLICT] {op.target.agent}/{op.target.logical_identity}: existing config was not "
                    "last written by aikito; review it or rerun with --force"
                )
        return False
    result = execute_mcp_plan(plan, home, output=output)
    return result.success
