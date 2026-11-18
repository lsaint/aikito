"""E2E smoke test suite for workspace reconcile and remote operations.

Managed under pytest runner for parallel execution, unified reporting, and tracebacks.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = [pytest.mark.smoke]

ROOT = Path(__file__).resolve().parents[1]
TESTS_DIR = ROOT / "tests"


def _run_smoke(script_name: str, *args: str, env: dict[str, str] | None = None) -> None:
    script = TESTS_DIR / script_name
    assert script.is_file(), f"Smoke script not found: {script}"
    cmd = [sys.executable, str(script), *args]
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


def test_workspace_bounded_reconcile_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_bounded_reconcile_smoke.py", str(tmp_path / "bounded"))


def test_workspace_reconcile_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_reconcile_smoke.py", str(tmp_path / "reconcile"))


def test_workspace_reconcile_resources_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_reconcile_resources_smoke.py", str(tmp_path / "resources"))


def test_workspace_reconcile_acceptance_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_reconcile_acceptance.py", str(tmp_path / "acceptance"))


def test_workspace_serialized_remote_acceptance_smoke(tmp_path: Path) -> None:
    _run_smoke(
        "workspace_serialized_remote_acceptance.py",
        str(tmp_path / "serialized_acceptance"),
    )


def test_workspace_memory_remote_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_memory_remote_smoke.py", str(tmp_path / "memory"))


def test_workspace_http_remote_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_http_remote_smoke.py", str(tmp_path / "http"))


def test_workspace_remote_binding_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_binding_smoke.py", str(tmp_path / "binding"))


def test_workspace_remote_format_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_format_smoke.py", str(tmp_path / "format"))


def test_workspace_remote_cli_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_cli_smoke.py", str(tmp_path / "cli"))


def test_workspace_remote_watch_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_watch_smoke.py", str(tmp_path / "watch"))


def test_workspace_remote_service_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_service_smoke.py", str(tmp_path / "service"))


def test_workspace_external_server_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_external_server_smoke.py", str(tmp_path / "external"))


def test_workspace_commit_capacity_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_commit_capacity_smoke.py", str(tmp_path / "capacity"))


def test_workspace_serialized_remote_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_serialized_remote_smoke.py", str(tmp_path / "serialized"))


def test_workspace_remote_store_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_store_smoke.py", str(tmp_path / "store"))


def test_workspace_remote_commit_smoke(tmp_path: Path) -> None:
    commit_dir = tmp_path / "commit"
    _run_smoke("workspace_remote_commit_smoke.py", str(commit_dir))
    _run_smoke("workspace_remote_commit_smoke.py", str(commit_dir), "--verify-only")


def test_workspace_remote_receipts_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_remote_receipts_smoke.py", str(tmp_path / "receipts"))


def test_workspace_pending_commit_smoke(tmp_path: Path) -> None:
    pending_dir = tmp_path / "pending"
    for stage in ("prepare", "complete", "clear"):
        _run_smoke(
            "workspace_pending_commit_smoke.py", str(pending_dir), "--stage", stage
        )


def test_workspace_commit_recovery_smoke(tmp_path: Path) -> None:
    recovery_dir = tmp_path / "recovery"
    for stage in ("send", "resume"):
        _run_smoke(
            "workspace_commit_recovery_smoke.py", str(recovery_dir), "--stage", stage
        )


def test_workspace_unreliable_remote_smoke(tmp_path: Path) -> None:
    for backend in ("filesystem", "memory"):
        _run_smoke(
            "workspace_unreliable_remote_smoke.py",
            str(tmp_path / "unreliable" / backend),
            "--backend",
            backend,
        )


def test_workspace_revision_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_revision_smoke.py", str(tmp_path / "revision"))


def test_local_state_doctor_smoke(tmp_path: Path) -> None:
    _run_smoke("local_state_doctor_smoke.py", str(tmp_path / "doctor"))


def test_workspace_reconcile_boundary_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_reconcile_boundary_smoke.py", str(tmp_path / "boundary"))


def test_workspace_payload_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_payload_smoke.py", str(tmp_path / "payload"))


def test_workspace_reconcile_resolution_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_reconcile_resolution_smoke.py", str(tmp_path / "resolution"))


def test_workspace_import_smoke(tmp_path: Path) -> None:
    _run_smoke("workspace_import_smoke.py", str(tmp_path / "import_base"))
    _run_smoke(
        "workspace_import_smoke.py",
        str(tmp_path / "import_exec"),
        "--execution-case",
    )
    _run_smoke(
        "workspace_import_smoke.py",
        str(tmp_path / "import_blocked"),
        "--blocked-inbox-case",
    )
    _run_smoke(
        "workspace_import_smoke.py",
        str(tmp_path / "import_partial"),
        "--conflict-case",
    )
