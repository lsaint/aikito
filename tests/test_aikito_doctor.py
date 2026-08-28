"""Tests for aikito_link and aikito_doctor modules."""

import json
import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_link import SymlinkVerdict, classify_symlink, symlink_verdict_to_status  # noqa: E402
from aikito_mcp import AgentSpec  # noqa: E402
from aikito_doctor import (  # noqa: E402
    check_config_syntax,
    check_drift,
    check_environment,
    check_orphans,
    check_projects,
    check_security,
    check_symlinks,
    run_doctor,
)
from aikito_render import (  # noqa: E402
    DoctorFinding,
    DoctorReport,
    DoctorSection,
    render_doctor_report,
)
from aikito_subagent import PlanItem  # noqa: E402


# ---------------------------------------------------------------------------
# aikito_link tests
# ---------------------------------------------------------------------------


class ClassifySymlinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_ok_valid_symlink(self) -> None:
        source = self.root / "source.md"
        source.write_text("content")
        link = self.root / "link.md"
        link.symlink_to(source)
        self.assertEqual(classify_symlink(link, source), SymlinkVerdict.OK)

    def test_dangling_symlink(self) -> None:
        nonexistent = self.root / "ghost.md"
        link = self.root / "link.md"
        link.symlink_to(nonexistent)
        self.assertEqual(classify_symlink(link, nonexistent), SymlinkVerdict.DANGLING)

    def test_wrong_target(self) -> None:
        source_a = self.root / "a.md"
        source_a.write_text("a")
        source_b = self.root / "b.md"
        source_b.write_text("b")
        link = self.root / "link.md"
        link.symlink_to(source_a)
        self.assertEqual(classify_symlink(link, source_b), SymlinkVerdict.WRONG_TARGET)

    def test_not_symlink_regular_file(self) -> None:
        path = self.root / "plain.md"
        path.write_text("plain")
        expected = self.root / "other.md"
        expected.write_text("other")
        self.assertEqual(classify_symlink(path, expected), SymlinkVerdict.NOT_SYMLINK)

    def test_missing_path(self) -> None:
        path = self.root / "missing.md"
        expected = self.root / "expected.md"
        self.assertEqual(classify_symlink(path, expected), SymlinkVerdict.MISSING)

    def test_status_mapping(self) -> None:
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.OK), "OK")
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.DANGLING), "CONFLICT")
        self.assertEqual(
            symlink_verdict_to_status(SymlinkVerdict.WRONG_TARGET), "CONFLICT"
        )
        self.assertEqual(
            symlink_verdict_to_status(SymlinkVerdict.NOT_SYMLINK), "CONFLICT"
        )
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.MISSING), "MISSING")


# ---------------------------------------------------------------------------
# render_doctor_report tests
# ---------------------------------------------------------------------------


class RenderDoctorReportTest(unittest.TestCase):
    def _make_report(self) -> DoctorReport:
        return DoctorReport(
            sections=[
                DoctorSection(
                    name="Symlinks",
                    findings=[
                        DoctorFinding(status="OK", message="Global instructions OK"),
                        DoctorFinding(
                            status="FAIL",
                            message="Project foo: dangling symlink",
                            fix_hint="aikito sync project foo",
                        ),
                    ],
                ),
                DoctorSection(
                    name="Environment",
                    findings=[
                        DoctorFinding(
                            status="WARN", message="opencode not found in $PATH"
                        ),
                    ],
                ),
            ]
        )

    def test_renders_section_names(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        self.assertIn("Symlinks", rendered)
        self.assertIn("Environment", rendered)

    def test_renders_fix_hint(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        self.assertIn("aikito sync project foo", rendered)

    def test_summary_shows_counts(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        self.assertIn("1 issue", rendered)
        self.assertIn("1 warning", rendered)

    def test_ascii_fallback(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(
            report, is_tty=False, no_color=True, use_unicode=False
        )
        self.assertIn("[OK]", rendered)
        self.assertIn("[FAIL]", rendered)
        self.assertIn("[WARN]", rendered)
        self.assertNotIn("╭", rendered)

    def test_all_ok_summary(self) -> None:
        report = DoctorReport(
            sections=[
                DoctorSection(
                    name="Test",
                    findings=[
                        DoctorFinding(status="OK", message="Everything fine"),
                    ],
                )
            ]
        )
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        self.assertIn("All checks passed", rendered)

    def test_box_right_border_alignment(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        lines = rendered.splitlines()
        # Find all title box header lines (starting with ╭, │, or ╰)
        box_lines = [
            line
            for line in lines
            if line.startswith("╭") or line.startswith("│") or line.startswith("╰")
        ]
        widths = [len(line) for line in box_lines]
        # All title box header lines across ALL sections must have identical width (max_title_w + 5)!
        self.assertEqual(
            len(set(widths)),
            1,
            msg=f"Title box lines have inconsistent widths: {widths}",
        )
        self.assertEqual(
            widths[0], 16
        )  # len("Environment") = 11 -> inner 14 -> total 16
        for line in box_lines:
            self.assertTrue(
                line.endswith("╮") or line.endswith("│") or line.endswith("╯")
            )

    def test_use_unicode_defaults_to_true_even_if_not_tty(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(
            report, is_tty=False, no_color=True, use_unicode=True
        )
        self.assertIn("╭", rendered)
        self.assertIn("│", rendered)
        self.assertIn("✓", rendered)

    def test_fail_count(self) -> None:
        report = self._make_report()
        self.assertEqual(report.fail_count, 1)
        self.assertEqual(report.warn_count, 1)


# ---------------------------------------------------------------------------
# Doctor project runtime tests
# ---------------------------------------------------------------------------


class CheckProjectsTest(unittest.TestCase):
    def test_reports_missing_native_instructions_with_sync_hint(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "project"
            definition = workspace / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (workspace / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (definition / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
            (definition / "memory").mkdir()

            section = check_projects(workspace, root)

        failures = [finding for finding in section.findings if finding.status == "FAIL"]
        self.assertEqual(len(failures), 1)
        self.assertIn("Project 'demo': MISSING — 1 missing", failures[0].message)
        self.assertTrue(any(f.fix_hint == "aikito sync project demo" for f in failures))

    def test_conflict_suppresses_sync_hint_for_same_project(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "project"
            definition = workspace / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (workspace / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
                "[agents.claude-code]\n"
                'project_instruction_path = ".claude/CLAUDE.md"\n',
                encoding="utf-8",
            )
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (definition / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
            (definition / "memory").mkdir()
            (project / "AGENTS.md").write_text("Unmanaged\n", encoding="utf-8")

            section = check_projects(workspace, root)

        self.assertEqual(len(section.findings), 1)
        finding = section.findings[0]
        self.assertEqual(finding.status, "FAIL")
        self.assertIn("Project 'demo': CONFLICT", finding.message)
        self.assertIn("1 missing, 1 conflict", finding.message)
        self.assertEqual(finding.fix_hint, "aikito show project demo")


# ---------------------------------------------------------------------------
# Doctor check_config_syntax tests
# ---------------------------------------------------------------------------


class CheckConfigSyntaxTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _write_minimal_toml_files(self) -> None:
        (self.aikito_dir / "agents.toml").write_text("[agents]\n")
        (self.aikito_dir / "skills.toml").write_text("skills = []\n")
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "subagents.toml").write_text("[subagents]\n")

    def test_valid_toml_files_produce_ok_findings(self) -> None:
        self._write_minimal_toml_files()
        section = check_config_syntax(self.aikito_dir, self.home)
        fail_messages = [f.message for f in section.findings if f.status == "FAIL"]
        self.assertEqual(fail_messages, [], msg=f"Unexpected failures: {fail_messages}")

    def test_invalid_toml_produces_fail(self) -> None:
        self._write_minimal_toml_files()
        (self.aikito_dir / "skills.toml").write_text("not valid toml ::::\n")
        section = check_config_syntax(self.aikito_dir, self.home)
        fails = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any("skills.toml" in f.message for f in fails),
            msg=f"Expected skills.toml failure, got: {[f.message for f in fails]}",
        )

    def test_missing_toml_produces_fail(self) -> None:
        # Don't create any files
        section = check_config_syntax(self.aikito_dir, self.home)
        fails = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any("agents.toml" in f.message for f in fails),
            msg="Expected agents.toml missing failure",
        )

    def test_missing_bundled_agent_fields_produce_fixable_warning(self) -> None:
        self._write_minimal_toml_files()
        (self.aikito_dir / "agents.toml").write_text(
            '[agents.codex]\ndisplay_name = "Custom Codex"\n', encoding="utf-8"
        )

        section = check_config_syntax(self.aikito_dir, self.home)

        warnings = [finding for finding in section.findings if finding.status == "WARN"]
        self.assertTrue(any("project_instruction_path" in f.message for f in warnings))
        self.assertTrue(all(f.fix_hint == "aikito doctor --fix" for f in warnings))

    def test_installed_unregistered_agent_produces_fixable_warning(self) -> None:
        self._write_minimal_toml_files()
        (self.aikito_dir / "agents.toml").write_text(
            '[agents.codex]\ndisplay_name = "Codex"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'instruction_path = ".codex/AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            '\n[agents.codex.runner]\ncommand = ["codex", "{prompt}"]\n',
            encoding="utf-8",
        )

        with patch(
            "aikito_doctor._detect_existing_agents",
            return_value=[("Grok Build", self.home / ".grok")],
        ):
            section = check_config_syntax(self.aikito_dir, self.home)

        warnings = [finding for finding in section.findings if finding.status == "WARN"]
        self.assertTrue(any("'grok' is not registered" in f.message for f in warnings))
        self.assertTrue(all(f.fix_hint == "aikito doctor --fix" for f in warnings))


class CheckSymlinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

        (self.aikito_dir / "agents.toml").write_text(
            """
[agents.claude-code]
display_name = "Claude Code"
skills_path = ".claude/skills"
""".strip()
        )
        (self.aikito_dir / "skills.toml").write_text('skills = ["my-skill"]\n')
        skill_dir = self.aikito_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_skill_symlink_reports_fail(self) -> None:
        (self.home / ".claude" / "skills").mkdir(parents=True)

        section = check_symlinks(self.aikito_dir, self.home)
        fail_findings = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any(
                "Claude Code/my-skill: missing symlink" in f.message
                for f in fail_findings
            ),
            msg=f"Expected missing skill symlink failure, got: {[f.message for f in fail_findings]}",
        )


# ---------------------------------------------------------------------------
# Doctor check_orphans tests
# ---------------------------------------------------------------------------


class CheckOrphansTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

        (self.aikito_dir / "agents.toml").write_text(
            """
[agents.claude-code]
display_name = "Claude Code"
[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
""".strip()
        )
        (self.aikito_dir / "subagents.toml").write_text("[subagents]\n")
        (self.aikito_dir / "skills.toml").write_text("skills = []\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unmanaged_user_mcp_is_not_reported_as_orphan(self) -> None:
        # mcps/ defines active server 'active-server'
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "mcps/active-server.toml").write_text(
            """
transport = "remote"
url = "https://example.com/active"
agents = ["claude-code"]
""".strip()
        )
        # .claude.json contains 'active-server' AND 'user-custom-server'
        (self.home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "active-server": {
                            "type": "http",
                            "url": "https://example.com/active",
                        },
                        "user-custom-server": {
                            "type": "http",
                            "url": "https://user.com",
                        },
                    }
                }
            )
        )
        # mcp-state.json has NO record of user-custom-server
        state_dir = self.home / ".local/state/aikito"
        state_dir.mkdir(parents=True)
        (state_dir / "mcp-state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        "claude-code:active-server": {
                            "config_path": str(self.home / ".claude.json"),
                            "target_name": "active-server",
                        }
                    },
                }
            )
        )

        section = check_orphans(self.aikito_dir, self.home)
        fails = [f.message for f in section.findings if f.status == "FAIL"]
        self.assertEqual(
            fails,
            [],
            msg=f"User custom server should not be reported as orphan: {fails}",
        )

    def test_residual_previously_managed_mcp_is_reported_as_orphan(self) -> None:
        # mcps directory has NO servers defined
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        # .claude.json contains 'old-removed-server'
        (self.home / ".claude.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "old-removed-server": {
                            "type": "http",
                            "url": "https://example.com/old",
                        },
                    }
                }
            )
        )
        # mcp-state.json DOES record old-removed-server as previously managed
        state_dir = self.home / ".local/state/aikito"
        state_dir.mkdir(parents=True)
        (state_dir / "mcp-state.json").write_text(
            json.dumps(
                {
                    "version": 1,
                    "entries": {
                        "claude-code:old-removed-server": {
                            "config_path": str(self.home / ".claude.json"),
                            "target_name": "old-removed-server",
                        }
                    },
                }
            )
        )

        section = check_orphans(self.aikito_dir, self.home)
        fails = [f.message for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any("residual managed MCP entry 'old-removed-server'" in m for m in fails),
            msg=f"Expected residual orphan error, got: {fails}",
        )

    def test_orphan_skill_directory_is_reported_as_warning(self) -> None:
        skills_dir = self.aikito_dir / "skills"
        skills_dir.mkdir()
        (skills_dir / "orphan-skill-dir").mkdir()

        section = check_orphans(self.aikito_dir, self.home)
        warn_findings = [f for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any(
                "skills/orphan-skill-dir: empty directory, safe to delete" in f.message
                for f in warn_findings
            ),
            msg=f"Expected empty orphan skill directory warning, got: {[f.message for f in warn_findings]}",
        )
        self.assertTrue(
            any(
                "Remove the empty directory manually:" in f.fix_hint
                for f in warn_findings
            ),
            msg=f"Expected manual removal fix_hint for empty orphan skill, got: {[f.fix_hint for f in warn_findings]}",
        )

        # Add a file to make it non-empty
        (skills_dir / "orphan-skill-dir" / "SKILL.md").write_text("dummy")
        section2 = check_orphans(self.aikito_dir, self.home)
        warn_findings2 = [f for f in section2.findings if f.status == "WARN"]
        self.assertTrue(
            any(
                "skills/orphan-skill-dir: orphan skill directory (not in skills.toml or any project agent.toml)"
                in f.message
                for f in warn_findings2
            ),
            msg=f"Expected non-empty orphan skill directory warning, got: {[f.message for f in warn_findings2]}",
        )

    def test_dangling_symlink_in_orphan_skill_directory_not_reported_as_empty(
        self,
    ) -> None:
        skills_dir = self.aikito_dir / "skills"
        skills_dir.mkdir(exist_ok=True)
        orphan_dir = skills_dir / "orphan-with-dangling"
        orphan_dir.mkdir()

        # Create a dangling symlink inside orphan skill dir
        (orphan_dir / "broken.link").symlink_to(self.root / "nonexistent")

        section = check_orphans(self.aikito_dir, self.home)
        warn_findings = [f for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any(
                "skills/orphan-with-dangling: orphan skill directory" in f.message
                for f in warn_findings
            ),
            msg=f"Expected non-empty warning for orphan skill with dangling symlink, got: {[f.message for f in warn_findings]}",
        )
        self.assertFalse(
            any(
                "skills/orphan-with-dangling: empty directory" in f.message
                for f in warn_findings
            ),
            msg="Dangling symlink inside orphan skill dir must not be reported as empty directory",
        )


# ---------------------------------------------------------------------------
# Doctor check_environment basic tests
# ---------------------------------------------------------------------------


class CheckEnvironmentTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        # Create minimal projects dir so iteration doesn't crash
        (self.aikito_dir / "projects").mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_env_var_produces_ok_finding(self) -> None:
        with patch.dict("os.environ", {}, clear=False):
            env = {
                k: v for k, v in __import__("os").environ.items() if k != "AIKITO_DIR"
            }
            with patch.dict("os.environ", env, clear=True):
                section = check_environment(self.aikito_dir, self.home)
        ok_messages = [f.message for f in section.findings if f.status == "OK"]
        self.assertTrue(
            any("AIKITO_DIR not set" in m for m in ok_messages),
            msg=f"Expected AIKITO_DIR not set OK, got: {ok_messages}",
        )

    def test_wrong_env_var_produces_warn(self) -> None:
        wrong_path = str(self.root / "wrong")
        with patch.dict("os.environ", {"AIKITO_DIR": wrong_path}):
            section = check_environment(self.aikito_dir, self.home)
        warn_messages = [f.message for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any("AIKITO_DIR" in m for m in warn_messages),
            msg=f"Expected AIKITO_DIR warning, got: {warn_messages}",
        )


# ---------------------------------------------------------------------------
# Doctor check_drift and check_security tests
# ---------------------------------------------------------------------------


class CheckDriftAndSecurityTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()
        # Marker directory: simulate an installed Claude Code so canonical
        # detection passes without depending on the host PATH.
        (self.home / ".claude").mkdir()

        (self.aikito_dir / "agents.toml").write_text(
            """
[agents.claude-code]
display_name = "Claude Code"
[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
""".strip()
        )
        (self.aikito_dir / "skills.toml").write_text("skills = []\n")
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "mcps/test-server.toml").write_text(
            """
transport = "remote"
url = "https://example.com"
agents = ["claude-code"]
""".strip()
        )
        (self.aikito_dir / "subagents.toml").write_text("[subagents]\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unreadable_mcp_config_reports_fail_instead_of_crashing(self) -> None:
        cfg = self.home / ".claude.json"
        cfg.write_text("corrupt binary data")

        orig_read_text = Path.read_text

        def mock_read_text(self_path, *args, **kwargs):
            if self_path.name == ".claude.json":
                raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
            return orig_read_text(self_path, *args, **kwargs)

        with patch.object(Path, "read_text", side_effect=mock_read_text, autospec=True):
            from aikito_doctor import check_config_syntax, check_drift

            syntax_section = check_config_syntax(self.aikito_dir, self.home)
            drift_section = check_drift(self.aikito_dir, self.home)

            syntax_fails = [f for f in syntax_section.findings if f.status == "FAIL"]
            drift_fails = [f for f in drift_section.findings if f.status == "FAIL"]

            self.assertTrue(
                any(
                    "Claude Code config: read/parse error" in f.message
                    for f in syntax_fails
                ),
                msg=f"Expected syntax read error FAIL, got: {[f.message for f in syntax_fails]}",
            )
            self.assertTrue(
                any(
                    "claude-code × test-server: config parse error" in f.message
                    for f in drift_fails
                ),
                msg=f"Expected drift read error FAIL, got: {[f.message for f in drift_fails]}",
            )

    def test_check_security_runs_cleanly(self) -> None:
        (self.aikito_dir / ".gitignore").write_text("/.local/\n")

        section = check_security(self.aikito_dir, self.home)
        self.assertEqual(section.name, "Security")
        self.assertTrue(len(section.findings) > 0)

    def test_missing_managed_subagent_is_reported_in_drift(self) -> None:
        missing_target = self.home / ".copilot" / "agents" / "formatter.agent.md"
        plan = [
            PlanItem(
                agent_name="github-copilot",
                subagent_name="formatter",
                target_path=missing_target,
                action="CREATE",
                reason="Target file does not exist",
            )
        ]

        with patch("aikito_doctor.build_plan", return_value=(plan, {})):
            section = check_drift(self.aikito_dir, self.home)

        failures = [finding for finding in section.findings if finding.status == "FAIL"]
        self.assertTrue(
            any(
                "github-copilot/formatter: managed subagent missing" in finding.message
                and finding.fix_hint == "aikito sync subagents"
                for finding in failures
            )
        )

    def test_missing_mcp_credential_suggests_refreshing_shell(self) -> None:
        spec = AgentSpec(
            agent="agy",
            server="atlassian-rovo",
            config_path=self.home / ".gemini/config/mcp_config.json",
            config_format="agy_json",
            target_name="atlassian-rovo",
            desired={"headers": {}},
            missing_credential_env="ATLASSIAN_ROVO_TOKEN",
        )

        with (
            patch("aikito_doctor.load_agent_specs", return_value=[spec]),
            patch("aikito_doctor.evaluate_spec_status", return_value="DRIFT"),
            patch("aikito_doctor.build_plan", return_value=([], {})),
        ):
            section = check_drift(self.aikito_dir, self.home)

        warnings = [finding for finding in section.findings if finding.status == "WARN"]
        self.assertTrue(
            any(
                "credential-dependent MCP config differs" in finding.message
                and "ATLASSIAN_ROVO_TOKEN" in finding.message
                and finding.fix_hint == "open a new shell and run: aikito doctor"
                for finding in warnings
            )
        )
        self.assertFalse(
            any(finding.fix_hint == "aikito sync mcp" for finding in warnings)
        )


# ---------------------------------------------------------------------------
# run_doctor integration test on the real workspace
# ---------------------------------------------------------------------------


class RunDoctorIntegrationTest(unittest.TestCase):
    def test_run_doctor_returns_report(self) -> None:
        # Run against the actual aikito workspace (read-only)
        report = run_doctor(ROOT, Path(tempfile.gettempdir()))
        self.assertIsInstance(report, DoctorReport)
        self.assertTrue(len(report.sections) == 8)
        # All section names present
        names = {s.name for s in report.sections}
        for expected in (
            "Symlinks",
            "Orphans",
            "Memory",
            "Configuration",
            "Drift",
            "Security",
            "Environment",
        ):
            self.assertIn(expected, names)

    def test_memory_integrity_cross_note_check_does_not_crash_on_real_workspace(
        self,
    ) -> None:
        # Real-workspace run is a smoke test only — it may legitimately surface
        # existing dangling links, which is the point of the check, not a bug.
        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(ROOT, Path(tempfile.gettempdir()))
        self.assertEqual(section.name, "Memory")


class CrossNoteWikilinkTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.home.mkdir()
        mem = self.aikito_dir / "memory"
        self.notes_dir = mem / "notes"
        self.notes_dir.mkdir(parents=True)
        (mem / "index.md").write_text("- [[note-a]]\n- [[note-b]]\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dangling_cross_note_link_reported(self) -> None:
        from aikito_doctor import check_memory_integrity

        (self.notes_dir / "note-a.md").write_text("See [[note-b]] and [[ghost-note]].")
        (self.notes_dir / "note-b.md").write_text("No links here.")
        section = check_memory_integrity(self.aikito_dir, self.home)
        fails = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any("note-a" in f.message and "ghost-note" in f.message for f in fails),
            msg=f"Expected dangling cross-note link FAIL, got: {[f.message for f in fails]}",
        )

    def test_valid_cross_note_link_produces_no_fail(self) -> None:
        from aikito_doctor import check_memory_integrity

        (self.notes_dir / "note-a.md").write_text("See [[note-b]].")
        (self.notes_dir / "note-b.md").write_text("No links here.")
        section = check_memory_integrity(self.aikito_dir, self.home)
        fails = [
            f
            for f in section.findings
            if f.status == "FAIL" and "links to" in f.message
        ]
        self.assertEqual(fails, [])


# ---------------------------------------------------------------------------
# check_memory_integrity (freshness) tests
# ---------------------------------------------------------------------------


class CheckMemoryFreshnessTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.notes_dir = self.aikito_dir / "memory" / "notes"
        self.notes_dir.mkdir(parents=True)
        self.home = self.root / "home"
        self.home.mkdir()

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_no_notes_reports_ok(self) -> None:
        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(self.aikito_dir, self.home)
        self.assertEqual(section.name, "Memory")
        self.assertTrue(all(f.status == "OK" for f in section.findings))

    def test_untracked_note_is_skipped_not_flagged(self) -> None:
        # A note with no git history (no repo at all here) should not crash
        # or be reported, since staleness can't be determined without history.
        (self.notes_dir / "orphan.md").write_text("content")
        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(self.aikito_dir, self.home)
        warns = [f for f in section.findings if f.status == "WARN"]
        self.assertEqual(warns, [])

    def test_stale_note_detected_via_git_history(self) -> None:
        import subprocess as sp

        sp.run(["git", "init", str(self.aikito_dir)], check=True, capture_output=True)
        sp.run(
            [
                "git",
                "-C",
                str(self.aikito_dir),
                "config",
                "user.email",
                "t@example.com",
            ],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "config", "user.name", "t"],
            check=True,
            capture_output=True,
        )

        note = self.notes_dir / "old-note.md"
        note.write_text("stale content")
        old_date = "2020-01-01T00:00:00"
        env = {"GIT_AUTHOR_DATE": old_date, "GIT_COMMITTER_DATE": old_date}
        import os

        full_env = {**os.environ, **env}
        sp.run(
            ["git", "-C", str(self.aikito_dir), "add", "memory/notes/old-note.md"],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "commit", "-m", "add old note"],
            check=True,
            capture_output=True,
            env=full_env,
        )

        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(self.aikito_dir, self.home)
        warns = [f for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any("old-note" in f.message for f in warns),
            msg=f"Expected old-note staleness WARN, got: {[f.message for f in section.findings]}",
        )

    def test_fresh_note_not_flagged(self) -> None:
        import subprocess as sp

        sp.run(["git", "init", str(self.aikito_dir)], check=True, capture_output=True)
        sp.run(
            [
                "git",
                "-C",
                str(self.aikito_dir),
                "config",
                "user.email",
                "t@example.com",
            ],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "config", "user.name", "t"],
            check=True,
            capture_output=True,
        )

        note = self.notes_dir / "new-note.md"
        note.write_text("fresh content")
        sp.run(
            ["git", "-C", str(self.aikito_dir), "add", "memory/notes/new-note.md"],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "commit", "-m", "add new note"],
            check=True,
            capture_output=True,
        )

        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(self.aikito_dir, self.home)
        warns = [f for f in section.findings if f.status == "WARN"]
        self.assertEqual(warns, [])

    def test_fresh_note_project_override_uses_actual_stale_days(self) -> None:
        import shutil
        import subprocess as sp

        shutil.rmtree(self.notes_dir, ignore_errors=True)

        sp.run(["git", "init", str(self.aikito_dir)], check=True, capture_output=True)
        sp.run(
            [
                "git",
                "-C",
                str(self.aikito_dir),
                "config",
                "user.email",
                "t@example.com",
            ],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "config", "user.name", "t"],
            check=True,
            capture_output=True,
        )

        proj_dir = self.aikito_dir / "projects" / "p1"
        proj_notes = proj_dir / "memory" / "notes"
        proj_notes.mkdir(parents=True)
        (proj_dir / "agent.toml").write_text("[memory]\nstale_days = 7\n")
        (proj_notes / "proj-note.md").write_text("content")

        sp.run(
            ["git", "-C", str(self.aikito_dir), "add", "projects/p1"],
            check=True,
            capture_output=True,
        )
        sp.run(
            ["git", "-C", str(self.aikito_dir), "commit", "-m", "add proj note"],
            check=True,
            capture_output=True,
        )

        from aikito_doctor import check_memory_integrity

        section = check_memory_integrity(self.aikito_dir, self.home)
        ok_messages = [f.message for f in section.findings if f.status == "OK"]
        self.assertIn("No memory notes older than 7 days", ok_messages)


class DoctorJsonSerialisableTest(unittest.TestCase):
    def test_doctor_json_serialisable(self) -> None:
        report = run_doctor(ROOT, Path(tempfile.gettempdir()))
        data = {
            "sections": [
                {
                    "name": s.name,
                    "findings": [
                        {
                            "status": f.status,
                            "message": f.message,
                            "fix_hint": f.fix_hint,
                        }
                        for f in s.findings
                    ],
                }
                for s in report.sections
            ],
            "fail_count": report.fail_count,
            "warn_count": report.warn_count,
        }
        # Must not raise
        serialised = json.dumps(data)
        parsed = json.loads(serialised)
        self.assertEqual(parsed["fail_count"], report.fail_count)


class MemoryFormatAndNamingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.home.mkdir()
        mem = self.aikito_dir / "memory"
        self.notes_dir = mem / "notes"
        self.notes_dir.mkdir(parents=True)
        self.index_file = mem / "index.md"
        self.index_file.write_text("- [[valid-note|Valid Note]]\n")
        (self.notes_dir / "valid-note.md").write_text("# Valid Note")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_invalid_note_filename_fails(self) -> None:
        from aikito_doctor import check_memory_integrity

        (self.notes_dir / "Invalid_Name.md").write_text("# Invalid")
        (self.notes_dir / ("a" * 51 + ".md")).write_text("# Too Long")
        self.index_file.write_text(
            "- [[valid-note|Valid Note]]\n- [[Invalid_Name|Invalid]]\n- [["
            + "a" * 51
            + "|Too Long]]\n"
        )
        section = check_memory_integrity(self.aikito_dir, self.home)
        fails = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any(
                "Invalid_Name" in f.message and "invalid filename" in f.message
                for f in fails
            )
        )
        self.assertTrue(
            any(
                "a" * 51 in f.message and "invalid filename" in f.message for f in fails
            )
        )

    def test_non_standard_index_entry_warns(self) -> None:
        from aikito_doctor import check_memory_integrity

        (self.notes_dir / "note-b.md").write_text("# Note B")
        self.index_file.write_text(
            "- [[valid-note|Valid Note]]\n- [[note-b]] — Trailing Description\n"
        )
        section = check_memory_integrity(self.aikito_dir, self.home)
        warns = [f for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any(
                "note-b" in f.message and "non-standard format" in f.message
                for f in warns
            )
        )


class DoctorFixesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.home.mkdir()
        mem = self.aikito_dir / "memory"
        self.notes_dir = mem / "notes"
        self.notes_dir.mkdir(parents=True)
        self.index_file = mem / "index.md"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_run_doctor_fixes_adds_missing_agent_fields_without_overwrite(self) -> None:
        from aikito_doctor import run_doctor_fixes

        self.aikito_dir.mkdir(exist_ok=True)
        agents_path = self.aikito_dir / "agents.toml"
        agents_path.write_text(
            '[agents.codex]\ndisplay_name = "Custom Codex"\n', encoding="utf-8"
        )

        fixes = run_doctor_fixes(self.aikito_dir, self.home)

        with agents_path.open("rb") as config_file:
            codex = tomllib.load(config_file)["agents"]["codex"]
        self.assertEqual(codex["display_name"], "Custom Codex")
        self.assertEqual(codex["project_instruction_path"], "AGENTS.md")
        self.assertEqual(codex["runner"]["command"][0], "codex")
        self.assertTrue(any("project_instruction_path" in fix for fix in fixes))

    def test_run_doctor_fixes_registers_installed_supported_agent(self) -> None:
        from aikito_doctor import run_doctor_fixes

        self.aikito_dir.mkdir(exist_ok=True)
        agents_path = self.aikito_dir / "agents.toml"
        agents_path.write_text("[agents]\n", encoding="utf-8")
        (self.home / ".grok").mkdir()

        with patch("aikito_init.shutil.which", return_value=None):
            fixes = run_doctor_fixes(self.aikito_dir, self.home)

        with agents_path.open("rb") as config_file:
            grok = tomllib.load(config_file)["agents"]["grok"]
        self.assertEqual(grok["project_instruction_path"], "AGENTS.md")
        self.assertEqual(grok["runner"]["command"][0], "grok")
        self.assertTrue(any("agents.grok" in fix for fix in fixes))

    def test_run_doctor_fixes_reconciles_memory_index(self) -> None:
        from aikito_doctor import run_doctor_fixes

        # 1. Existing note
        (self.notes_dir / "existing-note.md").write_text("# Existing Title\nContent")
        # 2. Missing note (not in index)
        (self.notes_dir / "missing-note.md").write_text("# Missing Title\nContent")
        # 3. Bare note (format normalization)
        (self.notes_dir / "bare-note.md").write_text("# Bare Note Title\nContent")
        # 4. Old format note
        (self.notes_dir / "old-format.md").write_text("# Old Title\nContent")

        # index.md with dangling note, bare note, old format note, but missing "missing-note"
        self.index_file.write_text(
            "- [[existing-note|Existing Title]]\n"
            "- [[ghost-note|Dangling Note]]\n"
            "- [[bare-note]]\n"
            "- [[old-format]] — Custom Description\n"
        )

        fixes = run_doctor_fixes(self.aikito_dir)
        self.assertTrue(any("ghost-note" in f for f in fixes))
        self.assertTrue(any("bare-note" in f for f in fixes))
        self.assertTrue(any("old-format" in f for f in fixes))

        updated_index = self.index_file.read_text()
        self.assertNotIn("ghost-note", updated_index)
        self.assertIn("- [[existing-note|Existing Title]]", updated_index)
        self.assertIn("- [[bare-note|Bare Note Title]]", updated_index)
        self.assertIn("- [[old-format|Old Title]]", updated_index)
        # Missing note is reported by doctor diagnostics, not blindly appended to preserve index structure
        self.assertNotIn("missing-note", updated_index)


if __name__ == "__main__":
    unittest.main()
