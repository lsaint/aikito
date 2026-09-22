"""Characterization tests for Phase 8: Application Layer, Diagnostics, and Adoption.

Freezes baseline behavior in Aikito 1.48.0 prior to Phase 8 convergence:
1. Workspace sync dry-run behavior, stdout marker capture, and SyncPlan statistics.
2. Global sync orchestration and GlobalSyncResult in cli.py.
3. Subagent legacy PlanItem / build_plan compatibility view.
4. Doctor diagnostics consistency across subagent and MCP states.
5. Adoption plan construction, finding collection, summary, and backup creation.
6. Public Python API export boundary (__all__ frozen to Project symbols).
7. Web Console read-only data collection and credential redaction discrepancies.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import aikito
from aikito.adopt import (
    build_adopt_plan,
    collect_adopt_findings,
    execute_adoption,
    summarize_adopt_plan,
)
from aikito.cli import GlobalSyncResult, sync_global_resources
from aikito.doctor import run_doctor
from aikito.mcp import redact_mcp_entry
from aikito.subagent import SubagentPlan, build_subagent_plan
from aikito.sync_plan import SyncPlan
from aikito.web_console import ConsoleData, _redact


class Phase8CharacterizationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()
        self.ws = self.root / "workspace"
        self.home = self.root / "home"
        self.ws.mkdir()
        self.home.mkdir()

        # Workspace configuration
        (self.ws / "config.toml").write_text(
            '[workspace]\nversion = "1.0"\n', encoding="utf-8"
        )
        (self.ws / "skills.toml").write_text(
            "skills = []\n", encoding="utf-8"
        )
        (self.ws / "subagents.toml").write_text(
            '[subagents.reviewer]\ndescription = "Code reviewer"\nagents = ["claude-code"]\n',
            encoding="utf-8",
        )
        (self.ws / "skills").mkdir()
        (self.ws / "mcps").mkdir()
        (self.ws / "subagents").mkdir()
        (self.ws / "projects").mkdir()
        (self.ws / "global").mkdir()
        (self.ws / "global" / "AGENTS.md").write_text(
            "# Global Instructions\n", encoding="utf-8"
        )

        # Configured agents
        agents_toml = """
[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"

[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"

[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
"""
        (self.ws / "agents.toml").write_text(agents_toml, encoding="utf-8")

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_sync_plan_is_workspace_sync_plan(self) -> None:
        """Verify SyncPlan is unified with WorkspaceSyncPlan under INV-APP-03."""
        from aikito.workspace_sync import WorkspaceSyncPlan

        self.assertIs(SyncPlan, WorkspaceSyncPlan)

    def test_global_sync_cli_orchestration_characterization(self) -> None:
        """Freeze sync_global_resources in cli.py returning GlobalSyncResult."""
        with patch("aikito.cli.get_agents_dir", return_value=self.home / ".agents"):
            result = sync_global_resources(
                self.ws,
                self.home,
                dry_run=True,
            )
            self.assertIsInstance(result, GlobalSyncResult)
            self.assertTrue(result.success)
            self.assertEqual(result.refreshed_bundled, ("aikito", "durable-memory"))
            self.assertIsNone(result.error_message)

    def test_subagent_legacy_plan_compatibility_characterization(self) -> None:
        """Verify subagent build_subagent_plan returns SubagentPlan and legacy view is retired."""
        subagent_md = (
            "---\n"
            "description: Code reviewer\n"
            "---\n"
            "Review prompt\n"
        )
        (self.ws / "subagents" / "reviewer.md").write_text(subagent_md, encoding="utf-8")

        # Formal structured plan
        formal_plan = build_subagent_plan(self.ws, home=self.home, gate_installed=False)
        self.assertIsInstance(formal_plan, SubagentPlan)
        self.assertTrue(formal_plan.can_apply)
        self.assertEqual(len(formal_plan.operations), 1)
        self.assertEqual(formal_plan.operations[0].action, "CREATE")
        self.assertEqual(formal_plan.operations[0].target.logical_identity, "reviewer")

        # Verify legacy names are retired from module
        import aikito.subagent as subagent_mod

        self.assertFalse(hasattr(subagent_mod, "PlanItem"))
        self.assertFalse(hasattr(subagent_mod, "build_plan"))

    def test_doctor_subagent_mcp_characterization(self) -> None:
        """Freeze doctor execution over configured workspace report sections."""
        mcp_toml = """
[mcp]
command = "npx"
args = ["-y", "@modelcontextprotocol/server-everything"]
"""
        (self.ws / "mcps" / "everything.toml").write_text(mcp_toml, encoding="utf-8")

        report = run_doctor(self.ws, home=self.home)
        section_names = {s.name for s in report.sections}
        self.assertIn("Drift", section_names)
        self.assertIn("Configuration", section_names)
        self.assertIn("Symlinks", section_names)

    def test_adopt_plan_and_execution_characterization(self) -> None:
        """Freeze baseline adopt plan, findings, summary, and execution."""
        # Create unmanaged native claude subagent in home
        claude_agents_dir = self.home / ".claude" / "agents"
        claude_agents_dir.mkdir(parents=True)
        (claude_agents_dir / "tester.md").write_text(
            "---\ndescription: Native tester\n---\nTest instructions\n",
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.ws, home=self.home)
        # Baseline AdoptPlan uses has_conflicts rather than can_apply
        self.assertFalse(plan.has_conflicts)
        findings = collect_adopt_findings(plan)
        self.assertIsInstance(findings, (list, tuple))
        summary = summarize_adopt_plan(plan)
        self.assertEqual(summary.subagent_imports, 1)
        self.assertEqual(summary.total_changes, 1)

        # Execution creates backup and writes to workspace
        backup_dir = self.home / ".aikito" / "backups"
        success = execute_adoption(plan, dry_run=False)
        self.assertTrue(success)
        self.assertTrue((self.ws / "subagents" / "tester.md").exists())
        self.assertTrue(backup_dir.exists())
        backup_subdirs = list(backup_dir.glob("adopt_*"))
        self.assertGreater(len(backup_subdirs), 0)

    def test_public_api_export_boundary_characterization(self) -> None:
        """Verify public __all__ in aikito package including Workspace exports."""
        expected_exports = {
            "AmbiguousProjectPathError",
            "InvalidProjectConfigError",
            "InvalidWorkspaceError",
            "NoAvailableProjectPathError",
            "PreparedProject",
            "Project",
            "ProjectError",
            "ProjectNotFoundError",
            "ProjectPrepareConflictError",
            "UnsupportedProjectAgentError",
            "Workspace",
            "WorkspaceError",
            "WorkspaceFinding",
            "WorkspaceInspection",
            "WorkspaceNotFoundError",
            "InvalidWorkspaceError",
            "WorkspaceProjectView",
            "WorkspaceSyncPreview",
            "__version__",
        }
        self.assertEqual(set(aikito.__all__), expected_exports)
        self.assertTrue(hasattr(aikito, "Workspace"))

    def test_workspace_public_api_load_inspect_plan_sync(self) -> None:
        from aikito import (
            InvalidWorkspaceError,
            Workspace,
            WorkspaceFinding,
            WorkspaceInspection,
            WorkspaceNotFoundError,
            WorkspaceProjectView,
            WorkspaceSyncPreview,
        )

        # 1. Invalid relative path raises InvalidWorkspaceError
        with self.assertRaises(InvalidWorkspaceError):
            Workspace.load("relative/path")

        # 2. Non-existent path raises WorkspaceNotFoundError
        with self.assertRaises(WorkspaceNotFoundError):
            Workspace.load(self.home / "nonexistent", home=self.home)

        # 3. Valid load
        ws = Workspace.load(self.ws, home=self.home)
        self.assertEqual(ws.path, self.ws)
        self.assertEqual(ws.home, self.home)

        # 4. Inspect
        inspection = ws.inspect()
        self.assertIsInstance(inspection, WorkspaceInspection)
        self.assertEqual(inspection.workspace_dir, self.ws)
        self.assertIsInstance(inspection.configured_agents, tuple)
        self.assertIsInstance(inspection.projects, tuple)
        self.assertIsInstance(inspection.diagnostics, tuple)
        self.assertIsInstance(inspection.ready_for_sync, bool)
        for p in inspection.projects:
            self.assertIsInstance(p, WorkspaceProjectView)
        for d in inspection.diagnostics:
            self.assertIsInstance(d, WorkspaceFinding)

        # 5. Plan sync preview
        preview = ws.plan_sync()
        self.assertIsInstance(preview, WorkspaceSyncPreview)
        self.assertEqual(preview.workspace_path, self.ws)
        self.assertFalse(hasattr(preview, "plan"))
        self.assertIsInstance(preview.changes, int)
        self.assertIsInstance(preview.unchanged, int)
        self.assertIsInstance(preview.offline, int)
        self.assertIsInstance(preview.warnings, int)
        self.assertIsInstance(preview.conflicts, int)
        self.assertIsInstance(preview.errors, int)
        self.assertIsInstance(preview.can_apply, bool)
        self.assertIsInstance(preview.will_mutate, bool)
        self.assertIsInstance(preview.findings, tuple)
        for f in preview.findings:
            self.assertIsInstance(f, WorkspaceFinding)
        self.assertIsInstance(preview.operations, tuple)

    def test_web_console_read_only_and_redaction_characterization(self) -> None:
        """Freeze ConsoleData read-only collection and credential redaction difference."""
        secret_entry = {
            "command": "node",
            "env": {"API_KEY": "supersecret123"},
            "headers": {"Authorization": "Bearer token456"},
        }
        # MCP uses '<redacted>'
        mcp_redacted = redact_mcp_entry(secret_entry)
        self.assertEqual(mcp_redacted["env"]["API_KEY"], "<redacted>")
        self.assertEqual(mcp_redacted["headers"]["Authorization"], "<redacted>")

        # Web console now also uses '<redacted>'
        web_redacted = _redact(secret_entry)
        self.assertEqual(web_redacted["env"]["API_KEY"], "<redacted>")
        self.assertEqual(web_redacted["headers"]["Authorization"], "<redacted>")

        console_data = ConsoleData(self.ws, self.home, version="1.48.0")
        overview = console_data.overview()
        self.assertIn("workspace", overview)
        self.assertIn("healthy", overview)
        self.assertIn("counts", overview)
        self.assertEqual(overview["counts"]["mcps"], 0)
