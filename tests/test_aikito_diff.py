import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito_diff import collect_drift_diffs, render_drift_diffs
from aikito_mcp import AgentSpec
from aikito_subagent import PlanItem


class DriftDiffTest(unittest.TestCase):
    def test_reports_copied_project_skill_text_and_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            canonical = root / "skills" / "example"
            runtime = project / ".agents" / "skills" / "example"
            project_config = root / "projects" / "demo"
            canonical.mkdir(parents=True)
            runtime.mkdir(parents=True)
            project_config.mkdir(parents=True)
            (project_config / "agent.toml").write_text(
                f'path = "{project}"\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )
            (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (runtime / "SKILL.md").write_text("runtime\n", encoding="utf-8")
            (canonical / "asset.bin").write_bytes(b"\0canonical")
            (runtime / "asset.bin").write_bytes(b"\0runtime")

            with (
                patch("aikito_diff.load_agent_specs", return_value=[]),
                patch("aikito_diff.build_plan", return_value=([], {})),
            ):
                rendered = render_drift_diffs(collect_drift_diffs(root, root))

        self.assertIn("[Project demo/skill example — SKILL.md]", rendered)
        self.assertIn("-runtime", rendered)
        self.assertIn("+canonical", rendered)
        self.assertIn("[Project demo/skill example — asset.bin]", rendered)
        self.assertIn("Binary files differ", rendered)

    def test_reports_redacted_mcp_and_subagent_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mcp_path = root / "config.json"
            mcp_path.write_text("{}", encoding="utf-8")
            subagent_path = root / "formatter.md"
            subagent_path.write_text("old\n", encoding="utf-8")
            spec = AgentSpec(
                agent="test-agent",
                server="example",
                config_path=mcp_path,
                config_format="jsonc",
                target_name="example",
                desired={"headers": {"Authorization": "new-secret"}, "url": "new"},
            )
            item = PlanItem(
                agent_name="test-agent",
                subagent_name="formatter",
                target_path=subagent_path,
                action="UPDATE",
                reason="Content changed",
                rendered_content="new\n",
            )

            with (
                patch("aikito_diff.load_agent_specs", return_value=[spec]),
                patch("aikito_diff.evaluate_spec_status", return_value="DRIFT"),
                patch(
                    "aikito_diff.read_entry",
                    return_value={
                        "headers": {"Authorization": "old-secret"},
                        "url": "old",
                    },
                ),
                patch("aikito_diff.build_plan", return_value=([item], {})),
            ):
                rendered = render_drift_diffs(collect_drift_diffs(root, root))

        self.assertIn("[MCP test-agent/example]", rendered)
        self.assertIn("[Subagent test-agent/formatter]", rendered)
        self.assertIn('"url": "old"', rendered)
        self.assertIn('"url": "new"', rendered)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn("old-secret", rendered)
        self.assertNotIn("new-secret", rendered)

    def test_no_drift_message(self) -> None:
        self.assertEqual(render_drift_diffs([]), "No drift detected.")

    def test_reports_drift_hidden_entirely_by_redaction(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            mcp_path = root / "config.json"
            mcp_path.write_text("{}", encoding="utf-8")
            spec = AgentSpec(
                agent="agy",
                server="atlassian-rovo",
                config_path=mcp_path,
                config_format="agy_json",
                target_name="atlassian-rovo",
                desired={"headers": {"Authorization": "new-secret"}},
            )

            with (
                patch("aikito_diff.load_agent_specs", return_value=[spec]),
                patch("aikito_diff.evaluate_spec_status", return_value="DRIFT"),
                patch(
                    "aikito_diff.read_entry",
                    return_value={"headers": {"Authorization": "old-secret"}},
                ),
                patch("aikito_diff.build_plan", return_value=([], {})),
            ):
                rendered = render_drift_diffs(collect_drift_diffs(root, root))

        self.assertIn("[MCP agy/atlassian-rovo]", rendered)
        self.assertIn("-<redacted value differs>", rendered)
        self.assertIn("+<expected redacted value>", rendered)
        self.assertNotIn("old-secret", rendered)
        self.assertNotIn("new-secret", rendered)
