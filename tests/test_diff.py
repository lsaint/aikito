import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import cli
from aikito.config_runtime import ConfigOperation, ConfigTarget
from aikito.diff import (
    collect_drift_diffs,
    filter_drift_diffs,
    render_drift_diffs,
    render_drift_index,
    render_project_drift_index,
)
from aikito.diff_model import DriftDiff
from aikito.mcp import AgentSpec
from aikito.subagent import SubagentPlan


class DriftDiffTest(unittest.TestCase):
    def test_reports_copied_project_skill_text_and_binary_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory).resolve()
            project = root / "project"
            canonical = root / "skills" / "example"
            runtime = project / ".agents" / "skills" / "example"
            project_config = root / "projects" / "demo"
            canonical.mkdir(parents=True)
            runtime.mkdir(parents=True)
            project_config.mkdir(parents=True)
            (project_config / "agent.toml").write_text(
                f'path = "{project.as_posix()}"\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )

            (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (runtime / "SKILL.md").write_text("runtime\n", encoding="utf-8")
            (canonical / "asset.bin").write_bytes(b"\0canonical")
            (runtime / "asset.bin").write_bytes(b"\0runtime")

            with (
                patch("aikito.diff.load_agent_specs", return_value=[]),
                patch(
                    "aikito.diff.build_subagent_plan",
                    return_value=SubagentPlan(operations=(), file_plans=()),
                ),
            ):
                diffs = collect_drift_diffs(root, root)
                rendered = render_drift_diffs(diffs)

        self.assertEqual(len(diffs), 2)
        text_diff = next(d for d in diffs if d.file == "SKILL.md")
        self.assertEqual(text_diff.kind, "project_skill")
        self.assertEqual(text_diff.project, "demo")
        self.assertEqual(text_diff.name, "example")
        self.assertEqual(text_diff.file, "SKILL.md")

        self.assertIn(f"[Project demo/skill example — SKILL.md ({project})]", rendered)
        self.assertIn("-runtime", rendered)
        self.assertIn("+canonical", rendered)
        self.assertIn(f"[Project demo/skill example — asset.bin ({project})]", rendered)
        self.assertIn("Binary files differ", rendered)

    def test_project_drilldown_ignores_broken_global_config_and_keeps_checkouts(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            canonical = root / "skills" / "example" / "scripts"
            canonical.mkdir(parents=True)
            (canonical.parent / "SKILL.md").write_text("# Example\n", encoding="utf-8")
            (canonical / "check.py").write_text("canonical\n", encoding="utf-8")
            checkouts = [root / "first", root / "second"]
            for checkout in checkouts:
                runtime = checkout / ".agents" / "skills" / "example"
                (runtime / "scripts").mkdir(parents=True)
                (runtime / "SKILL.md").write_text("# Example\n", encoding="utf-8")
                (runtime / "scripts" / "check.py").write_text(
                    "local\n", encoding="utf-8"
                )
            config = root / "projects" / "demo" / "agent.toml"
            config.parent.mkdir(parents=True)
            paths = ", ".join(f'"{path.as_posix()}"' for path in checkouts)
            config.write_text(
                f'paths = [{paths}]\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )
            (root / "mcps").mkdir()
            (root / "mcps" / "broken.toml").write_text("invalid = [", encoding="utf-8")
            (root / "subagents.toml").write_text("invalid = [", encoding="utf-8")

            diffs = collect_drift_diffs(
                root, root, kind="project_skill", project_filter="demo"
            )
            self.assertEqual({d.checkout for d in diffs}, {str(p) for p in checkouts})
            index = render_drift_index(diffs)
            project_index = render_project_drift_index("demo", diffs)
            for checkout in checkouts:
                self.assertIn(f"Checkout: {checkout}", index)
                self.assertIn(f"Checkout: {checkout}", project_index)
            self.assertEqual(index.count("1 file changed"), 2)
            self.assertNotIn("2 files changed", index)

            parser = cli.build_parser()
            for relative in (
                "scripts/check.py",
                "./scripts/check.py",
                r"scripts\check.py",
                "scripts/../scripts/check.py",
            ):
                with self.subTest(relative=relative):
                    output = io.StringIO()
                    args = parser.parse_args(
                        ["diff", "project", "demo", "example", relative]
                    )
                    with (
                        patch.object(cli, "get_aikito_dir", return_value=root),
                        patch.object(cli.Path, "home", return_value=root),
                        patch("sys.stdout", output),
                    ):
                        args.func(args)
                    self.assertEqual(output.getvalue().count("-local"), 2)
                    for checkout in checkouts:
                        self.assertIn(f"({checkout})]", output.getvalue())

    def test_resource_scope_does_not_read_other_resource_kinds(self) -> None:
        with (
            patch("aikito.diff.load_agent_specs", return_value=[]) as load_mcp,
            patch("aikito.diff.build_mcp_plan"),
            patch(
                "aikito.diff.build_subagent_plan",
                return_value=SubagentPlan(operations=(), file_plans=()),
            ) as load_subagents,
            patch(
                "aikito.diff.collect_project_skill_diffs", return_value=[]
            ) as load_projects,
        ):
            root = Path("unused")
            collect_drift_diffs(root, root, kind="subagent")
            load_subagents.assert_called_once()
            load_mcp.assert_not_called()
            load_projects.assert_not_called()
            load_subagents.reset_mock()
            collect_drift_diffs(root, root, kind="mcp")
            load_mcp.assert_called_once()
            load_subagents.assert_not_called()
            load_projects.assert_not_called()

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
            target = ConfigTarget(
                path=subagent_path,
                logical_identity="formatter",
                agent="test-agent",
            )
            op = ConfigOperation(
                target=target,
                action="UPDATE",
                reason="Content changed",
                rendered_payload="new\n",
            )
            subagent_plan = SubagentPlan(operations=(op,), file_plans=())

            with (
                patch("aikito.diff.load_agent_specs", return_value=[spec]),
                patch("aikito.diff.evaluate_spec_status", return_value="DRIFT"),
                patch(
                    "aikito.diff.read_entry",
                    return_value={
                        "headers": {"Authorization": "old-secret"},
                        "url": "old",
                    },
                ),
                patch("aikito.diff.build_subagent_plan", return_value=subagent_plan),
            ):
                diffs = collect_drift_diffs(root, root)
                rendered = render_drift_diffs(diffs)

        self.assertEqual(len(diffs), 2)
        mcp_diff = next(d for d in diffs if d.kind == "mcp")
        self.assertEqual(mcp_diff.agent, "test-agent")
        self.assertEqual(mcp_diff.name, "example")

        subagent_diff = next(d for d in diffs if d.kind == "subagent")
        self.assertEqual(subagent_diff.agent, "test-agent")
        self.assertEqual(subagent_diff.name, "formatter")

        self.assertIn("[MCP test-agent/example]", rendered)
        self.assertIn("[Subagent test-agent/formatter]", rendered)
        self.assertIn('"url": "old"', rendered)
        self.assertIn('"url": "new"', rendered)
        self.assertIn("<redacted>", rendered)
        self.assertNotIn("old-secret", rendered)
        self.assertNotIn("new-secret", rendered)

    def test_no_drift_messages(self) -> None:
        self.assertEqual(render_drift_diffs([]), "No drift detected.")
        self.assertEqual(render_drift_index([]), "No drift detected.")
        self.assertEqual(
            render_project_drift_index("demo", []), "No matching drift detected."
        )

    def test_render_drift_index_grouped(self) -> None:
        diffs = [
            DriftDiff(kind="mcp", name="github", agent="codex", diff="mcp-diff"),
            DriftDiff(
                kind="subagent", name="reviewer", agent="claude", diff="sub-diff"
            ),
            DriftDiff(
                kind="project_skill",
                name="formatter",
                project="demo",
                file="SKILL.md",
                diff="f-diff1",
            ),
            DriftDiff(
                kind="project_skill",
                name="formatter",
                project="demo",
                file="scripts/check.py",
                diff="f-diff2",
            ),
            DriftDiff(
                kind="project_skill",
                name="python",
                project="demo",
                file="SKILL.md",
                diff="p-diff",
            ),
            DriftDiff(
                kind="project_skill",
                name="testing",
                project="backend",
                file="SKILL.md",
                diff="t-diff",
            ),
        ]
        index = render_drift_index(diffs)
        self.assertIn("Drift detected:", index)
        self.assertIn("MCP\n  codex/github", index)
        self.assertIn("Subagents\n  claude/reviewer", index)
        self.assertIn("Projects", index)
        self.assertIn("demo", index)
        self.assertIn("formatter      2 files changed", index)
        self.assertIn("python         1 file changed", index)
        self.assertIn("backend", index)
        self.assertIn("testing        1 file changed", index)
        self.assertIn("aikito diff project <project> <skill>", index)
        self.assertIn("aikito diff mcp <agent> <server>", index)
        self.assertIn("aikito diff subagent <agent> <name>", index)
        # Should not contain raw diff content
        self.assertNotIn("mcp-diff", index)
        self.assertNotIn("f-diff1", index)

    def test_render_project_drift_index(self) -> None:
        diffs = [
            DriftDiff(
                kind="project_skill",
                name="formatter",
                project="demo",
                file="SKILL.md",
                diff="diff1",
            ),
            DriftDiff(
                kind="project_skill",
                name="formatter",
                project="demo",
                file="references/python.md",
                diff="diff2",
            ),
            DriftDiff(
                kind="project_skill",
                name="testing",
                project="backend",
                file="SKILL.md",
                diff="diff3",
            ),
        ]
        proj_index = render_project_drift_index("demo", diffs)
        self.assertIn("Project demo", proj_index)
        self.assertIn("formatter", proj_index)
        self.assertIn("  SKILL.md", proj_index)
        self.assertIn("  references/python.md", proj_index)
        self.assertIn("Review details:\n  aikito diff project demo <skill>", proj_index)
        # Should not contain other projects or diff content
        self.assertNotIn("backend", proj_index)
        self.assertNotIn("diff1", proj_index)

    def test_filter_drift_diffs(self) -> None:
        d1 = DriftDiff(
            kind="project_skill",
            name="formatter",
            project="demo",
            file="SKILL.md",
            diff="d1",
        )
        d2 = DriftDiff(
            kind="project_skill",
            name="formatter",
            project="demo",
            file="check.py",
            diff="d2",
        )
        d3 = DriftDiff(
            kind="project_skill",
            name="python",
            project="demo",
            file="SKILL.md",
            diff="d3",
        )
        d4 = DriftDiff(kind="mcp", name="github", agent="codex", diff="d4")
        all_diffs = [d1, d2, d3, d4]

        # Filter by project & skill
        self.assertEqual(
            filter_drift_diffs(
                all_diffs, kind="project_skill", project="demo", name="formatter"
            ),
            [d1, d2],
        )
        # Filter by file
        self.assertEqual(
            filter_drift_diffs(
                all_diffs,
                kind="project_skill",
                project="demo",
                name="formatter",
                file="SKILL.md",
            ),
            [d1],
        )
        # Filter by mcp
        self.assertEqual(
            filter_drift_diffs(all_diffs, kind="mcp", agent="codex", name="github"),
            [d4],
        )

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
                patch("aikito.diff.load_agent_specs", return_value=[spec]),
                patch("aikito.diff.evaluate_spec_status", return_value="DRIFT"),
                patch(
                    "aikito.diff.read_entry",
                    return_value={"headers": {"Authorization": "old-secret"}},
                ),
                patch(
                    "aikito.diff.build_subagent_plan",
                    return_value=SubagentPlan(operations=(), file_plans=()),
                ),
            ):
                rendered = render_drift_diffs(collect_drift_diffs(root, root))

        self.assertIn("[MCP agy/atlassian-rovo]", rendered)
        self.assertIn("-<redacted value differs>", rendered)
        self.assertIn("+<expected redacted value>", rendered)
        self.assertNotIn("old-secret", rendered)
        self.assertNotIn("new-secret", rendered)

    def test_offline_copy_project_produces_no_diff(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical = root / "skills" / "example"
            canonical.mkdir(parents=True)
            (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")

            project_config = root / "projects" / "offline_p1"
            project_config.mkdir(parents=True)
            (project_config / "agent.toml").write_text(
                'path = "D:/nonexistent/p1"\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )

            with (
                patch("aikito.diff.load_agent_specs", return_value=[]),
                patch(
                    "aikito.diff.build_subagent_plan",
                    return_value=SubagentPlan(operations=(), file_plans=()),
                ),
            ):
                diffs = collect_drift_diffs(root, root)
                rendered = render_drift_diffs(diffs)

            self.assertEqual(diffs, [])
            self.assertEqual(rendered, "No drift detected.")

    def test_collect_drift_diffs_with_project_filter(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            canonical = root / "skills" / "example"
            canonical.mkdir(parents=True)
            (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")

            # Project 1
            p1_path = root / "p1"
            p1_runtime = p1_path / ".agents" / "skills" / "example"
            p1_runtime.mkdir(parents=True)
            (p1_runtime / "SKILL.md").write_text("runtime1\n", encoding="utf-8")
            p1_config = root / "projects" / "p1"
            p1_config.mkdir(parents=True)
            (p1_config / "agent.toml").write_text(
                f'path = "{p1_path.as_posix()}"\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )

            # Project 2
            p2_path = root / "p2"
            p2_runtime = p2_path / ".agents" / "skills" / "example"
            p2_runtime.mkdir(parents=True)
            (p2_runtime / "SKILL.md").write_text("runtime2\n", encoding="utf-8")
            p2_config = root / "projects" / "p2"
            p2_config.mkdir(parents=True)
            (p2_config / "agent.toml").write_text(
                f'path = "{p2_path.as_posix()}"\nsync_mode = "copy"\nskills = ["example"]\n',
                encoding="utf-8",
            )

            with (
                patch("aikito.diff.load_agent_specs", return_value=[]),
                patch(
                    "aikito.diff.build_subagent_plan",
                    return_value=SubagentPlan(operations=(), file_plans=()),
                ),
            ):
                all_diffs = collect_drift_diffs(root, root, project_filter=None)
                p1_diffs = collect_drift_diffs(root, root, project_filter="p1")
                p2_diffs = collect_drift_diffs(root, root, project_filter="p2")

            self.assertEqual(len(all_diffs), 2)
            self.assertEqual(len(p1_diffs), 1)
            self.assertIn("Project p1/skill example", p1_diffs[0].display_label)
            self.assertEqual(len(p2_diffs), 1)
            self.assertIn("Project p2/skill example", p2_diffs[0].display_label)
