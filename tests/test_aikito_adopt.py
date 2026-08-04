import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_adopt import build_adopt_plan, execute_adoption


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
        mcps_toml = self.target_path / "mcps.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn("[servers.test_server]", content)

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
        mcps_toml = self.target_path / "mcps.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn("[servers.complex_server]", content)
        self.assertIn('GITHUB_TOKEN = "${GITHUB_TOKEN}"', content)

        # Verify tomllib.loads succeeds on generated mcps.toml
        import tomllib

        data = tomllib.loads(content)
        self.assertIn("servers", data)
        self.assertEqual(
            data["servers"]["complex_server"]["env"]["GITHUB_TOKEN"],
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
        config_json.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        'ev"il': {
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
        mcps_toml = self.target_path / "mcps.toml"
        self.assertTrue(mcps_toml.is_file())
        content = mcps_toml.read_text(encoding="utf-8")
        self.assertIn('[servers."ev\\"il"]', content)

        # Verify tomllib.loads succeeds on generated mcps.toml
        import tomllib

        data = tomllib.loads(content)
        self.assertIn("servers", data)
        self.assertIn('ev"il', data["servers"])

    def test_adopt_handles_exceptions_with_friendly_error(self) -> None:
        from unittest.mock import patch

        plan = build_adopt_plan(self.target_path, self.fake_home)
        with patch(
            "aikito_adopt.create_adopt_backup",
            side_effect=RuntimeError("Simulated backup storage failure"),
        ):
            with patch("sys.stderr.write") as mock_stderr:
                with self.assertRaises(SystemExit) as cm:
                    execute_adoption(plan, dry_run=False)
                self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
