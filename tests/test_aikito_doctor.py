"""Tests for aikito_link and aikito_doctor modules."""

import json
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_link import SymlinkVerdict, classify_symlink, symlink_verdict_to_status  # noqa: E402
from aikito_doctor import (  # noqa: E402
    check_config_syntax,
    check_drift,
    check_environment,
    check_orphans,
    check_security,
    check_symlinks,
    run_doctor,
)
from aikito_render import DoctorFinding, DoctorReport, DoctorSection, render_doctor_report  # noqa: E402


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
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.WRONG_TARGET), "CONFLICT")
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.NOT_SYMLINK), "CONFLICT")
        self.assertEqual(symlink_verdict_to_status(SymlinkVerdict.MISSING), "MISSING")


# ---------------------------------------------------------------------------
# render_doctor_report tests
# ---------------------------------------------------------------------------

class RenderDoctorReportTest(unittest.TestCase):
    def _make_report(self) -> DoctorReport:
        return DoctorReport(sections=[
            DoctorSection(name="Symlinks", findings=[
                DoctorFinding(status="OK", message="Global instructions OK"),
                DoctorFinding(status="FAIL", message="Project foo: dangling symlink",
                              fix_hint="aikito sync project foo"),
            ]),
            DoctorSection(name="Environment", findings=[
                DoctorFinding(status="WARN", message="opencode not found in $PATH"),
            ]),
        ])

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
        rendered = render_doctor_report(report, is_tty=False, no_color=True, use_unicode=False)
        self.assertIn("[OK]", rendered)
        self.assertIn("[FAIL]", rendered)
        self.assertIn("[WARN]", rendered)
        self.assertNotIn("╭", rendered)

    def test_all_ok_summary(self) -> None:
        report = DoctorReport(sections=[
            DoctorSection(name="Test", findings=[
                DoctorFinding(status="OK", message="Everything fine"),
            ])
        ])
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        self.assertIn("All checks passed", rendered)

    def test_box_right_border_alignment(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=True, no_color=True)
        lines = rendered.splitlines()
        # Find all title box header lines (starting with ╭, │, or ╰)
        box_lines = [l for l in lines if l.startswith("╭") or l.startswith("│") or l.startswith("╰")]
        widths = [len(l) for l in box_lines]
        # All title box header lines across ALL sections must have identical width (max_title_w + 5)!
        self.assertEqual(len(set(widths)), 1, msg=f"Title box lines have inconsistent widths: {widths}")
        self.assertEqual(widths[0], 16)  # len("Environment") = 11 -> inner 14 -> total 16
        for l in box_lines:
            self.assertTrue(l.endswith("╮") or l.endswith("│") or l.endswith("╯"))

    def test_use_unicode_defaults_to_true_even_if_not_tty(self) -> None:
        report = self._make_report()
        rendered = render_doctor_report(report, is_tty=False, no_color=True, use_unicode=True)
        self.assertIn("╭", rendered)
        self.assertIn("│", rendered)
        self.assertIn("✓", rendered)

    def test_fail_count(self) -> None:
        report = self._make_report()
        self.assertEqual(report.fail_count, 1)
        self.assertEqual(report.warn_count, 1)


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
        (self.aikito_dir / "skills.toml").write_text('skills = []\n')
        (self.aikito_dir / "mcps.toml").write_text("[servers]\n")
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
            msg=f"Expected agents.toml missing failure",
        )


class CheckSymlinksTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.aikito_dir.mkdir()
        self.home = self.root / "home"
        self.home.mkdir()

        (self.aikito_dir / "agents.toml").write_text("""
[agents.claude-code]
display_name = "Claude Code"
skills_path = ".claude/skills"
""".strip())
        (self.aikito_dir / "skills.toml").write_text('skills = ["my-skill"]\n')
        skill_dir = self.aikito_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("# My Skill")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_missing_skill_symlink_reports_fail(self) -> None:
        (self.home / ".claude" / "skills").mkdir(parents=True)

        from aikito_doctor import check_symlinks
        section = check_symlinks(self.aikito_dir, self.home)
        fail_findings = [f for f in section.findings if f.status == "FAIL"]
        self.assertTrue(
            any("Claude Code/my-skill: missing symlink" in f.message for f in fail_findings),
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

        (self.aikito_dir / "agents.toml").write_text("""
[agents.claude-code]
display_name = "Claude Code"
[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
""".strip())
        (self.aikito_dir / "subagents.toml").write_text("[subagents]\n")
        (self.aikito_dir / "skills.toml").write_text("skills = []\n")

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_unmanaged_user_mcp_is_not_reported_as_orphan(self) -> None:
        # mcps.toml defines active server 'active-server'
        (self.aikito_dir / "mcps.toml").write_text("""
[servers.active-server]
transport = "remote"
url = "https://example.com/active"
agents = ["claude-code"]
""".strip())
        # .claude.json contains 'active-server' AND 'user-custom-server'
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "active-server": {"type": "http", "url": "https://example.com/active"},
                "user-custom-server": {"type": "http", "url": "https://user.com"},
            }
        }))
        # mcp-state.json has NO record of user-custom-server
        state_dir = self.home / ".local/state/aikito"
        state_dir.mkdir(parents=True)
        (state_dir / "mcp-state.json").write_text(json.dumps({
            "version": 1,
            "entries": {
                "claude-code:active-server": {
                    "config_path": str(self.home / ".claude.json"),
                    "target_name": "active-server",
                }
            }
        }))

        section = check_orphans(self.aikito_dir, self.home)
        fails = [f.message for f in section.findings if f.status == "FAIL"]
        self.assertEqual(fails, [], msg=f"User custom server should not be reported as orphan: {fails}")

    def test_residual_previously_managed_mcp_is_reported_as_orphan(self) -> None:
        # mcps.toml has NO servers defined
        (self.aikito_dir / "mcps.toml").write_text("[servers]\n")
        # .claude.json contains 'old-removed-server'
        (self.home / ".claude.json").write_text(json.dumps({
            "mcpServers": {
                "old-removed-server": {"type": "http", "url": "https://example.com/old"},
            }
        }))
        # mcp-state.json DOES record old-removed-server as previously managed
        state_dir = self.home / ".local/state/aikito"
        state_dir.mkdir(parents=True)
        (state_dir / "mcp-state.json").write_text(json.dumps({
            "version": 1,
            "entries": {
                "claude-code:old-removed-server": {
                    "config_path": str(self.home / ".claude.json"),
                    "target_name": "old-removed-server",
                }
            }
        }))

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
            any("skills/orphan-skill-dir: empty directory, safe to delete" in f.message for f in warn_findings),
            msg=f"Expected empty orphan skill directory warning, got: {[f.message for f in warn_findings]}",
        )
        self.assertTrue(
            any("rm -rf" in f.fix_hint for f in warn_findings),
            msg=f"Expected rm -rf fix_hint for empty orphan skill, got: {[f.fix_hint for f in warn_findings]}",
        )

        # Add a file to make it non-empty
        (skills_dir / "orphan-skill-dir" / "SKILL.md").write_text("dummy")
        section2 = check_orphans(self.aikito_dir, self.home)
        warn_findings2 = [f for f in section2.findings if f.status == "WARN"]
        self.assertTrue(
            any("skills/orphan-skill-dir: orphan skill directory (not in skills.toml or any project agent.toml)" in f.message for f in warn_findings2),
            msg=f"Expected non-empty orphan skill directory warning, got: {[f.message for f in warn_findings2]}",
        )

    def test_dangling_symlink_in_orphan_skill_directory_not_reported_as_empty(self) -> None:
        skills_dir = self.aikito_dir / "skills"
        skills_dir.mkdir(exist_ok=True)
        orphan_dir = skills_dir / "orphan-with-dangling"
        orphan_dir.mkdir()
        
        # Create a dangling symlink inside orphan skill dir
        (orphan_dir / "broken.link").symlink_to(self.root / "nonexistent")

        section = check_orphans(self.aikito_dir, self.home)
        warn_findings = [f for f in section.findings if f.status == "WARN"]
        self.assertTrue(
            any("skills/orphan-with-dangling: orphan skill directory" in f.message for f in warn_findings),
            msg=f"Expected non-empty warning for orphan skill with dangling symlink, got: {[f.message for f in warn_findings]}",
        )
        self.assertFalse(
            any("skills/orphan-with-dangling: empty directory" in f.message for f in warn_findings),
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
            env = {k: v for k, v in __import__("os").environ.items() if k != "AIKITO_DIR"}
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

        (self.aikito_dir / "agents.toml").write_text("""
[agents.claude-code]
display_name = "Claude Code"
[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
""".strip())
        (self.aikito_dir / "skills.toml").write_text("skills = []\n")
        (self.aikito_dir / "mcps.toml").write_text("""
[servers.test-server]
transport = "remote"
url = "https://example.com"
agents = ["claude-code"]
""".strip())
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
                any("Claude Code config: read/parse error" in f.message for f in syntax_fails),
                msg=f"Expected syntax read error FAIL, got: {[f.message for f in syntax_fails]}",
            )
            self.assertTrue(
                any("claude-code × test-server: config parse error" in f.message for f in drift_fails),
                msg=f"Expected drift read error FAIL, got: {[f.message for f in drift_fails]}",
            )

    def test_check_security_runs_cleanly(self) -> None:
        (self.aikito_dir / ".gitignore").write_text("/.local/\n")
        from aikito_doctor import check_security
        section = check_security(self.aikito_dir, self.home)
        self.assertEqual(section.name, "Security")
        self.assertTrue(len(section.findings) > 0)


# ---------------------------------------------------------------------------
# run_doctor integration test on the real workspace
# ---------------------------------------------------------------------------

class RunDoctorIntegrationTest(unittest.TestCase):
    def test_run_doctor_returns_report(self) -> None:
        # Run against the actual aikito workspace (read-only)
        report = run_doctor(ROOT, Path(tempfile.gettempdir()))
        self.assertIsInstance(report, DoctorReport)
        self.assertTrue(len(report.sections) == 6)
        # All section names present
        names = {s.name for s in report.sections}
        for expected in ("Symlinks", "Orphans", "Configuration", "Drift", "Security", "Environment"):
            self.assertIn(expected, names)

    def test_doctor_json_serialisable(self) -> None:
        report = run_doctor(ROOT, Path(tempfile.gettempdir()))
        data = {
            "sections": [
                {
                    "name": s.name,
                    "findings": [
                        {"status": f.status, "message": f.message, "fix_hint": f.fix_hint}
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


if __name__ == "__main__":
    unittest.main()

