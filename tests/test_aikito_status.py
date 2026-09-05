import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from aikito_init import init_workspace
from aikito_mcp import MCPToolProbeResult
from aikito_project import ProjectSummary
from aikito_render import (
    AgentStatusRow,
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
    render_mcp_status_table,
    render_mcp_runtime_table,
    render_memory_notes_table,
    render_projects_table,
    render_skills_table,
    render_status_report,
    render_subagents_status_table,
)
from aikito_status import (
    MCPDetailRow,
    MCPRuntimeRow,
    SubagentDetailRow,
    _summarize_subagent_status,
    collect_agent_status_rows,
    collect_mcp_details,
    collect_mcp_matrix,
    collect_mcp_runtime,
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
                name="Global Memory",
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
        self.assertIn("Legend:", rendered)
        self.assertIn("⚠ M", rendered)
        self.assertIn("⚠ C 3/4", rendered)
        self.assertIn("2 issues · run: aikito doctor", rendered)
        self.assertNotIn("notes across", rendered)
        self.assertNotIn("Memory Resources", rendered)

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
        self.assertNotIn("Legend:", rendered)
        self.assertIn("✓ all synced · 1 agents · 11 skills", rendered)

        self.assertIn("  27 notes across 1 scopes", rendered)

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
        self.assertTrue(
            rendered.startswith("\033[1mWorkspace: /tmp/aikito (configured)\033[0m\n\n")
        )

    def test_subagent_status_distinguishes_missing_drift_and_conflict(self) -> None:
        self.assertEqual(_summarize_subagent_status(["CREATE"]), "MISSING (0/1)")

    def test_count_badges_render_without_checkmark(self) -> None:
        from aikito_render import render_agents_table

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
        self.assertEqual(_summarize_subagent_status(["UPDATE"]), "DRIFT (0/1)")
        self.assertEqual(_summarize_subagent_status(["CONFLICT"]), "CONFLICT (0/1)")

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
        from aikito_render import _get_display_width

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
        from aikito_render import _truncate_display_text

        self.assertEqual(_truncate_display_text("hello world", 8), "hello w…")
        self.assertEqual(_truncate_display_text("中文测试标题", 7), "中文测…")

    def test_render_memory_notes_table(self) -> None:
        notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                is_indexed=True,
                link_status="SKIP",
            ),
            MemoryNoteRow(
                scope_name="aikito",
                note_name="unindexed",
                title="-",
                is_indexed=False,
                link_status="OK",
            ),
        ]
        rendered = render_memory_notes_table(notes, use_unicode=True, use_color=False)
        self.assertIn("Global", rendered)
        self.assertIn("demo", rendered)
        self.assertIn("Demo Title", rendered)
        self.assertIn("✓", rendered)
        self.assertIn("⚠ M", rendered)

    def test_render_memory_notes_table_balances_note_and_title_widths(self) -> None:
        notes = [
            MemoryNoteRow(
                scope_name="aikito",
                note_name="aikito-distribution-and-install-architecture",
                title="Aikito keeps source code and user workspaces separate",
                is_indexed=True,
                link_status="OK",
            )
        ]

        with patch("aikito_render._get_terminal_width", return_value=80):
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
        from aikito_render import render_memory_table

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
        from aikito_render import _format_memory_updated_date

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
                is_indexed=True,
                link_status="OK",
            )
        ]
        out_clean = render_memory_notes_table(
            clean_notes, use_unicode=True, use_color=False
        )
        self.assertNotIn("Legend:", out_clean)

        unindexed_notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                is_indexed=False,
                link_status="OK",
            )
        ]
        out_issue = render_memory_notes_table(
            unindexed_notes, use_unicode=True, use_color=False
        )
        self.assertIn("Legend:", out_issue)

        conflict_notes = [
            MemoryNoteRow(
                scope_name="Global",
                note_name="demo",
                title="Demo Title",
                is_indexed=True,
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
        self.assertNotIn("Legend:", out_clean)

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
        self.assertIn("Legend:", out_issue)
        self.assertIn("⚠ M missing", out_issue)
        self.assertIn("⚠ C conflict", out_issue)

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
                "aikito_status.probe_mcp_tools_for_specs",
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
                "aikito_status.probe_mcp_tools_for_specs",
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
        from aikito_status import collect_agent_status_rows

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
        from aikito_render import render_agents_table

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


if __name__ == "__main__":
    unittest.main()
