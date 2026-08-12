import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_diff import collect_drift_diffs, render_drift_diffs  # noqa: E402
from aikito_mcp import AgentSpec  # noqa: E402
from aikito_subagent import PlanItem  # noqa: E402


class DriftDiffTest(unittest.TestCase):
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
