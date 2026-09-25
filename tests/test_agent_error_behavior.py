"""CLI-level regressions: invalid agents.toml keeps v1.50.0 user-visible behavior."""

from __future__ import annotations
from layout_helpers import write_agents

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import cli
from aikito.doctor import run_doctor
from aikito.init import init_workspace
from aikito.workspace_sync import build_global_sync_plan

MESSAGE = "Agent 'a' mcp section must be a table"


class InvalidAgentsConfigBehaviorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        root = Path(self.td.name)
        self.home = root / "home"
        self.home.mkdir()
        self.ws = root / "ws"
        init_workspace(self.ws, self.home)
        write_agents(self.ws, '[agents.a]\ndisplay_name = "A"\nmcp = 1\n')

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_status_reports_error_without_traceback(self) -> None:
        stderr = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_DIR": str(self.ws)}),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.argv", ["aikito", "status"]),
            patch("sys.stdout", new_callable=io.StringIO),
            patch("sys.stderr", stderr),
        ):
            with self.assertRaises(SystemExit) as ctx:
                cli.main()
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn(f"[ERROR] {MESSAGE}", stderr.getvalue())

    def test_doctor_degrades_per_section(self) -> None:
        report = run_doctor(self.ws, self.home)
        messages = {
            section.name: [finding.message for finding in section.findings]
            for section in report.sections
        }
        self.assertIn(f"Cannot load Agent definitions: {MESSAGE}", messages["Symlinks"])
        self.assertIn(f"Cannot check subagent orphans: {MESSAGE}", messages["Orphans"])
        self.assertIn(f"Cannot load MCP specs: {MESSAGE}", messages["Drift"])
        self.assertIn(
            f"Cannot load agents for config check: {MESSAGE}",
            messages["Configuration"],
        )

    def test_global_sync_plan_keeps_mcp_config_error_code(self) -> None:
        plan = build_global_sync_plan(self.ws, self.home)
        self.assertFalse(plan.can_apply)
        codes = {(finding.code, finding.message) for finding in plan.findings}
        self.assertIn(("MCP_CONFIG_ERROR", MESSAGE), codes)


if __name__ == "__main__":
    unittest.main()
