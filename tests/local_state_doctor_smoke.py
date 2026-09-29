"""Exercise real doctor CLI diagnostics and cleanup in an isolated home."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from aikito.skill_state import get_binding_hash, validate_state_store_root
from workspace_reconcile_smoke import _workspace


def state_file(home: Path, workspace: Path, checkout: Path) -> Path:
    directory, error = validate_state_store_root(home, create_if_missing=True)
    assert error is None
    path = directory / f"{get_binding_hash(workspace, 'demo', checkout)}.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "generation": 1,
                "workspace_root": str(workspace),
                "project_name": "demo",
                "physical_checkout": str(checkout),
                "records": {},
            }
        ),
        encoding="utf-8",
    )
    return path


RUNNER = """
import sys
from pathlib import Path
from unittest.mock import patch
home = Path(sys.argv.pop(1))
with patch.object(Path, 'home', return_value=home):
    from aikito.cli import main
    main()
"""


def exercise(base: Path) -> None:
    home = base / "home"
    workspace = _workspace(base / "workspace")
    checkout = base / "checkout"
    checkout.mkdir()
    live = state_file(home, workspace, checkout)
    unavailable = state_file(home, workspace, base / "missing-checkout")
    with tempfile.TemporaryDirectory(prefix="aikito-abandoned-") as temporary:
        abandoned = Path(temporary)
        stale = state_file(home, abandoned / "workspace", abandoned / "checkout")
    env = dict(os.environ, AIKITO_DIR=str(workspace))

    def doctor(*args):
        result = subprocess.run(
            [sys.executable, "-c", RUNNER, str(home), "doctor", "--json", *args],
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode in (0, 1), result.stderr
        return json.loads(result.stdout)

    before = {p: p.read_bytes() for p in (stale, live, unavailable)}
    report = doctor()
    section = next(s for s in report["sections"] if s["name"] == "LocalState")
    findings_by_code = {f.get("code"): f for f in section["findings"]}
    assert "local-state.stale" in findings_by_code, (
        f"Expected local-state.stale, got: {section['findings']}"
    )
    finding = findings_by_code["local-state.stale"]
    assert finding["fix_hint"] == "aikito doctor --fix"
    assert finding["actions"][0]["command"] == finding["fix_hint"]
    assert all(p.read_bytes() == value for p, value in before.items())
    report = doctor("--fix")
    assert not stale.exists()
    assert any(str(stale) in message for message in report["fixes"])
    assert live.read_bytes() == before[live]
    assert unavailable.read_bytes() == before[unavailable]
    (base / "checked.json").write_text(json.dumps(report), encoding="utf-8")


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Local state doctor checks passed")
