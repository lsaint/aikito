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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from ..compat import resolve_executable, secure_file_permissions
from ..config_runtime import StaleConfigPlanError
from .adapters import (
    _entry_matches_desired,
    _load_document,
    _parse_jsonc,
    _read_entry,
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
from .executor import _load_state
from .loader import (
    _agent_detected,
    _find_agent_spec,
    _load_basic_token_auth,
    load_agent_specs,
)
from .planner import (
    build_mcp_plan,
    evaluate_spec_status,
    mcp_operation_effect,
    mcp_operation_finding,
    observe_mcp_operation,
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
