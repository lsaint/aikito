from layout_helpers import write_agents
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.workspace import (
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)

ROOT = Path(__file__).resolve().parents[1]


class AikitoWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_workspace(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_workspace(self.home), (self.home / "aikito").resolve()
            )
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "default")

    def test_persisted_workspace(self) -> None:
        workspace = self.home / "custom-workspace"
        with patch.dict(os.environ, {}, clear=True):
            persist_workspace(workspace, self.home)
            self.assertEqual(resolve_workspace(self.home), workspace.resolve())
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "configured")

    def test_environment_overrides_persisted_workspace(self) -> None:
        persisted = self.home / "persisted"
        environment = self.home / "environment"
        with patch.dict(os.environ, {}, clear=True):
            persist_workspace(persisted, self.home)
        with patch.dict(os.environ, {"AIKITO_DIR": str(environment)}, clear=True):
            self.assertEqual(resolve_workspace(self.home), environment.resolve())
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "AIKITO_DIR")

    def test_workspace_plan_sync_operations_include_global_skills_and_instructions(
        self,
    ) -> None:
        """INV-API-11 / Public API: plan_sync operations must include sanitized global skill and instruction summaries."""
        from aikito import (
            Workspace,
            WorkspaceFinding,
            WorkspaceInspection,
            WorkspaceOperationView,
            WorkspaceProjectView,
            WorkspaceSyncPreview,
        )

        ws_dir = self.home / "my-workspace"
        ws_dir.mkdir(parents=True, exist_ok=True)
        (ws_dir / "config.toml").write_text(
            '[workspace]\nversion = "1.0"\n', encoding="utf-8"
        )
        (ws_dir / "skills.toml").write_text('skills = ["my-skill"]\n', encoding="utf-8")
        write_agents(
            ws_dir,
            "[agents.claude-code]\n"
            'display_name = "Claude Code"\n'
            'instruction_path = ".claude/CLAUDE.md"\n'
            'skills_path = ".claude/skills"\n',
        )
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        (ws_dir / "subagents").mkdir(exist_ok=True)
        (ws_dir / "mcps").mkdir(parents=True, exist_ok=True)

        # Create global instructions source
        global_dir = ws_dir / "global"
        global_dir.mkdir(parents=True, exist_ok=True)
        (global_dir / "AGENTS.md").write_text("# Global Rules\n", encoding="utf-8")

        # Create global skill source
        skill_dir = ws_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text(
            "---\nname: my-skill\n---\n", encoding="utf-8"
        )

        # Also add a project
        proj_dir = ws_dir / "projects" / "test-proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        checkout = self.home / "checkouts" / "test-proj"
        checkout.mkdir(parents=True, exist_ok=True)
        (proj_dir / "agent.toml").write_text(
            f'path = "{checkout.as_posix()}"\nskills = []\n', encoding="utf-8"
        )

        ws = Workspace.load(ws_dir, home=self.home)

        # 1. Inspection returns sanitized public views without internal types
        inspection = ws.inspect()
        self.assertIsInstance(inspection, WorkspaceInspection)
        self.assertEqual(len(inspection.projects), 1)
        self.assertIsInstance(inspection.projects[0], WorkspaceProjectView)
        self.assertEqual(inspection.projects[0].name, "test-proj")
        for d in inspection.diagnostics:
            self.assertIsInstance(d, WorkspaceFinding)

        # 2. Plan sync preview captures global skill and instruction CREATE operations
        preview = ws.plan_sync()
        self.assertIsInstance(preview, WorkspaceSyncPreview)
        self.assertFalse(hasattr(preview, "plan"))
        self.assertGreater(preview.changes, 0)

        for f in preview.findings:
            self.assertIsInstance(f, WorkspaceFinding)

        ops = preview.operations
        self.assertTrue(
            any("Global Skill CREATE" in op and "my-skill" in op for op in ops),
            f"Expected Global Skill CREATE for my-skill in operations, got: {ops}",
        )
        self.assertTrue(
            any("Global Instructions CREATE" in op for op in ops),
            f"Expected Global Instructions CREATE in operations, got: {ops}",
        )

        # 3. P2-A: Structured operation views projection
        self.assertIsInstance(preview.operation_views, tuple)
        self.assertGreater(len(preview.operation_views), 0)
        for view in preview.operation_views:
            self.assertIsInstance(view, WorkspaceOperationView)
            self.assertIsInstance(view.resource_type, str)
            self.assertIsInstance(view.effect, str)
            # Ensure model is frozen / immutable
            with self.assertRaises((AttributeError, TypeError)):
                view.effect = "mutated"  # type: ignore

        # Verify structured attributes for global skill create
        skill_ops = [
            v
            for v in preview.operation_views
            if v.resource_type == "global_skill" and v.resource_name == "my-skill"
        ]
        self.assertEqual(len(skill_ops), 1)
        self.assertEqual(skill_ops[0].effect, "create")
        self.assertEqual(skill_ops[0].scope, "global")
        self.assertEqual(skill_ops[0].domain_action, "CREATE")
        self.assertTrue(skill_ops[0].authorized)

        # Verify structured attributes for instruction create
        inst_ops = [
            v for v in preview.operation_views if v.resource_type == "instruction"
        ]
        self.assertGreater(len(inst_ops), 0)
        self.assertTrue(all(v.effect in ("create", "noop", "none") for v in inst_ops))

        # Verify project structured view
        project_ops = [
            v for v in preview.operation_views if v.resource_type == "project"
        ]
        self.assertEqual(len(project_ops), 1)
        self.assertEqual(project_ops[0].project, "test-proj")
        self.assertEqual(project_ops[0].effect, "noop")


if __name__ == "__main__":
    unittest.main()
