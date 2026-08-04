import base64
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_mcp import (  # noqa: E402
    STATE_FILE,
    AgentSpec,
    MCPConfigError,
    authenticate_mcp,
    get_claude_json_server,
    get_jsonc_server,
    get_agy_json_server,
    get_toml_server,
    load_agent_specs,
    load_agents,
    sync_mcp_configs,
    update_claude_json_server,
    update_jsonc_server,
    update_agy_json_server,
    update_toml_server,
)


AGENTS_TOML = """
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "underscore"
live_command = ["codex", "mcp", "list"]
auth_command = ["codex", "mcp", "login", "{target}"]

[agents.opencode]
display_name = "OpenCode"
instruction_path = ".config/opencode/AGENTS.md"

[agents.opencode.mcp]
config_path = ".config/opencode/opencode.jsonc"
config_format = "jsonc"
name_style = "verbatim"
live_command = ["opencode", "mcp", "list"]
auth_command = ["opencode", "mcp", "auth", "{target}"]

[agents.agy]
display_name = "Antigravity CLI (agy)"
instruction_path = ".gemini/GEMINI.md"
skills_path = ".gemini/antigravity-cli/skills"

[agents.agy.mcp]
config_path = ".gemini/config/mcp_config.json"
config_format = "agy_json"
name_style = "verbatim"

[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"
skills_path = ".claude/skills"

[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
live_command = ["claude", "mcp", "list"]
auth_command = ["claude", "mcp", "login", "{target}"]
""".lstrip()


DESIRED_CODEX = {"url": "https://example.com/mcp"}
DESIRED_OPENCODE = {
    "type": "remote",
    "url": "https://example.com/mcp",
    "enabled": True,
    "timeout": 30000,
}


class ConfigEditorTest(unittest.TestCase):
    def test_jsonc_update_preserves_unmanaged_content(self) -> None:
        source = """{
  // Keep this comment.
  "$schema": "https://example.com/schema.json",
  "mcp": {
    "other": {"type": "local"},
    "managed": {"type": "remote", "url": "https://old.example.com"}
  }
}
"""

        updated = update_jsonc_server(source, "managed", DESIRED_OPENCODE)

        self.assertIn("// Keep this comment.", updated)
        self.assertIn('"other": {"type": "local"}', updated)
        self.assertEqual(get_jsonc_server(updated, "managed"), DESIRED_OPENCODE)

    def test_jsonc_insert_adds_mcp_and_server_objects(self) -> None:
        source_without_mcp = '{\n  "theme": "dark"\n}\n'
        source_with_empty_mcp = '{\n  "mcp": {}\n}\n'
        source_with_trailing_comma = '{\n  "theme": "dark",\n}\n'

        updated_root = update_jsonc_server(
            source_without_mcp, "managed", DESIRED_OPENCODE
        )
        updated_mcp = update_jsonc_server(
            source_with_empty_mcp, "managed", DESIRED_OPENCODE
        )
        updated_trailing_comma = update_jsonc_server(
            source_with_trailing_comma, "managed", DESIRED_OPENCODE
        )

        self.assertEqual(get_jsonc_server(updated_root, "managed"), DESIRED_OPENCODE)
        self.assertEqual(get_jsonc_server(updated_mcp, "managed"), DESIRED_OPENCODE)
        self.assertEqual(
            get_jsonc_server(updated_trailing_comma, "managed"), DESIRED_OPENCODE
        )
        self.assertEqual(json.loads(updated_root)["theme"], "dark")

    def test_toml_update_preserves_other_sections(self) -> None:
        source = """model = "gpt"

[mcp_servers.managed]
url = "https://old.example.com"

[other]
enabled = true
"""

        updated = update_toml_server(source, "managed", DESIRED_CODEX)

        self.assertIn('model = "gpt"', updated)
        self.assertIn("[other]\nenabled = true", updated)
        self.assertEqual(get_toml_server(updated, "managed"), DESIRED_CODEX)

    def test_toml_insert_adds_server_section(self) -> None:
        updated = update_toml_server('model = "gpt"\n', "managed", DESIRED_CODEX)

        self.assertEqual(get_toml_server(updated, "managed"), DESIRED_CODEX)

    def test_toml_supports_environment_header_map(self) -> None:
        desired = {
            "url": "https://example.com/mcp",
            "env_http_headers": {"Authorization": "MCP_AUTHORIZATION"},
        }

        updated = update_toml_server("", "managed", desired)

        self.assertEqual(get_toml_server(updated, "managed"), desired)

    def test_agy_json_update_preserves_other_servers(self) -> None:
        source = json.dumps(
            {"mcpServers": {"other": {"serverUrl": "https://other.example.com"}}}
        )
        desired = {"serverUrl": "https://example.com/mcp"}

        updated = update_agy_json_server(source, "managed", desired)

        self.assertEqual(get_agy_json_server(updated, "managed"), desired)
        self.assertIn("other", json.loads(updated)["mcpServers"])

    def test_claude_json_update_preserves_application_state(self) -> None:
        source = json.dumps(
            {
                "machineID": "keep-me",
                "mcpServers": {
                    "other": {"type": "http", "url": "https://other.example.com"}
                },
            }
        )
        desired = {"type": "http", "url": "https://example.com/mcp"}

        updated = update_claude_json_server(source, "managed", desired)

        self.assertEqual(get_claude_json_server(updated, "managed"), desired)
        document = json.loads(updated)
        self.assertEqual(document["machineID"], "keep-me")
        self.assertIn("other", document["mcpServers"])


class SynchronizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.aikito_dir.mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".config/opencode").mkdir(parents=True)
        (self.home / ".gemini/config").mkdir(parents=True)
        (self.aikito_dir / "agents.toml").write_text(AGENTS_TOML)
        (self.aikito_dir / "mcps.toml").write_text(
            """
[servers.managed]
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex", "claude-code", "opencode", "agy"]

[servers.managed.overrides.opencode]
timeout = 30000

[servers.managed.overrides.agy]
enabled = false
reason = "Unsupported test agent"
""".lstrip()
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def install_fake_codex(self, script: str) -> Path:
        executable_directory = self.root / "bin"
        executable_directory.mkdir(exist_ok=True)
        executable = executable_directory / "codex"
        executable.write_text(script)
        executable.chmod(0o700)
        return executable_directory

    def test_sync_preserves_unmanaged_settings_and_is_idempotent(self) -> None:
        codex_config = self.home / ".codex/config.toml"
        opencode_config = self.home / ".config/opencode/opencode.jsonc"
        claude_config = self.home / ".claude.json"
        codex_config.write_text('model = "gpt"\n')
        opencode_config.write_text('{\n  // Keep me.\n  "theme": "dark"\n}\n')
        claude_config.write_text('{"machineID": "keep-me"}\n')
        first_output: list[str] = []

        first_result = sync_mcp_configs(
            aikito_dir=self.aikito_dir,
            home=self.home,
            output=first_output.append,
        )
        first_codex = codex_config.read_text()
        first_opencode = opencode_config.read_text()
        second_output: list[str] = []
        second_result = sync_mcp_configs(
            aikito_dir=self.aikito_dir,
            home=self.home,
            output=second_output.append,
        )

        self.assertTrue(first_result)
        self.assertTrue(second_result)
        self.assertIn('model = "gpt"', first_codex)
        self.assertIn("// Keep me.", first_opencode)
        self.assertEqual(json.loads(claude_config.read_text())["machineID"], "keep-me")
        self.assertEqual(
            get_claude_json_server(claude_config.read_text(), "managed"),
            {"type": "http", "url": "https://example.com/mcp"},
        )
        self.assertEqual(codex_config.read_text(), first_codex)
        self.assertEqual(opencode_config.read_text(), first_opencode)
        self.assertTrue((self.home / STATE_FILE).is_file())
        self.assertTrue(
            any(
                "auth mcp codex managed" in line
                for line in first_output
                if line.startswith("[AUTH]")
            )
        )
        self.assertTrue(any("[OK] codex/managed" in line for line in second_output))

    def test_conflict_requires_force(self) -> None:
        codex_config = self.home / ".codex/config.toml"
        codex_config.write_text(
            '[mcp_servers.managed]\nurl = "https://custom.example.com"\n'
        )
        original = codex_config.read_text()
        output: list[str] = []

        result = sync_mcp_configs(
            aikito_dir=self.aikito_dir,
            home=self.home,
            output=output.append,
        )

        self.assertFalse(result)
        self.assertEqual(codex_config.read_text(), original)
        self.assertTrue(any("[CONFLICT]" in line for line in output))

        forced_result = sync_mcp_configs(
            aikito_dir=self.aikito_dir,
            home=self.home,
            force=True,
            output=output.append,
        )

        self.assertTrue(forced_result)
        self.assertEqual(
            get_toml_server(codex_config.read_text(), "managed"), DESIRED_CODEX
        )

    def test_dry_run_does_not_write_files(self) -> None:
        codex_config = self.home / ".codex/config.toml"
        codex_config.write_text('model = "gpt"\n')

        result = sync_mcp_configs(
            aikito_dir=self.aikito_dir,
            home=self.home,
            dry_run=True,
            output=lambda _: None,
        )

        self.assertTrue(result)
        self.assertEqual(codex_config.read_text(), 'model = "gpt"\n')
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_auth_captures_browser_url_and_redacts_callback(self) -> None:
        (self.home / ".codex/config.toml").write_text(
            '[mcp_servers.managed]\nurl = "https://example.com/mcp"\n'
        )
        executable_directory = self.install_fake_codex(
            """#!/bin/sh
"$BROWSER" "https://auth.atlassian.com/authorize?client_id=test&redirect_uri=http%3A%2F%2Flocalhost"
printf '%s\\n' 'callback: http://127.0.0.1/callback?code=secret'
"""
        )
        original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{executable_directory}{os.pathsep}{original_path}"
        output: list[str] = []
        try:
            result = authenticate_mcp(
                aikito_dir=self.aikito_dir,
                home=self.home,
                agent="codex",
                server="managed",
                output=output.append,
                open_browser=False,
            )
        finally:
            os.environ["PATH"] = original_path

        combined_output = "\n".join(output)
        self.assertTrue(result)
        self.assertIn(
            "[AUTH URL] https://auth.atlassian.com/authorize?", combined_output
        )
        self.assertIn("[REDACTED CALLBACK URL]", combined_output)
        self.assertNotIn("code=secret", combined_output)

    def test_auth_fails_when_no_authorization_url_is_exposed(self) -> None:
        (self.home / ".codex/config.toml").write_text(
            '[mcp_servers.managed]\nurl = "https://example.com/mcp"\n'
        )
        executable_directory = self.install_fake_codex(
            "#!/bin/sh\nprintf '%s\\n' 'No authorization required'\n"
        )
        original_path = os.environ.get("PATH", "")
        os.environ["PATH"] = f"{executable_directory}{os.pathsep}{original_path}"
        output: list[str] = []
        try:
            result = authenticate_mcp(
                aikito_dir=self.aikito_dir,
                home=self.home,
                agent="codex",
                server="managed",
                output=output.append,
                open_browser=False,
            )
        finally:
            os.environ["PATH"] = original_path

        self.assertFalse(result)
        self.assertTrue(
            any("did not expose an authorization URL" in line for line in output)
        )


class AgentRegistryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.aikito_dir.mkdir(parents=True)
        (self.aikito_dir / "agents.toml").write_text(AGENTS_TOML)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_servers(self, body: str) -> None:
        (self.aikito_dir / "mcps.toml").write_text(body.lstrip())

    def test_load_agents_parses_registry(self) -> None:
        agents = load_agents(self.aikito_dir, self.home)

        self.assertEqual(set(agents), {"codex", "opencode", "agy", "claude-code"})
        self.assertEqual(
            agents["codex"].instruction_path, self.home / ".codex/AGENTS.md"
        )
        self.assertIsNone(agents["codex"].skills_path)
        self.assertTrue(agents["codex"].supports_mcp)
        self.assertEqual(agents["codex"].mcp_config_format, "toml")
        self.assertEqual(
            agents["claude-code"].skills_path, self.home / ".claude/skills"
        )
        self.assertEqual(
            agents["agy"].skills_path,
            self.home / ".gemini/antigravity-cli/skills",
        )
        self.assertTrue(agents["claude-code"].supports_mcp)
        self.assertEqual(
            agents["claude-code"].mcp_config_path, self.home / ".claude.json"
        )
        self.assertEqual(agents["claude-code"].mcp_config_format, "claude_json")

    def test_missing_agents_config_raises(self) -> None:
        (self.aikito_dir / "agents.toml").unlink()
        with self.assertRaises(MCPConfigError):
            load_agents(self.aikito_dir, self.home)

    def test_specs_synthesized_from_registry_and_servers(self) -> None:
        self.write_servers(
            """
[servers.my-server]
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex", "claude-code", "opencode", "agy"]

[servers.my-server.overrides.opencode]
timeout = 45000
"""
        )

        specs = {s.agent: s for s in load_agent_specs(self.aikito_dir, self.home)}

        # name_style = underscore -> my-server becomes my_server for codex.
        self.assertEqual(specs["codex"].target_name, "my_server")
        self.assertEqual(specs["codex"].desired, {"url": "https://example.com/mcp"})
        self.assertEqual(
            specs["codex"].auth_command,
            ("codex", "mcp", "login", "my_server"),
        )
        # name_style = verbatim -> opencode keeps the server name.
        self.assertEqual(specs["opencode"].target_name, "my-server")
        self.assertEqual(specs["opencode"].desired["timeout"], 45000)
        self.assertEqual(
            specs["opencode"].auth_command,
            ("opencode", "mcp", "auth", "my-server"),
        )
        self.assertEqual(
            specs["claude-code"].desired,
            {"type": "http", "url": "https://example.com/mcp"},
        )
        self.assertEqual(
            specs["claude-code"].auth_command,
            ("claude", "mcp", "login", "my-server"),
        )
        self.assertTrue(specs["agy"].enabled)
        self.assertEqual(specs["agy"].config_format, "agy_json")
        self.assertEqual(
            specs["agy"].desired,
            {"serverUrl": "https://example.com/mcp"},
        )

    def test_specs_render_basic_token_auth_per_agent(self) -> None:
        token = "test-token"
        self.write_servers(
            """
[servers.my-server]
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex", "claude-code", "opencode", "agy"]

[servers.my-server.authentication]
method = "basic_api_token"
account_email = "user@example.com"
token_env = "TEST_MCP_TOKEN"
authorization_env = "TEST_MCP_AUTHORIZATION"
"""
        )
        previous = os.environ.get("TEST_MCP_TOKEN")
        os.environ["TEST_MCP_TOKEN"] = token
        try:
            specs = {
                spec.agent: spec
                for spec in load_agent_specs(self.aikito_dir, self.home)
            }
        finally:
            if previous is None:
                os.environ.pop("TEST_MCP_TOKEN", None)
            else:
                os.environ["TEST_MCP_TOKEN"] = previous

        self.assertEqual(
            specs["codex"].desired["env_http_headers"],
            {"Authorization": "TEST_MCP_AUTHORIZATION"},
        )
        self.assertEqual(
            specs["opencode"].desired["headers"],
            {"Authorization": "{env:TEST_MCP_AUTHORIZATION}"},
        )
        self.assertEqual(
            specs["claude-code"].desired["headers"],
            {"Authorization": "${TEST_MCP_AUTHORIZATION}"},
        )
        self.assertFalse(specs["opencode"].desired["oauth"])
        expected = base64.b64encode(b"user@example.com:test-token").decode()
        self.assertEqual(
            specs["agy"].desired["headers"]["Authorization"],
            f"Basic {expected}",
        )
        self.assertTrue(specs["agy"].contains_secret)
        self.assertEqual(specs["codex"].auth_command, ())
        self.assertEqual(specs["claude-code"].auth_command, ())
        self.assertEqual(specs["opencode"].auth_command, ())
        self.assertNotIn(token, json.dumps(specs["codex"].desired))
        self.assertNotIn(token, json.dumps(specs["claude-code"].desired))
        self.assertNotIn(token, json.dumps(specs["opencode"].desired))

    def test_unknown_agent_reference_raises(self) -> None:
        self.write_servers(
            """
[servers.my-server]
transport = "remote"
url = "https://example.com/mcp"
agents = ["nonexistent"]
"""
        )

        with self.assertRaises(MCPConfigError):
            load_agent_specs(self.aikito_dir, self.home)


class AgentSpecTest(unittest.TestCase):
    def test_state_key_is_stable(self) -> None:
        spec = AgentSpec(
            agent="codex",
            server="managed",
            config_path=Path("config.toml"),
            config_format="toml",
            target_name="managed",
            desired=DESIRED_CODEX,
        )

        self.assertEqual(spec.state_key, "codex:managed")


if __name__ == "__main__":
    unittest.main()
