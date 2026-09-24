"""Authentication workflows and browser helpers for agent MCP configs."""

import os
import queue
import shutil
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from typing import Any, Callable

from ..compat import resolve_executable
from .adapters import _entry_matches_desired, _read_entry
from .loader import _agent_detected, _find_agent_spec, load_agent_specs
from .model import BROWSER_HELPER, MCPConfigError
from .redact import (
    _environment_reference,
    _is_authorization_url,
    _redact_sensitive_urls,
    _urls_in_text,
)


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
