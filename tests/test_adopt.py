import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aikito import cli as AIKITO_CLI
from aikito.adopt import (
    AdoptExecutionResult,
    AdoptFilePlan,
    AdoptRequest,
    AdoptSkipError,
    apply_adopt_skips,
    build_adopt_plan,
    collect_adopt_findings,
    execute_adopt_plan,
    execute_adoption,
    summarize_adopt_plan,
)
from aikito.templating import (
    load_agents_template,
    load_default_memory_instruction,
    load_global_agents_template,
)
from aikito.mcp import load_agent_specs
from aikito.subagent import load_subagent_definitions

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MEMORY_INSTRUCTION = load_default_memory_instruction()
GLOBAL_AGENTS_TEMPLATE = load_global_agents_template()


class AikitoAdoptTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp_dir.name)
        self.fake_home = self.root / "home"
        self.target_path = self.root / "aikito"

        self.fake_home.mkdir(parents=True)
        self.target_path.mkdir(parents=True)

        (self.target_path / "global").mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_adopt_instructions_merge_when_identical(self) -> None:
        # Create identical instructions in codex and claude-code
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text(
            "Same Global Instructions", encoding="utf-8"
        )

        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text(
            "Same Global Instructions", encoding="utf-8"
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertFalse(plan.instructions.has_conflict)
        self.assertEqual(plan.instructions.merged_content, "Same Global Instructions")

        # Execute adopt without dry-run -> should write file
        success = execute_adoption(plan, dry_run=False)
        self.assertTrue(success)

        target_file = self.target_path / "global" / "AGENTS.md"
        self.assertTrue(target_file.is_file())
        self.assertEqual(
            target_file.read_text(encoding="utf-8"), "Same Global Instructions"
        )

    def test_adopt_instructions_conflict_when_different(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Codex Rules", encoding="utf-8")

        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("Claude Rules", encoding="utf-8")

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertTrue(plan.instructions.has_conflict)

    def test_adopt_appends_default_memory_instruction_to_user_instructions(
        self,
    ) -> None:
        (self.target_path / "global" / "AGENTS.md").write_text(
            GLOBAL_AGENTS_TEMPLATE, encoding="utf-8"
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text(
            "# User Instructions\n\n- Keep existing behavior.\n", encoding="utf-8"
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertFalse(plan.instructions.has_conflict)
        self.assertEqual(
            plan.instructions.merged_content,
            "# User Instructions\n\n- Keep existing behavior.\n\n"
            + DEFAULT_MEMORY_INSTRUCTION,
        )

        execute_adoption(plan, dry_run=False)
        merged = (self.target_path / "global" / "AGENTS.md").read_text(encoding="utf-8")
        self.assertIn("Keep existing behavior", merged)
        self.assertEqual(merged.count("## Persistent Memory"), 1)

    def test_adopt_default_memory_instruction_merge_is_idempotent(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        source = codex_dir / "AGENTS.md"
        source.write_text("# User Instructions\n", encoding="utf-8")
        target = self.target_path / "global" / "AGENTS.md"
        target.write_text(GLOBAL_AGENTS_TEMPLATE, encoding="utf-8")

        first_plan = build_adopt_plan(self.target_path, self.fake_home)
        execute_adoption(first_plan, dry_run=False)
        first_content = target.read_text(encoding="utf-8")

        second_plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertFalse(second_plan.instructions.has_conflict)
        execute_adoption(second_plan, dry_run=False)

        self.assertEqual(target.read_text(encoding="utf-8"), first_content)
        self.assertEqual(first_content.count("## Persistent Memory"), 1)

    def test_adopt_does_not_duplicate_existing_default_memory_instruction(
        self,
    ) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        imported = f"# User Instructions\n\n{DEFAULT_MEMORY_INSTRUCTION}"
        (codex_dir / "AGENTS.md").write_text(imported, encoding="utf-8")
        (self.target_path / "global" / "AGENTS.md").write_text(
            GLOBAL_AGENTS_TEMPLATE, encoding="utf-8"
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertFalse(plan.instructions.has_conflict)
        self.assertEqual(
            plan.instructions.merged_content.count("## Persistent Memory"), 1
        )

    def test_adopt_reports_conflict_with_custom_canonical_instructions(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("# Imported\n", encoding="utf-8")
        canonical = self.target_path / "global" / "AGENTS.md"
        canonical.write_text("# Existing Canonical Rules\n", encoding="utf-8")

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertTrue(plan.instructions.has_conflict)
        self.assertIsNone(plan.instructions.merged_content)
        self.assertFalse(execute_adoption(plan, dry_run=False))
        self.assertEqual(
            canonical.read_text(encoding="utf-8"), "# Existing Canonical Rules\n"
        )

    def test_adopt_apply_blocks_all_writes_when_instructions_conflict(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Codex Rules\n", encoding="utf-8")
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("Claude Rules\n", encoding="utf-8")
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"example": {"command": "example"}}}),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertFalse(execute_adoption(plan, dry_run=False))
        self.assertFalse((self.target_path / "mcps" / "example.toml").exists())

    def test_empty_adopt_plan_is_a_no_op_without_apply_hint(self) -> None:
        plan = build_adopt_plan(self.target_path, self.fake_home)
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertTrue(execute_adoption(plan, dry_run=True))

        rendered = output.getvalue()
        self.assertIn("No adoptable Agent configuration found", rendered)
        self.assertNotIn("adopt --apply", rendered)

    def test_adopt_summary_counts_pending_resources(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.example]\ncommand = "example"\n', encoding="utf-8"
        )

        summary = summarize_adopt_plan(
            build_adopt_plan(self.target_path, self.fake_home)
        )

        self.assertEqual(summary.instruction_updates, 1)
        self.assertEqual(summary.mcp_imports, 1)
        self.assertEqual(summary.subagent_imports, 0)
        self.assertEqual(summary.conflicts, 0)
        self.assertEqual(summary.errors, 0)
        self.assertEqual(summary.skipped, 0)

    def test_adopt_invalid_mcp_blocks_all_writes_before_backup(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers."../escape"]\ncommand = "example"\n', encoding="utf-8"
        )
        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(summarize_adopt_plan(plan).errors, 1)
        self.assertFalse(execute_adoption(plan, dry_run=False))
        self.assertFalse((self.target_path / "global" / "AGENTS.md").exists())
        self.assertFalse((self.fake_home / ".aikito" / "backups").exists())
        self.assertFalse((self.target_path.parent / "escape.toml").exists())

    def test_adopt_malformed_source_blocks_all_writes_before_backup(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text("invalid = [\n", encoding="utf-8")
        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(summarize_adopt_plan(plan).errors, 1)
        self.assertFalse(execute_adoption(plan, dry_run=False))
        self.assertFalse((self.target_path / "global" / "AGENTS.md").exists())
        self.assertFalse((self.fake_home / ".aikito" / "backups").exists())

    def test_adopt_malformed_agent_registry_blocks_all_writes(self) -> None:
        (self.target_path / "agents.toml").write_text(
            "[agents.codex\n", encoding="utf-8"
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"example": {"command": "example"}}}),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(len(plan.errors), 1)
        self.assertEqual(plan.errors[0].resource, "agents")
        self.assertFalse(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertFalse((self.target_path / "mcps").exists())

    def test_adopt_skip_invalid_mcp_applies_remaining_resources(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers."../escape"]\ncommand = "example"\n', encoding="utf-8"
        )
        plan = apply_adopt_skips(
            build_adopt_plan(self.target_path, self.fake_home),
            ["mcp/../escape"],
        )

        self.assertTrue(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertEqual(summarize_adopt_plan(plan).skipped, 1)
        self.assertTrue((self.target_path / "global" / "AGENTS.md").is_file())
        self.assertFalse((self.target_path.parent / "escape.toml").exists())

    def test_adopt_skip_instructions_allows_other_resources(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Codex Rules\n", encoding="utf-8")
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text("Claude Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.example]\ncommand = "example"\n', encoding="utf-8"
        )
        plan = apply_adopt_skips(
            build_adopt_plan(self.target_path, self.fake_home), ["instructions"]
        )

        self.assertTrue(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertFalse((self.target_path / "global" / "AGENTS.md").exists())
        self.assertTrue((self.target_path / "mcps" / "example.toml").is_file())

    def test_adopt_mcp_canonicalizes_underscore_to_existing_hyphen_server(self) -> None:
        (self.target_path / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )
        mcps_dir = self.target_path / "mcps"
        mcps_dir.mkdir(parents=True)
        (mcps_dir / "atlassian-rovo.toml").write_text(
            'transport = "remote"\nurl = "https://mcp.atlassian.com/v2/mcp"\nagents = ["claude-code"]\n',
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.atlassian_rovo]\nurl = "https://mcp.atlassian.com/v2/mcp"\n',
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)
        self.assertEqual(plan.mcp_servers[0].server_name, "atlassian-rovo")
        self.assertEqual(plan.mcp_servers[0].agents, ["codex"])

        summary = summarize_adopt_plan(plan)
        self.assertEqual(summary.mcp_imports, 0)

        self.assertTrue(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertFalse((mcps_dir / "atlassian_rovo.toml").exists())
        self.assertTrue((mcps_dir / "atlassian-rovo.toml").exists())

    def test_adopt_mcp_unifies_hyphen_and_underscore_across_agents(self) -> None:
        (self.target_path / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps(
                {"mcpServers": {"atlassian-rovo": {"url": "https://example.com"}}}
            ),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.atlassian_rovo]\nurl = "https://example.com"\n',
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)
        self.assertEqual(plan.mcp_servers[0].server_name, "atlassian-rovo")
        self.assertEqual(sorted(plan.mcp_servers[0].agents), ["claude-code", "codex"])

        summary = summarize_adopt_plan(plan)
        self.assertEqual(summary.mcp_imports, 1)

        self.assertTrue(execute_adoption(plan, dry_run=False, verbose=False))
        mcps_dir = self.target_path / "mcps"
        self.assertTrue((mcps_dir / "atlassian-rovo.toml").exists())
        self.assertFalse((mcps_dir / "atlassian_rovo.toml").exists())

    def test_adopt_mcp_keeps_verbatim_hyphen_and_underscore_names_distinct(
        self,
    ) -> None:
        (self.target_path / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )
        (self.fake_home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"example-server": {"command": "first"}}}),
            encoding="utf-8",
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"example_server": {"command": "second"}}}),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(
            {server.server_name for server in plan.mcp_servers},
            {"example-server", "example_server"},
        )
        self.assertEqual(plan.errors, ())

    def test_adopt_mcp_blocks_different_urls_after_name_mapping(self) -> None:
        (self.target_path / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps({"mcpServers": {"company-api": {"url": "https://claude.test"}}}),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.company_api]\nurl = "https://codex.test"\n',
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(len(plan.errors), 1)
        self.assertEqual(plan.errors[0].code, "adopt.mcp_conflict")
        self.assertFalse(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertFalse((self.target_path / "mcps").exists())

    def test_adopt_mcp_ignores_agent_specific_fields_when_urls_match(self) -> None:
        (self.target_path / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "atlassian-rovo": {
                            "type": "http",
                            "url": "https://mcp.atlassian.com/v2/mcp",
                            "headers": {
                                "Authorization": "${ATLASSIAN_MCP_AUTHORIZATION}"
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            """
[mcp_servers.atlassian_rovo]
url = "https://mcp.atlassian.com/v2/mcp"
env_http_headers = { Authorization = "ATLASSIAN_MCP_AUTHORIZATION" }
""".lstrip(),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)

        self.assertEqual(plan.errors, ())
        self.assertEqual(len(plan.mcp_servers), 1)
        self.assertEqual(plan.mcp_servers[0].server_name, "atlassian-rovo")
        self.assertEqual(sorted(plan.mcp_servers[0].agents), ["claude-code", "codex"])

    def test_adopt_mcp_skips_agent_builtin_servers(self) -> None:
        (self.target_path / "agents.toml").write_text(
            """
[agents.codex]
display_name = "Codex"
[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
builtin_mcps = ["openaiDeveloperDocs"]
""".lstrip(),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.openaiDeveloperDocs]\nurl = "https://developers.openai.com/mcp"\n',
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 0)
        self.assertEqual(plan.builtin_mcps, (("openaiDeveloperDocs", "codex"),))

        summary = summarize_adopt_plan(plan)
        self.assertEqual(summary.mcp_imports, 0)

        self.assertTrue(execute_adoption(plan, dry_run=False, verbose=False))
        self.assertFalse(
            (self.target_path / "mcps" / "openaiDeveloperDocs.toml").exists()
        )

    def test_adopt_mcp_reports_builtin_skip_when_plan_has_no_changes(self) -> None:
        (self.target_path / "agents.toml").write_text(
            """
[agents.codex]
display_name = "Codex"
[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
builtin_mcps = ["openaiDeveloperDocs"]
""".lstrip(),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.openaiDeveloperDocs]\nurl = "https://developers.openai.com/mcp"\n',
            encoding="utf-8",
        )
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertTrue(
                execute_adoption(
                    build_adopt_plan(self.target_path, self.fake_home),
                    dry_run=True,
                    verbose=True,
                )
            )

        self.assertIn(
            "[SKIP MCP] Server 'openaiDeveloperDocs' is built-in to codex",
            output.getvalue(),
        )

    def test_adopt_mcp_keeps_builtin_server_when_shared_with_another_agent(
        self,
    ) -> None:
        (self.target_path / "agents.toml").write_text(
            """
[agents.codex]
display_name = "Codex"
[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
builtin_mcps = ["sharedServer"]
[agents.claude-code]
display_name = "Claude Code"
[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
""".lstrip(),
            encoding="utf-8",
        )
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "claude_desktop_config.json").write_text(
            json.dumps(
                {"mcpServers": {"sharedServer": {"url": "https://example.com"}}}
            ),
            encoding="utf-8",
        )
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        (codex_dir / "config.toml").write_text(
            '[mcp_servers.sharedServer]\nurl = "https://example.com"\n',
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)
        self.assertEqual(plan.mcp_servers[0].server_name, "sharedServer")
        self.assertEqual(sorted(plan.mcp_servers[0].agents), ["claude-code", "codex"])
        self.assertEqual(len(plan.builtin_mcps), 0)

    def test_adopt_rejects_unknown_skip_target(self) -> None:
        plan = build_adopt_plan(self.target_path, self.fake_home)

        with self.assertRaises(AdoptSkipError) as raised:
            apply_adopt_skips(plan, ["mcp/missing"])

        self.assertIn("Unknown adoption skip target", str(raised.exception))

    def test_adopt_findings_include_source_reason_and_skip_action(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        source = codex_dir / "config.toml"
        source.write_text(
            '[mcp_servers."../escape"]\ncommand = "example"\n', encoding="utf-8"
        )

        findings = collect_adopt_findings(
            build_adopt_plan(self.target_path, self.fake_home)
        )

        finding = next(item for item in findings if item.code == "adopt.invalid_mcp")
        self.assertEqual(finding.resource, "mcp/../escape")
        self.assertEqual(finding.source, str(source))
        self.assertTrue(finding.reason)
        self.assertEqual(
            finding.actions[0].command, "aikito adopt --skip mcp/../escape"
        )

    def test_cli_adopt_applies_by_default(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        output = io.StringIO()

        with (
            patch("pathlib.Path.home", return_value=self.fake_home),
            redirect_stdout(output),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["adopt", str(self.target_path)]
            )
            args.func(args)

        self.assertEqual(
            (self.target_path / "global" / "AGENTS.md").read_text(encoding="utf-8"),
            "Shared Rules",
        )
        self.assertIn("Adoption plan", output.getvalue())
        self.assertNotIn("Global Instructions Adoption", output.getvalue())

    def test_cli_adopt_dry_run_is_read_only_and_verbose_is_detailed(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        source = codex_dir / "AGENTS.md"
        source.write_text("Shared Rules\n", encoding="utf-8")
        concise_output = io.StringIO()
        output = io.StringIO()

        with (
            patch("pathlib.Path.home", return_value=self.fake_home),
            redirect_stdout(concise_output),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["adopt", str(self.target_path), "--dry-run"]
            )
            args.func(args)

        self.assertFalse((self.target_path / "global" / "AGENTS.md").exists())
        self.assertIn("Safe to apply", concise_output.getvalue())
        self.assertNotIn(str(source), concise_output.getvalue())

        with (
            patch("pathlib.Path.home", return_value=self.fake_home),
            redirect_stdout(output),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["adopt", str(self.target_path), "--dry-run", "--verbose"]
            )
            args.func(args)

        self.assertFalse((self.target_path / "global" / "AGENTS.md").exists())
        self.assertIn("Global Instructions Adoption", output.getvalue())
        self.assertIn(str(source), output.getvalue())
        self.assertIn("No files were modified", output.getvalue())

    def test_cli_adopt_accepts_repeatable_skip_targets(self) -> None:
        args = AIKITO_CLI.build_parser().parse_args(
            [
                "adopt",
                str(self.target_path),
                "--skip",
                "instructions",
                "--skip",
                "mcp/example",
            ]
        )

        self.assertEqual(args.skip, ["instructions", "mcp/example"])

    def test_cli_adopt_rejects_removed_apply_flag(self) -> None:
        error = io.StringIO()

        with redirect_stderr(error), self.assertRaises(SystemExit):
            AIKITO_CLI.build_parser().parse_args(["adopt", "--apply"])

        self.assertIn("unrecognized arguments: --apply", error.getvalue())

    def test_adopt_apply_points_to_safe_sync(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        plan = build_adopt_plan(self.target_path, self.fake_home)
        output = io.StringIO()

        with redirect_stdout(output):
            self.assertTrue(execute_adoption(plan, dry_run=False))

        self.assertIn("aikito sync", output.getvalue())
        self.assertNotIn("aikito sync --dry-run", output.getvalue())

    def test_adopt_mcp_servers(self) -> None:
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        config_json = claude_dir / "claude_desktop_config.json"
        config_json.write_text(
            json.dumps(
                {"mcpServers": {"test_server": {"command": "npx", "args": ["test"]}}}
            ),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)
        self.assertEqual(plan.mcp_servers[0].server_name, "test_server")

        execute_adoption(plan, dry_run=False)
        mcps_toml = self.target_path / "mcps" / "test_server.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn('command = "npx"', content)

    def test_adopt_mcp_servers_with_env_dict_and_escaping(self) -> None:
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        config_json = claude_dir / "claude_desktop_config.json"
        config_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "complex_server": {
                            "command": 'npx "with quotes"',
                            "args": ["arg1", "arg2"],
                            "env": {
                                "GITHUB_TOKEN": "ghp_SECRET",
                                "API_URL": "https://api.example.com",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)

        execute_adoption(plan, dry_run=False)
        mcps_toml = self.target_path / "mcps" / "complex_server.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn('GITHUB_TOKEN = "${GITHUB_TOKEN}"', content)

        # Verify tomllib.loads succeeds on generated TOML
        import tomllib

        data = tomllib.loads(content)
        self.assertEqual(
            data["env"]["GITHUB_TOKEN"],
            "${GITHUB_TOKEN}",
        )

    def test_adopt_instructions_ignores_whitespace_differences(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text(
            "Same Global Instructions  \n\n", encoding="utf-8"
        )

        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True)
        (claude_dir / "CLAUDE.md").write_text(
            "Same Global Instructions\n", encoding="utf-8"
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertFalse(plan.instructions.has_conflict)

    def test_adopt_subagent_parses_frontmatter_and_sets_source_agent_only(self) -> None:
        claude_agents_dir = self.fake_home / ".claude" / "agents"
        claude_agents_dir.mkdir(parents=True)
        sub_file = claude_agents_dir / "reviewer.md"
        sub_file.write_text(
            "---\nname: reviewer\ndescription: Code Reviewer Agent\n---\nSystem instructions for reviewer",
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.subagents), 1)
        sub = plan.subagents[0]
        self.assertEqual(sub.subagent_name, "reviewer")
        self.assertEqual(sub.description, "Code Reviewer Agent")
        self.assertEqual(sub.system_prompt, "System instructions for reviewer")
        self.assertEqual(sub.target_agents, ["claude-code"])

    def test_adopt_mcp_servers_escapes_special_key_names(self) -> None:
        claude_dir = self.fake_home / ".claude"
        claude_dir.mkdir(parents=True, exist_ok=True)
        config_json = claude_dir / "claude_desktop_config.json"
        server_key = "ev.il" if sys.platform == "win32" else 'ev"il'
        config_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        server_key: {
                            "command": "npx",
                            "args": ["test"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertEqual(len(plan.mcp_servers), 1)

        execute_adoption(plan, dry_run=False)
        mcps_toml = self.target_path / "mcps" / f"{server_key}.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn('command = "npx"', content)

        # Verify tomllib.loads succeeds on generated TOML
        import tomllib

        data = tomllib.loads(content)
        self.assertEqual(data["command"], "npx")

    def test_adopt_handles_exceptions_with_friendly_error(self) -> None:
        from unittest.mock import patch

        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")
        plan = build_adopt_plan(self.target_path, self.fake_home)
        with patch(
            "aikito.adopt.create_adopt_backup",
            side_effect=RuntimeError("Simulated backup storage failure"),
        ):
            with patch("sys.stderr.write"):
                with self.assertRaises(SystemExit) as cm:
                    execute_adoption(plan, dry_run=False)
                self.assertEqual(cm.exception.code, 1)

    def test_adopt_copilot_cli_resources(self) -> None:
        copilot_dir = self.fake_home / ".copilot"
        copilot_dir.mkdir(parents=True)

        (copilot_dir / "copilot-instructions.md").write_text(
            "Shared Copilot Instructions", encoding="utf-8"
        )
        (copilot_dir / "mcp-config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "copilot_server": {
                            "type": "http",
                            "url": "https://mcp.copilot.example.com",
                            "headers": {
                                "Accept": "application/json",
                                "Authorization": "Bearer ${COPILOT_TOKEN}",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        agents_dir = copilot_dir / "agents"
        agents_dir.mkdir()
        (agents_dir / "formatter.agent.md").write_text(
            "---\nname: formatter\ndescription: Copilot Formatter Agent\n"
            'tools: ["read", "search"]\nuser-invocable: false\n---\nCopilot prompt',
            encoding="utf-8",
        )

        (self.target_path / "agents.toml").write_text(
            load_agents_template(),
            encoding="utf-8",
        )

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertTrue(
            any(ag == "github-copilot" for ag, _, _ in plan.instructions.sources)
        )
        self.assertTrue(
            any(s.server_name == "copilot_server" for s in plan.mcp_servers)
        )
        self.assertTrue(
            any(
                sub.subagent_name == "formatter"
                and "github-copilot" in sub.target_agents
                for sub in plan.subagents
            )
        )

        execute_adoption(plan, dry_run=False)

        specs = load_agent_specs(self.target_path, self.fake_home)
        copilot_spec = next(
            spec
            for spec in specs
            if spec.server == "copilot_server" and spec.agent == "github-copilot"
        )
        self.assertEqual(copilot_spec.desired["headers"]["Accept"], "application/json")
        self.assertEqual(
            copilot_spec.desired["headers"]["Authorization"],
            "Bearer ${COPILOT_TOKEN}",
        )

        definitions = load_subagent_definitions(self.target_path)
        formatter = definitions["formatter"]
        self.assertEqual(formatter.agents, ["github-copilot"])
        self.assertEqual(formatter.instructions, "Copilot prompt")
        self.assertEqual(
            formatter.platform_configs["github-copilot"]["tools"],
            ["read", "search"],
        )
        self.assertFalse(formatter.platform_configs["github-copilot"]["user-invocable"])

    def test_adopt_copilot_plaintext_authorization_header_is_sanitized(self) -> None:
        copilot_dir = self.fake_home / ".copilot"
        copilot_dir.mkdir(parents=True)
        (copilot_dir / "mcp-config.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "private-api": {
                            "type": "http",
                            "url": "https://example.com/mcp",
                            "headers": {
                                "Authorization": "Bearer plaintext-secret",
                                "X-API-Version": "2026-08-09",
                            },
                        }
                    }
                }
            ),
            encoding="utf-8",
        )

        server = build_adopt_plan(self.target_path, self.fake_home).mcp_servers[0]

        self.assertEqual(
            server.config_data["headers"]["Authorization"],
            "${AIKITO_PRIVATE_API_AUTHORIZATION}",
        )
        self.assertEqual(server.config_data["headers"]["X-API-Version"], "2026-08-09")

    def test_adopt_request_and_structured_file_plans(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")

        req = AdoptRequest(workspace=self.target_path, home=self.fake_home)
        plan = build_adopt_plan(self.target_path, request=req)

        self.assertEqual(plan.request, req)
        self.assertTrue(plan.can_apply)
        self.assertGreater(len(plan.file_plans), 0)

        inst_plan = next(fp for fp in plan.file_plans if fp.resource_kind == "instructions")
        self.assertIsInstance(inst_plan, AdoptFilePlan)
        self.assertIsNone(inst_plan.expected_pre_image)
        self.assertEqual(inst_plan.action, "CREATE")

    def test_adopt_pre_image_mismatch_prevents_writes(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")

        plan = build_adopt_plan(self.target_path, self.fake_home)
        self.assertTrue(plan.can_apply)

        # Simulate workspace file created after planning
        target_file = self.target_path / "global" / "AGENTS.md"
        target_file.parent.mkdir(parents=True, exist_ok=True)
        target_file.write_text("Interfering content", encoding="utf-8")

        # Execution must fail due to stale pre-image, zero writes made to that plan
        result = execute_adopt_plan(plan, dry_run=False, verbose=False)
        self.assertIsInstance(result, AdoptExecutionResult)
        self.assertFalse(result.success)
        self.assertFalse(bool(result))
        self.assertIn("created after plan", result.error_message or "")
        # File content was untouched
        self.assertEqual(target_file.read_text(encoding="utf-8"), "Interfering content")

    def test_execute_adopt_plan_returns_structured_execution_result(self) -> None:
        codex_dir = self.fake_home / ".codex"
        codex_dir.mkdir(parents=True)
        (codex_dir / "AGENTS.md").write_text("Shared Rules\n", encoding="utf-8")

        plan = build_adopt_plan(self.target_path, self.fake_home)
        result = execute_adopt_plan(plan, dry_run=False, verbose=False)

        self.assertIsInstance(result, AdoptExecutionResult)
        self.assertTrue(result.success)
        self.assertTrue(bool(result))
        self.assertEqual(len(result.instructions), 1)
        self.assertGreater(len(result.backups), 0)


if __name__ == "__main__":
    unittest.main()
