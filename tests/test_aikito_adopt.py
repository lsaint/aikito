import json
import sys
import tempfile
import unittest
from pathlib import Path


from aikito_adopt import build_adopt_plan, execute_adoption
from aikito_templates import (
    load_agents_template,
    load_default_memory_instruction,
    load_global_agents_template,
)
from aikito_mcp import load_agent_specs
from aikito_subagent import load_subagent_definitions

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
        execute_adoption(plan, dry_run=False)
        self.assertEqual(
            canonical.read_text(encoding="utf-8"), "# Existing Canonical Rules\n"
        )

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

        plan = build_adopt_plan(self.target_path, self.fake_home)
        with patch(
            "aikito_adopt.create_adopt_backup",
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


if __name__ == "__main__":
    unittest.main()
