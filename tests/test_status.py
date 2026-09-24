import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from aikito.init import init_workspace
from aikito.inspection import InspectionStatus
from aikito.mcp import MCPToolProbeResult
from aikito.project import (
    ProjectSummary,
    evaluate_project_health,
    format_project_path_counts,
)
from aikito.render import (
    AgentStatusRow,
    GlobalSummary,
    MCPServerRow,
    MemoryNoteRow,
    MemoryStatusRow,
    OrphanSubagentFile,
    SkillRow,
    StatusReportData,
    SubagentRow,
    render_agent_mcp_table,
    render_agent_subagent_table,
    render_key_value_fields,
    render_legend,
    render_mcp_runtime_table,
    render_mcp_status_table,
    render_memory_notes_table,
    render_projects_table,
    render_skills_table,
    render_status_report,
    render_subagents_status_table,
)
from aikito.status import (
    MCPDetailRow,
    MCPRuntimeRow,
    SubagentDetailRow,
    _summarize_subagent_status,
    collect_agent_status_rows,
    collect_mcp_details,
    collect_mcp_matrix,
    collect_mcp_runtime,
    collect_memory_notes_rows,
    collect_skills_rows,
    collect_subagents_matrix,
    get_status_report_data,
)

ROOT = Path(__file__).resolve().parents[1]


class AikitoStatusRenderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_rows = [
            AgentStatusRow(
                agent_name="claude-code",
                display_name="Claude Code",
                instructions_status="OK",
                skills_status="OK (11)",
                mcp_status="OK (1)",
                subagent_status="OK (1)",
            ),
            AgentStatusRow(
                agent_name="codex",
                display_name="Codex",
                instructions_status="OK",
                skills_status="SKIP",
                mcp_status="CONFLICT (1)",
                subagent_status="SKIP",
            ),
        ]
        self.memory_rows = [
            MemoryStatusRow(
                name="Global",
                scope="Global",
                status="OK",
                notes_count=6,
                updated_on=date.today(),
            )
        ]

    def test_render_with_issues_shows_legend(self) -> None:
        conflict_rows = [
            AgentStatusRow(
                agent_name="claude-code",
                display_name="Claude Code",
                instructions_status="MISSING",
                skills_status="OK (11)",
                mcp_status="OK (1)",
                subagent_status="OK (1)",
            ),
            AgentStatusRow(
                agent_name="codex",
                display_name="Codex",
                instructions_status="OK",
                skills_status="SKIP",
                mcp_status="CONFLICT (3/4)",
                subagent_status="SKIP",
            ),
        ]
        report_data = StatusReportData(
            agents=conflict_rows,
            memories=self.memory_rows,
            total_subagents_count=1,
            total_mcp_count=2,
            total_skills_count=11,
            total_memory_notes=27,
            issues_count=2,
        )
        rendered = render_status_report(report_data, is_tty=True, no_color=True)
        self.assertIn("Project", rendered)
        self.assertIn("Status", rendered)
        self.assertIn("Global: ! 2 issues", rendered)
        self.assertIn("Consumers (2): claude · codex", rendered)

    def test_render_all_synced_hides_legend(self) -> None:
        clean_rows = [
            AgentStatusRow(
                agent_name="claude-code",
                display_name="Claude Code",
                instructions_status="OK",
                skills_status="OK (11)",
                mcp_status="OK (1)",
                subagent_status="OK (1)",
            )
        ]
        report_data = StatusReportData(
            agents=clean_rows,
            memories=self.memory_rows,
            total_subagents_count=1,
            total_mcp_count=1,
            total_skills_count=11,
            total_memory_notes=27,
            issues_count=0,
        )
        rendered = render_status_report(report_data, is_tty=True, no_color=True)
        self.assertIn("Project", rendered)
        self.assertIn("Status", rendered)
        self.assertIn("Global: ✓", rendered)
        self.assertIn("Consumers (1): claude", rendered)

    def test_render_workspace_header_is_bold_in_color_output(self) -> None:
        report_data = StatusReportData(
            agents=self.agent_rows,
            memories=self.memory_rows,
        )
        rendered = render_status_report(
            report_data,
            is_tty=True,
            workspace="/tmp/aikito",
            workspace_source="configured",
        )
        self.assertIn(
            "\033[1mAll in one workspace: /tmp/aikito\033[0m (configured)", rendered
        )

    def test_subagent_status_distinguishes_missing_drift_and_conflict(self) -> None:
        self.assertEqual(
            _summarize_subagent_status([InspectionStatus.MISSING]), "MISSING (0/1)"
        )

    def test_count_badges_render_without_checkmark(self) -> None:
        from aikito.render import render_agents_table

        rows = [
            AgentStatusRow(
                agent_name="codex",
                display_name="Codex",
                instructions_status="OK",
                skills_status="SKIP",
                mcp_status="OK (3)",
                subagent_status="OK (2)",
            ),
            AgentStatusRow(
                agent_name="grok",
                display_name="Grok Build",
                instructions_status="OK",
                skills_status="SKIP",
                mcp_status="OK (0)",
                subagent_status="OK (0)",
            ),
            AgentStatusRow(
                agent_name="pi",
                display_name="Pi",
                instructions_status="OK",
                skills_status="SKIP",
                mcp_status="SKIP",
                subagent_status="SKIP",
            ),
        ]

        rendered_unicode = render_agents_table(rows, use_unicode=True, use_color=False)
        self.assertIn("│ 3 ", rendered_unicode)
        self.assertIn("│ 2 ", rendered_unicode)
        self.assertIn("│ 0 ", rendered_unicode)
        self.assertIn("│ – ", rendered_unicode)
        self.assertNotIn("✓ 3", rendered_unicode)
        self.assertNotIn("✓ 0", rendered_unicode)

        rendered_ascii = render_agents_table(rows, use_unicode=False, use_color=False)
        self.assertIn("| 3 ", rendered_ascii)
        self.assertNotIn("v 3", rendered_ascii)
        self.assertEqual(
            _summarize_subagent_status([InspectionStatus.UPDATE]), "DRIFT (0/1)"
        )
        self.assertEqual(
            _summarize_subagent_status([InspectionStatus.CONFLICT]), "CONFLICT (0/1)"
        )

    def test_render_mcp_status_table(self) -> None:
        server_rows = [
            MCPServerRow(
                server_name="atlassian-rovo",
                agent_statuses={"Claude Code": "OK", "Codex": "SKIP"},
            )
        ]
        agent_names = ["Claude Code", "Codex"]
        rendered = render_mcp_status_table(
            server_rows, agent_names, use_unicode=True, use_color=False
        )
        self.assertIn("MCP Server", rendered)
        self.assertIn("atlassian-rovo", rendered)
        self.assertIn("✓", rendered)
        self.assertIn("–", rendered)

    def test_render_mcp_runtime_table(self) -> None:
        rows = [
            MCPRuntimeRow(
                agent_name="codex",
                agent_display_name="Codex",
                connect_status="OK",
                auth_method="Basic · env header",
                tool_names=("one", "two", "three"),
            )
        ]
        rendered = render_mcp_runtime_table(rows, use_unicode=True, use_color=False)
        self.assertIn("Connect", rendered)
        self.assertIn("Auth method", rendered)
        self.assertIn("✓", rendered)
        self.assertIn("Basic · env header", rendered)
        self.assertIn("3", rendered)

    def test_render_subagents_status_table(self) -> None:
        subagent_rows = [
            SubagentRow(
                subagent_name="formatter",
                agent_statuses={"Claude Code": "OK", "Codex": "MISSING"},
            )
        ]
        orphan_files = [
            OrphanSubagentFile(
                agent_display_name="Claude Code", file_path="~/.claude/agents/old.md"
            )
        ]
        agent_names = ["Claude Code", "Codex"]
        rendered = render_subagents_status_table(
            subagent_rows, orphan_files, agent_names, use_unicode=True, use_color=False
        )
        self.assertIn("Subagent", rendered)
        self.assertIn("formatter", rendered)
        self.assertIn("Orphan Subagent Files", rendered)
        self.assertIn("old.md", rendered)

    def test_get_display_width(self) -> None:
        from aikito.render import _get_display_width

        self.assertEqual(_get_display_width("abc"), 3)
        self.assertEqual(_get_display_width("中文"), 4)
        self.assertEqual(_get_display_width("agy 1.1.8 的"), 12)

    def test_render_key_value_fields_aligns_labels_without_borders(self) -> None:
        rendered = render_key_value_fields(
            [("Name:", "demo"), ("Canonical source:", "/tmp/demo")]
        )

        self.assertEqual(
            rendered,
            "            Name:  demo\nCanonical source:  /tmp/demo",
        )
        self.assertNotIn("|", rendered)
        self.assertNotIn("+", rendered)

    def test_truncate_display_text(self) -> None:
        from aikito.render import _truncate_display_text

        self.assertEqual(_truncate_display_text("hello world", 8), "hello w…")
        self.assertEqual(_truncate_display_text("中文测试标题", 7), "中文测…")

    def test_render_memory_notes_table(self) -> None:
        notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                link_status="SKIP",
            ),
            MemoryNoteRow(
                scope_name="aikito",
                note_name="unindexed",
                title="-",
                link_status="OK",
            ),
        ]
        rendered = render_memory_notes_table(notes, use_unicode=True, use_color=False)
        self.assertIn("Global", rendered)
        self.assertIn("demo", rendered)
        self.assertIn("Demo Title", rendered)
        self.assertIn("–", rendered)
        self.assertIn("✓", rendered)

    def test_render_memory_notes_table_balances_note_and_title_widths(self) -> None:
        notes = [
            MemoryNoteRow(
                scope_name="aikito",
                note_name="aikito-distribution-and-install-architecture",
                title="Aikito keeps source code and user workspaces separate",
                link_status="OK",
            )
        ]

        with patch("aikito.render._get_terminal_width", return_value=80):
            rendered = render_memory_notes_table(
                notes, use_unicode=True, use_color=False
            )

        header_line = rendered.splitlines()[1]
        header_cells = header_line.split("│")[1:-1]
        note_width = len(header_cells[1]) - 2
        title_width = len(header_cells[2]) - 2
        self.assertLessEqual(abs(note_width - title_width), 1)
        self.assertGreaterEqual(note_width, 20)
        self.assertGreaterEqual(title_width, 20)

    def test_render_memory_table_combines_index_and_link_status(self) -> None:
        from aikito.render import render_memory_table

        rendered = render_memory_table(
            [
                MemoryStatusRow(
                    name="demo",
                    scope="Project",
                    status="MISSING",
                    notes_count=2,
                    updated_on=date(2025, 12, 3),
                )
            ],
            use_unicode=False,
            use_color=False,
        )

        self.assertIn("Status", rendered)
        self.assertIn("Updated", rendered)
        self.assertIn("2025-12-03", rendered)
        self.assertNotIn("Link Status", rendered)
        self.assertNotIn("Index", rendered)
        self.assertIn("! M", rendered)

    def test_render_memory_table_uses_calendar_relative_dates(self) -> None:
        from aikito.render import _format_memory_updated_date

        current = date.today()
        self.assertEqual(_format_memory_updated_date(current), "today")
        self.assertEqual(
            _format_memory_updated_date(current - timedelta(days=1)), "yesterday"
        )
        self.assertEqual(
            _format_memory_updated_date(date(current.year, 8, 26)), "Aug 26"
        )
        self.assertEqual(_format_memory_updated_date(date(2025, 12, 3)), "2025-12-03")

    def test_render_skills_table_legend(self) -> None:
        clean_rows = [
            SkillRow(
                skill_name="test-skill",
                scope="Global",
                source_status="OK",
                description="Test description text",
            ),
        ]
        out_clean = render_skills_table(clean_rows, use_unicode=True, use_color=False)
        self.assertNotIn("Legend:", out_clean)

        issue_rows = [
            SkillRow(
                skill_name="orphan-skill",
                scope="Orphan",
                source_status="MISSING",
                description="-",
            ),
        ]
        out_issue = render_skills_table(issue_rows, use_unicode=True, use_color=False)
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ M missing", out_issue)

    def test_render_mcp_status_table_legend(self) -> None:
        clean_rows = [
            MCPServerRow(
                server_name="test-srv",
                agent_statuses={"Codex": "OK", "Claude Code": "SKIP"},
            )
        ]
        out_clean = render_mcp_status_table(
            clean_rows, ["Codex", "Claude Code"], use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        issue_rows = [
            MCPServerRow(
                server_name="test-srv",
                agent_statuses={"Codex": "DRIFT", "Claude Code": "MISSING"},
            )
        ]
        out_issue = render_mcp_status_table(
            issue_rows, ["Codex", "Claude Code"], use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ D drift", out_issue)

    def test_render_mcp_runtime_table_legend(self) -> None:
        clean_rows = [
            MCPRuntimeRow(
                agent_name="codex",
                agent_display_name="Codex",
                connect_status="OK",
                auth_method="Basic",
                tool_names=("tool1",),
            )
        ]
        out_clean = render_mcp_runtime_table(
            clean_rows, use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        error_rows = [
            MCPRuntimeRow(
                agent_name="codex",
                agent_display_name="Codex",
                connect_status="ERROR",
                auth_method="Basic",
                tool_names=(),
                error="Connection refused",
            )
        ]
        out_error = render_mcp_runtime_table(
            error_rows, use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_error)
        self.assertIn("⚠ E error", out_error)

    def test_render_agent_mcp_table_legend(self) -> None:
        clean_rows = [
            MCPDetailRow(
                server_name="rovo",
                target_name="rovo",
                agent_name="codex",
                agent_display_name="Codex",
                source="managed",
                status="OK",
                config_path=Path("/tmp/dummy"),
                config_format="toml",
                entry=None,
            )
        ]
        out_clean = render_agent_mcp_table(
            clean_rows, use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        issue_rows = [
            MCPDetailRow(
                server_name="rovo",
                target_name="rovo",
                agent_name="codex",
                agent_display_name="Codex",
                source="managed",
                status="DRIFT",
                config_path=Path("/tmp/dummy"),
                config_format="toml",
                entry=None,
            )
        ]
        out_issue = render_agent_mcp_table(
            issue_rows, use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ D drift", out_issue)

    def test_render_subagents_status_table_legend(self) -> None:
        clean_rows = [
            SubagentRow(
                subagent_name="clean",
                agent_statuses={"Codex": "OK"},
            )
        ]
        out_clean = render_subagents_status_table(
            clean_rows, [], ["Codex"], use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        issue_rows = [
            SubagentRow(
                subagent_name="conflict",
                agent_statuses={"Codex": "CONFLICT"},
            )
        ]
        out_issue = render_subagents_status_table(
            issue_rows, [], ["Codex"], use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ C conflict", out_issue)

        orphan_out = render_subagents_status_table(
            clean_rows,
            [OrphanSubagentFile("Codex", "/path/old.md")],
            ["Codex"],
            use_unicode=True,
            use_color=False,
        )
        self.assertIn("Legend:", orphan_out)

    def test_render_agent_subagent_table_legend(self) -> None:
        clean_rows = [
            SubagentDetailRow(
                subagent_name="formatter",
                description="Code formatter",
                agent_name="codex",
                agent_display_name="Codex",
                status="OK",
                target_path=Path("/tmp/formatter.toml"),
                config_format="toml",
                platform_options={},
                canonical_path=Path("/tmp/formatter.md"),
            )
        ]
        out_clean = render_agent_subagent_table(
            clean_rows, use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        issue_rows = [
            SubagentDetailRow(
                subagent_name="formatter",
                description="Code formatter",
                agent_name="codex",
                agent_display_name="Codex",
                status="MISSING",
                target_path=Path("/tmp/formatter.toml"),
                config_format="toml",
                platform_options={},
                canonical_path=Path("/tmp/formatter.md"),
            )
        ]
        out_issue = render_agent_subagent_table(
            issue_rows, use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ M missing", out_issue)

    def test_render_memory_notes_table_legend(self) -> None:
        clean_notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                link_status="OK",
            )
        ]
        out_clean = render_memory_notes_table(
            clean_notes, use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        conflict_notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                link_status="CONFLICT",
            )
        ]
        out_conflict = render_memory_notes_table(
            conflict_notes, use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_conflict)

    def test_render_projects_table_legend(self) -> None:
        clean_projects = [
            ProjectSummary(
                name="demo",
                path=Path("/tmp/demo"),
                config_path=Path("/tmp/demo/agent.toml"),
                description="demo project",
                sync_mode="link",
                instructions_status="OK",
                skills_count=2,
                memory_notes_count=3,
                runtime_status="OK",
            )
        ]
        out_clean = render_projects_table(
            clean_projects, use_unicode=True, use_color=False
        )
        self.assertIn("✓", out_clean)
        self.assertIn("Paths", out_clean)
        self.assertIn("Status", out_clean)

        issue_projects = [
            ProjectSummary(
                name="demo",
                path=Path("/tmp/demo"),
                config_path=Path("/tmp/demo/agent.toml"),
                description="demo project",
                sync_mode="link",
                instructions_status="MISSING",
                skills_count=2,
                memory_notes_count=3,
                runtime_status="CONFLICT",
            )
        ]
        out_issue = render_projects_table(
            issue_projects, use_unicode=True, use_color=False
        )
        self.assertIn("! conflict", out_issue)

    def test_render_legend_format(self) -> None:
        unicode_legend = render_legend(use_unicode=True, use_color=False)
        self.assertEqual(
            unicode_legend,
            "Legend: ✓ synced · – n/a · 0 none · ⚠ M missing · ⚠ C conflict · ⚠ D drift · ⚠ E error",
        )
        ascii_legend = render_legend(use_unicode=False, use_color=False)
        self.assertEqual(
            ascii_legend,
            "Legend: v synced * - n/a * 0 none * ! M missing * ! C conflict * ! D drift * ! E error",
        )


class AikitoStatusCollectorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.aikito_dir = root / "aikito"
        self.home = root / "home"
        (self.home / ".codex").mkdir(parents=True)
        self.assertTrue(init_workspace(self.aikito_dir, self.home))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_collect_status_data_structure(self) -> None:
        data = get_status_report_data(self.aikito_dir, self.home)
        self.assertIsInstance(data, StatusReportData)
        self.assertTrue(len(data.agents) > 0)
        self.assertTrue(len(data.memories) > 0)
        self.assertTrue(data.total_skills_count >= 0)
        self.assertTrue(data.total_memory_notes >= 0)

    def test_status_ignores_legacy_index(self) -> None:
        note = self.aikito_dir / "memory" / "notes" / "decision.md"
        note.write_text(
            "---\ncategory: Decisions\n---\n\n# Decision\n", encoding="utf-8"
        )
        index_file = self.aikito_dir / "memory" / "index.md"
        index_file.write_text("# Legacy\n", encoding="utf-8")

        data = get_status_report_data(self.aikito_dir, self.home)

        global_memory = next(row for row in data.memories if row.scope == "Global")
        self.assertEqual(global_memory.name, "Global")
        self.assertEqual(global_memory.status, "OK")
        self.assertEqual(index_file.read_text(encoding="utf-8"), "# Legacy\n")

    def test_capable_agent_without_targets_shows_zero_not_skip(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "home"
            aikito_dir = root / "aikito"
            home.mkdir()
            (aikito_dir / "global").mkdir(parents=True)
            (aikito_dir / "global" / "AGENTS.md").write_text("", encoding="utf-8")
            (aikito_dir / "skills.toml").write_text("skills = []\n", encoding="utf-8")
            (aikito_dir / "mcps").mkdir()
            (aikito_dir / "subagents.toml").write_text(
                "[subagents]\n", encoding="utf-8"
            )
            (aikito_dir / "agents.toml").write_text(
                '[agents.codex]\ndisplay_name = "Codex"\n'
                'instruction_path = ".codex/AGENTS.md"\n'
                'skills_path = ".agents/skills"\n'
                "[agents.codex.subagents]\n"
                'config_path = ".codex/agents"\n'
                'config_format = "codex_toml"\n'
                "[agents.codex.mcp]\n"
                'config_path = ".codex/config.toml"\n'
                'config_format = "toml"\n'
                'name_style = "underscore"\n'
                '[agents.pi]\ndisplay_name = "Pi"\n'
                'instruction_path = ".pi/agent/AGENTS.md"\n'
                'skills_path = ".agents/skills"\n',
                encoding="utf-8",
            )

            rows, _issues, _subagents, _mcp = collect_agent_status_rows(
                aikito_dir, home
            )
            by_name = {row.agent_name: row for row in rows}

            self.assertEqual(by_name["codex"].mcp_status, "OK (0)")
            self.assertEqual(by_name["codex"].subagent_status, "OK (0)")
            self.assertEqual(by_name["pi"].mcp_status, "SKIP")
            self.assertEqual(by_name["pi"].subagent_status, "SKIP")

    def test_collect_mcp_matrix(self) -> None:
        rows, agents = collect_mcp_matrix(self.aikito_dir, self.home)
        self.assertIsInstance(rows, list)
        self.assertIsInstance(agents, list)

    def test_collect_mcp_runtime_compares_agents_for_one_server(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "home"
            aikito_dir = root / "aikito"
            (home / ".codex").mkdir(parents=True)
            (home / ".gemini/config").mkdir(parents=True)
            (aikito_dir / "mcps").mkdir(parents=True)
            (aikito_dir / "agents.toml").write_text(
                """
[agents.codex]
display_name = "Codex"
[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "underscore"

[agents.agy]
display_name = "Antigravity CLI"
[agents.agy.mcp]
config_path = ".gemini/config/mcp_config.json"
config_format = "agy_json"
name_style = "verbatim"
""".lstrip()
            )
            (aikito_dir / "mcps/managed.toml").write_text(
                'transport = "remote"\nurl = "https://example.com/mcp"\n'
                'agents = ["codex", "agy"]\n'
            )
            (home / ".codex/config.toml").write_text(
                '[mcp_servers.managed]\nurl = "https://example.com/mcp"\n'
            )
            (home / ".gemini/config/mcp_config.json").write_text(
                '{"mcpServers":{"managed":{"serverUrl":"https://example.com/mcp"}}}'
            )

            with patch(
                "aikito.status.probe_mcp_tools_for_specs",
                return_value=[
                    MCPToolProbeResult(
                        "codex", "OK", "Basic · env header", ("one", "two")
                    ),
                    MCPToolProbeResult("agy", "OK", "Basic · inline header", ("one",)),
                ],
            ):
                server_name, rows = collect_mcp_runtime(aikito_dir, home, "man")

            self.assertEqual(server_name, "managed")
            self.assertEqual(
                [row.agent_display_name for row in rows], ["Codex", "Antigravity CLI"]
            )
            self.assertEqual([len(row.tool_names) for row in rows], [2, 1])

    def test_collect_mcp_details_lists_unmanaged_and_redacts_authorization(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "home"
            aikito_dir = root / "aikito"
            config_dir = home / ".gemini/config"
            config_dir.mkdir(parents=True)
            aikito_dir.mkdir()
            (aikito_dir / "agents.toml").write_text(
                """
[agents.agy]
display_name = "Antigravity CLI"

[agents.agy.mcp]
config_path = ".gemini/config/mcp_config.json"
config_format = "agy_json"
name_style = "verbatim"
""".lstrip()
            )
            (aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
            (aikito_dir / "mcps/managed.toml").write_text(
                """
transport = "remote"
url = "https://example.com/mcp"
agents = ["agy"]
""".lstrip()
            )
            (config_dir / "mcp_config.json").write_text(
                """{
  "mcpServers": {
    "managed": {
      "serverUrl": "https://example.com/mcp",
      "headers": {"Authorization": "Basic sensitive"}
    },
    "custom": {"serverUrl": "https://custom.example.com"}
  }
}
"""
            )

            rows = collect_mcp_details(aikito_dir, home, agent_target="agy")

            self.assertEqual([row.server_name for row in rows], ["managed", "custom"])
            self.assertEqual(rows[0].entry["headers"]["Authorization"], "<redacted>")
            self.assertEqual(rows[1].source, "unmanaged")
            self.assertIsNone(rows[1].entry)

    def test_collect_mcp_matrix_marks_successful_live_checks(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "home"
            aikito_dir = root / "aikito"
            (home / ".codex").mkdir(parents=True)
            aikito_dir.mkdir()
            (aikito_dir / "agents.toml").write_text(
                """
[agents.codex]
display_name = "Codex"

[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "underscore"
live_command = ["codex", "mcp", "list"]
""".lstrip()
            )
            (aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
            (aikito_dir / "mcps/managed.toml").write_text(
                """
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex"]
""".lstrip()
            )
            (home / ".codex" / "config.toml").write_text(
                '[mcp_servers.managed]\nurl = "https://example.com/mcp"\n'
            )

            with patch(
                "aikito.status.probe_mcp_tools_for_specs",
                return_value=[
                    MCPToolProbeResult(
                        agent="codex",
                        status="OK",
                        auth_method="None",
                        tool_names=("one", "two"),
                    )
                ],
            ) as live_check:
                rows, _ = collect_mcp_matrix(aikito_dir, home, live=True)

            self.assertEqual(rows[0].agent_statuses["Codex"], "OK (2)")
            self.assertEqual(len(live_check.call_args.args[0]), 1)
            self.assertEqual(live_check.call_args.args[0][0].agent, "codex")

    def test_collect_subagents_matrix(self) -> None:
        sub_rows, orphans, agents = collect_subagents_matrix(self.aikito_dir, self.home)
        self.assertIsInstance(sub_rows, list)
        self.assertIsInstance(orphans, list)
        self.assertIsInstance(agents, list)

    def test_dangling_symlink_reports_conflict_in_agent_status(self) -> None:
        from aikito.status import collect_agent_status_rows

        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            home = root / "home"
            aikito_dir = root / "aikito"
            (home / ".codex").mkdir(parents=True)
            (aikito_dir / "global").mkdir(parents=True)
            aikito_dir.mkdir(exist_ok=True)

            # Expected source does NOT exist
            expected_global_agents = aikito_dir / "global" / "AGENTS.md"

            (aikito_dir / "agents.toml").write_text(
                """
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"
""".strip()
            )
            (aikito_dir / "skills.toml").write_text("skills = []\n")
            (aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
            (aikito_dir / "subagents.toml").write_text("[subagents]\n")

            # Create dangling symlink pointing to expected_global_agents (which does not exist!)
            target_link = home / ".codex" / "AGENTS.md"
            target_link.symlink_to(expected_global_agents)

            rows, issues, _, _ = collect_agent_status_rows(aikito_dir, home)
            codex_row = next(r for r in rows if r.agent_name == "codex")

            # With strict=True in classify_symlink, dangling symlink must be reported as CONFLICT
            self.assertEqual(codex_row.instructions_status, "CONFLICT")
            self.assertGreater(issues, 0)

    def test_collect_skills_rows(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            root = Path(tmp_dir)
            aikito_dir = root / "aikito"
            aikito_dir.mkdir()
            (aikito_dir / "skills.toml").write_text(
                'skills = ["global-skill-1", "missing-skill"]\n'
            )

            skills_dir = aikito_dir / "skills"
            skills_dir.mkdir()

            g_skill = skills_dir / "global-skill-1"
            g_skill.mkdir()
            (g_skill / "SKILL.md").write_text(
                "---\nname: global-skill-1\ndescription: Global skill description\n---\n"
            )

            orphan_skill = skills_dir / "orphan-skill"
            orphan_skill.mkdir()
            (orphan_skill / "SKILL.md").write_text(
                "---\nname: orphan-skill\ndescription: Orphan description\n---\n"
            )

            projects_dir = aikito_dir / "projects" / "test-proj"
            projects_dir.mkdir(parents=True)
            (projects_dir / "agent.toml").write_text(
                'name = "test-proj"\nskills = ["proj-skill"]\n'
            )

            p_skill = skills_dir / "proj-skill"
            p_skill.mkdir()
            (p_skill / "SKILL.md").write_text(
                "---\nname: proj-skill\ndescription: Project skill description\n---\n"
            )

            rows = collect_skills_rows(aikito_dir)

            self.assertEqual(len(rows), 4)

            global_row = next(r for r in rows if r.skill_name == "global-skill-1")
            self.assertEqual(global_row.scope, "Global")
            self.assertEqual(global_row.source_status, "OK")
            self.assertEqual(global_row.description, "Global skill description")

            missing_row = next(r for r in rows if r.skill_name == "missing-skill")
            self.assertEqual(missing_row.scope, "Global")
            self.assertEqual(missing_row.source_status, "MISSING")
            self.assertEqual(missing_row.description, "-")

            proj_row = next(r for r in rows if r.skill_name == "proj-skill")
            self.assertEqual(proj_row.scope, "test-proj")
            self.assertEqual(proj_row.source_status, "OK")
            self.assertEqual(proj_row.description, "Project skill description")

            orphan_row = next(r for r in rows if r.skill_name == "orphan-skill")
            self.assertEqual(orphan_row.scope, "Orphan")
            self.assertEqual(orphan_row.source_status, "OK")
            self.assertEqual(orphan_row.description, "Orphan description")

    def test_render_skills_table(self) -> None:
        rows = [
            SkillRow(
                skill_name="test-skill",
                scope="Global",
                source_status="OK",
                description="Test description text",
            ),
            SkillRow(
                skill_name="orphan-skill",
                scope="Orphan",
                source_status="MISSING",
                description="-",
            ),
        ]
        out = render_skills_table(rows, use_unicode=True, use_color=False)
        self.assertIn("test-skill", out)
        self.assertIn("Global", out)
        self.assertIn("Test description text", out)
        self.assertIn("orphan-skill", out)
        self.assertIn("Orphan", out)

    def test_render_skills_depth_symbols(self) -> None:
        from aikito.render import render_agents_table

        rows = [
            AgentStatusRow(
                agent_name="codex",
                display_name="Codex",
                instructions_status="OK",
                skills_status="OK (2)",
                skills_link_depth=1,
                mcp_status="OK (1)",
                subagent_status="SKIP",
            ),
            AgentStatusRow(
                agent_name="github-copilot",
                display_name="GitHub Copilot CLI",
                instructions_status="OK",
                skills_status="OK (2)",
                skills_link_depth=1,
                mcp_status="OK (1)",
                subagent_status="SKIP",
            ),
            AgentStatusRow(
                agent_name="claude-code",
                display_name="Claude Code",
                instructions_status="OK",
                skills_status="OK (2)",
                skills_link_depth=2,
                mcp_status="OK (1)",
                subagent_status="SKIP",
            ),
            AgentStatusRow(
                agent_name="opencode",
                display_name="OpenCode",
                instructions_status="OK",
                skills_status="OK (2)",
                skills_link_depth=1,
                mcp_status="OK (1)",
                subagent_status="SKIP",
            ),
        ]

        rendered_unicode = render_agents_table(rows, use_unicode=True, use_color=False)
        self.assertIn("2 ›", rendered_unicode)
        self.assertIn("2 »", rendered_unicode)
        self.assertIn("–", rendered_unicode)

        rendered_ascii = render_agents_table(rows, use_unicode=False, use_color=False)
        self.assertIn("2 >", rendered_ascii)
        self.assertIn("2 >>", rendered_ascii)
        self.assertIn("-", rendered_ascii)

    def test_collect_memory_notes_rows_offline_project(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            aikito_dir = Path(tmpdir) / "workspace"
            home = Path(tmpdir) / "home"
            init_workspace(aikito_dir, home)

            proj_dir = aikito_dir / "projects" / "myproject"
            proj_dir.mkdir(parents=True, exist_ok=True)
            (proj_dir / "agent.toml").write_text(
                'name = "myproject"\npath = "~/nonexistent-checkout"\n',
                encoding="utf-8",
            )
            notes_dir = proj_dir / "memory" / "notes"
            notes_dir.mkdir(parents=True, exist_ok=True)
            (notes_dir / "demo.md").write_text(
                "# Demo Title\nContent\n", encoding="utf-8"
            )

            rows = collect_memory_notes_rows(aikito_dir, home)
            proj_rows = [r for r in rows if r.scope_name == "myproject"]
            self.assertEqual(len(proj_rows), 1)
            self.assertEqual(proj_rows[0].link_status, "OFFLINE")

            rendered = render_memory_notes_table(
                proj_rows, use_unicode=True, use_color=False
            )
            self.assertIn("–", rendered)
            self.assertNotIn("Legend:", rendered)

    def test_collect_agent_status_rows_with_subagent_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            aikito_dir = Path(tmpdir) / "workspace"
            home = Path(tmpdir) / "home"
            init_workspace(aikito_dir, home)

            subagent_dir = aikito_dir / "subagents"
            subagent_dir.mkdir(parents=True, exist_ok=True)
            (subagent_dir / "bad-agent.md").write_text(
                "Prompt body",
                encoding="utf-8",
            )
            (aikito_dir / "subagents.toml").write_text(
                '[subagents.bad-agent]\ndescription = "Test"\nagents = ["nonexistent-agent"]\n',
                encoding="utf-8",
            )

            # Must not raise SubagentConfigError; must count as an agent issue
            rows, agent_issues, total_subagents, total_mcp = collect_agent_status_rows(
                aikito_dir, home
            )
            self.assertGreaterEqual(agent_issues, 1)

            report = get_status_report_data(aikito_dir, home)
            self.assertGreaterEqual(report.issues_count, 1)

    def test_status_report_scope_rows_and_consumers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            aikito_dir = Path(tmpdir) / "workspace"
            home = Path(tmpdir) / "home"
            init_workspace(aikito_dir, home)

            # Global AGENTS.md with 3 physical lines
            (aikito_dir / "AGENTS.md").write_text(
                "# Line 1\n# Line 2\n# Line 3\n", encoding="utf-8"
            )
            # Create a project with 2 lines in AGENTS.md
            proj_dir = aikito_dir / "projects" / "myproj"
            proj_dir.mkdir(parents=True)
            (proj_dir / "AGENTS.md").write_text(
                "Instr line 1\nInstr line 2\n", encoding="utf-8"
            )
            (proj_dir / "agent.toml").write_text(
                'name = "myproj"\nskills = ["aikito"]\n', encoding="utf-8"
            )

            report = get_status_report_data(aikito_dir, home)
            self.assertEqual(report.global_summary.instr, "3L")

            proj_row = next(p for p in report.projects if p.name == "myproj")
            self.assertEqual(proj_row.skills_count, 1)

            rendered = render_status_report(
                report,
                is_tty=False,
                no_color=True,
                workspace=str(aikito_dir),
                home=home,
            )
            self.assertIn("myproj", rendered)
            self.assertIn("Global: ", rendered)
            self.assertIn("Instr 3L", rendered)
            self.assertIn("Consumers (", rendered)
            self.assertIn("All in one workspace:", rendered)
            self.assertIn("2L", rendered)

    def test_project_health_priority_order(self) -> None:
        proj_base = ProjectSummary(
            name="demo",
            path="-",
            config_path=Path("/tmp/demo/agent.toml"),
            sync_mode="link",
            instructions_status="OK",
            skills_count=1,
            memory_notes_count=1,
            runtime_status="OK",
            candidate_paths=(("default", "/tmp/demo", True),),
        )

        # INVALID CONFIG > CONFLICT
        p_invalid = ProjectSummary(
            **{
                **proj_base.__dict__,
                "runtime_status": "INVALID CONFIG",
                "instructions_status": "CONFLICT",
            }
        )
        self.assertEqual(
            evaluate_project_health(p_invalid, "MISSING"), "! invalid config"
        )

        # CONFLICT > DRIFT
        p_conflict = ProjectSummary(
            **{
                **proj_base.__dict__,
                "runtime_status": "CONFLICT",
                "instructions_status": "OK",
            }
        )
        self.assertEqual(evaluate_project_health(p_conflict, "DRIFT"), "! conflict")

        # DRIFT > MISSING
        p_drift = ProjectSummary(
            **{
                **proj_base.__dict__,
                "runtime_status": "DRIFT",
                "instructions_status": "MISSING",
            }
        )
        self.assertEqual(evaluate_project_health(p_drift, "MISSING"), "! drift")

        # MISSING > UNBOUND
        p_missing = ProjectSummary(
            **{
                **proj_base.__dict__,
                "runtime_status": "UNBOUND",
                "instructions_status": "MISSING",
            }
        )
        self.assertEqual(evaluate_project_health(p_missing, "OK"), "! missing")

        # UNBOUND alone
        p_unbound = ProjectSummary(
            name="demo",
            path="-",
            config_path=Path("/tmp/demo/agent.toml"),
            sync_mode="link",
            instructions_status="OK",
            skills_count=1,
            memory_notes_count=1,
            runtime_status="UNBOUND",
            candidate_paths=(),
        )
        self.assertEqual(evaluate_project_health(p_unbound, "OK"), "! unbound")

        # EMPTY instructions does not trigger anomaly
        p_empty = ProjectSummary(
            **{
                **proj_base.__dict__,
                "runtime_status": "OK",
                "instructions_status": "EMPTY",
            }
        )
        self.assertEqual(evaluate_project_health(p_empty, "OK"), "OK")

    def test_offline_candidate_paths(self) -> None:
        p_all_offline = ProjectSummary(
            name="offline-proj",
            path="-",
            config_path=Path("/tmp/off/agent.toml"),
            sync_mode="link",
            instructions_status="OK",
            skills_count=0,
            memory_notes_count=0,
            runtime_status="OFFLINE",
            candidate_paths=(
                ("default", "/nonexistent/a", False),
                ("alt", "/nonexistent/b", False),
            ),
        )
        self.assertEqual(evaluate_project_health(p_all_offline, "OK"), "-")
        self.assertEqual(format_project_path_counts(p_all_offline), "0/2")

        p_partial_offline = ProjectSummary(
            name="partial-proj",
            path="-",
            config_path=Path("/tmp/part/agent.toml"),
            sync_mode="link",
            instructions_status="OK",
            skills_count=0,
            memory_notes_count=0,
            runtime_status="OK",
            candidate_paths=(
                ("default", "/existent/a", True),
                ("alt", "/nonexistent/b", False),
            ),
        )
        self.assertEqual(evaluate_project_health(p_partial_offline, "OK"), "OK")
        self.assertEqual(format_project_path_counts(p_partial_offline), "1/2")

    def test_status_and_show_projects_consistency(self) -> None:
        p = ProjectSummary(
            name="drift-proj",
            path="-",
            config_path=Path("/tmp/drift/agent.toml"),
            sync_mode="copy",
            instructions_status="OK",
            skills_count=2,
            memory_notes_count=4,
            runtime_status="DRIFT",
            candidate_paths=(("default", "/tmp/drift", True),),
        )
        mem_row = MemoryStatusRow(
            name="drift-proj",
            scope="Project",
            status="OK",
            notes_count=4,
            updated_on=date.today(),
        )
        health = evaluate_project_health(p, mem_row.status)
        self.assertEqual(health, "! drift")

        table = render_projects_table(
            [p], use_unicode=False, use_color=False, memory_rows=[mem_row]
        )
        self.assertIn("! drift", table)

    def test_show_memory_updated_date_formats(self) -> None:
        today = date.today()
        yesterday = today - timedelta(days=1)
        last_year = date(today.year - 1, 6, 15)

        notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="note-today",
                title="T1",
                link_status="OK",
                updated_on=today,
            ),
            MemoryNoteRow(
                scope_name="Global",
                note_name="note-yesterday",
                title="T2",
                link_status="OK",
                updated_on=yesterday,
            ),
            MemoryNoteRow(
                scope_name="Global",
                note_name="note-lastyear",
                title="T3",
                link_status="OK",
                updated_on=last_year,
            ),
        ]
        rendered = render_memory_notes_table(notes, use_unicode=False, use_color=False)
        self.assertIn("Updated", rendered)
        self.assertIn("today", rendered)
        self.assertIn("yesterday", rendered)
        self.assertIn(last_year.isoformat(), rendered)

    def test_status_report_full_layout(self) -> None:
        p1 = ProjectSummary(
            name="aikito",
            path=Path("/Users/user/aikito"),
            config_path=Path("/Users/user/aikito/agent.toml"),
            sync_mode="link",
            instructions_status="OK",
            skills_count=1,
            memory_notes_count=12,
            runtime_status="OK",
        )
        p2 = ProjectSummary(
            name="infra",
            path=Path("/Users/user/infra"),
            config_path=Path("/Users/user/infra/agent.toml"),
            sync_mode="copy",
            instructions_status="DRIFT",
            skills_count=6,
            memory_notes_count=21,
            runtime_status="DRIFT",
        )
        global_summary = GlobalSummary(
            status="OK",
            instr="24L",
            skills_count=12,
            memory_notes_count=6,
            mcp_count="1",
            subagent_count="3",
        )
        report_data = StatusReportData(
            agents=[],
            memories=[],
            projects=[p1, p2],
            global_summary=global_summary,
            consumers=["claude", "copilot"],
        )
        rendered_ascii = render_status_report(
            report_data,
            is_tty=False,
            no_color=True,
            workspace="/Users/user/aikito",
            workspace_source="default",
            home=Path("/Users/user"),
        )
        self.assertIn("Project", rendered_ascii)
        self.assertIn("Instr", rendered_ascii)
        self.assertIn("Skills", rendered_ascii)
        self.assertIn("Memory", rendered_ascii)
        self.assertIn("Paths", rendered_ascii)
        self.assertIn("Mode", rendered_ascii)
        self.assertIn("Status", rendered_ascii)
        self.assertTrue(
            rendered_ascii.index("Project")
            < rendered_ascii.index("Instr")
            < rendered_ascii.index("Skills")
            < rendered_ascii.index("Memory")
            < rendered_ascii.index("Paths")
            < rendered_ascii.index("Mode")
            < rendered_ascii.index("Status")
        )
        self.assertIn("aikito", rendered_ascii)
        self.assertIn("infra", rendered_ascii)
        self.assertIn("! drift", rendered_ascii)
        self.assertIn(
            "Global: v · Instr 24L · Skills 12 · Memory 6 · MCP 1 · Sub 3",
            rendered_ascii,
        )
        self.assertIn("Consumers (2): claude · copilot", rendered_ascii)
        self.assertIn("All in one workspace: ~/aikito (default)", rendered_ascii)

        rendered_tty = render_status_report(
            report_data,
            is_tty=True,
            no_color=False,
            workspace="/Users/user/aikito",
            workspace_source="configured",
            home=Path("/Users/user"),
        )
        self.assertIn(
            "\033[1mAll in one workspace: ~/aikito\033[0m (configured)", rendered_tty
        )
        self.assertIn("Global: \033[32m✓\033[0m", rendered_tty)


if __name__ == "__main__":
    unittest.main()
