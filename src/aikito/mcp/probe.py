"""Remote MCP endpoint probing and tool discovery."""

import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, build_opener

from ..compat import resolve_executable
from .adapters import read_entry
from .auth import describe_mcp_auth
from .model import (
    AgentSpec,
    LiveMCPResult,
    MCPConfigError,
    MCPToolProbeResult,
    _MCPProbeError,
    _RejectRedirects,
)
from .redact import (
    _headers_contain_credentials,
    _is_loopback_url,
    _redact_probe_error,
    environment_reference,
)

_MCP_PROTOCOL_VERSION = "2025-11-25"
_MCP_USER_AGENT = "aikito"
_MAX_MCP_RESPONSE_BYTES = 8 * 1024 * 1024


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
            env_name = environment_reference(raw_value)
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
