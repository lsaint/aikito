"""E2E smoke test suite for encrypted remote, scoped export, recipient trust, and web console.

Managed under pytest runner for parallel execution, unified reporting, and tracebacks.
"""

from __future__ import annotations

import os
import subprocess
import sys
import venv
from pathlib import Path

import pytest

pytestmark = [pytest.mark.smoke]

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"


def _run_smoke(
    script_name: str,
    *args: str,
    env: dict[str, str] | None = None,
    executable: str | Path | None = None,
) -> None:
    script = TESTS_DIR / script_name
    assert script.is_file(), f"Smoke script not found: {script}"
    python_bin = str(executable) if executable else sys.executable
    cmd = [python_bin, str(script), *args]
    process_env = dict(os.environ)
    if env:
        process_env.update(env)
    res = subprocess.run(cmd, env=process_env, capture_output=True, text=True)
    assert res.returncode == 0, (
        f"Smoke script {script_name} failed with code {res.returncode}.\n"
        f"Args: {args}\n"
        f"STDOUT:\n{res.stdout}\n"
        f"STDERR:\n{res.stderr}"
    )


def test_workspace_encrypted_core_smoke(tmp_path: Path) -> None:
    # Requires an isolated interpreter without optional [encrypted] dependencies
    env_dir = tmp_path / "clean_env"
    venv.create(env_dir, with_pip=False, clear=True)
    clean_python = (
        env_dir / "Scripts" / "python.exe"
        if sys.platform == "win32"
        else env_dir / "bin" / "python"
    )
    _run_smoke(
        "workspace_encrypted_core_smoke.py",
        str(tmp_path / "enc_core"),
        executable=clean_python,
    )


def test_workspace_encrypted_remote_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_encrypted_remote_smoke.py", str(tmp_path / "enc_remote"))


def test_workspace_encrypted_discovery_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_encrypted_discovery_smoke.py", str(tmp_path / "enc_disc"))


def test_web_console_access_smoke(tmp_path: Path) -> None:
    _run_smoke("web_console_access_smoke.py", str(tmp_path / "console"))


def test_workspace_encrypted_sync_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_encrypted_sync_smoke.py", str(tmp_path / "enc_sync"))


def test_workspace_context_export_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_context_export_smoke.py", str(tmp_path / "ctx_export"))


def test_workspace_scoped_export_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_scoped_export_smoke.py", str(tmp_path / "scoped_export"))


def test_workspace_scoped_submit_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_scoped_submit_smoke.py", str(tmp_path / "scoped_submit"))


def test_workspace_recipient_key_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_recipient_key_smoke.py", str(tmp_path / "recipient"))


def test_workspace_remote_pairing_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_pairing_smoke.py", str(tmp_path / "pairing"))


def test_workspace_remote_claim_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_claim_smoke.py", str(tmp_path / "claim"))


def test_project_auto_apply_smoke(tmp_path: Path) -> None:
    _run_smoke("project_auto_apply_smoke.py", str(tmp_path / "auto_apply"))


def test_global_external_skills_smoke(tmp_path: Path) -> None:
    _run_smoke("global_external_skills_smoke.py", str(tmp_path / "ext_skills"))


def test_agent_adapters_smoke() -> None:
    _run_smoke("agent_adapters_smoke.py")


def test_adopt_definitions_smoke(tmp_path: Path) -> None:
    _run_smoke(
        "adopt_definitions_smoke.py",
        env={"AIKITO_ADOPT_SMOKE_ROOT": str(tmp_path / "adopt")},
    )


def test_maintenance_cli_smoke(tmp_path: Path) -> None:
    _run_smoke("maintenance_cli_smoke.py", str(tmp_path / "maintenance"))
