import base64
import io
import json
import os
import sys
import tempfile

import time
import tomllib
import unittest
from pathlib import Path
from urllib.error import HTTPError, URLError
from unittest.mock import MagicMock, patch

from aikito_mcp import (
    STATE_FILE,
    AgentSpec,
    MCPConfigError,
    MCPToolProbeResult,
    _LiveLoadingIndicator,
    _MCPProbeError,
    _agent_detected,
    _list_remote_mcp_tools,
    _post_mcp_message,
    _redact_probe_error,
    _response_message,
    authenticate_mcp,
    describe_mcp_auth,
    evaluate_spec_status,
    get_agy_json_server,
    get_claude_json_server,
    get_copilot_json_server,
    get_dsh_cordis_server,
    get_jsonc_server,
    get_toml_server,
    is_agent_installed,
    load_agent_specs,
    load_agents,
    probe_mcp_tools,
    probe_mcp_tools_for_specs,
    read_all_entries,
    redact_mcp_entry,
    sync_mcp_configs,
    update_agy_json_server,
    update_claude_json_server,
    update_copilot_json_server,
    update_dsh_cordis_server,
    update_jsonc_server,
    update_toml_server,
)

ROOT = Path(__file__).resolve().parents[1]


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
display_name = "Antigravity CLI"
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

[agents.dsh]
display_name = "DeepSeek Harness"
instruction_path = ".dsh/AGENTS.md"
skills_path = ".agents/skills"

[agents.dsh.runner]
command = ["dsh", "--profile", "headless", "{prompt}"]

[agents.dsh.mcp]
config_path = ".dsh/cordis.patch.yml"
config_format = "dsh_cordis"
name_style = "verbatim"

[agents.grok]
display_name = "Grok Build"
instruction_path = ".grok/rules/aikito.md"

[agents.grok.mcp]
config_path = ".grok/config.toml"
config_format = "toml"
name_style = "verbatim"
live_command = ["grok", "mcp", "list"]
"""


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

    def test_toml_supports_interpolated_header_map(self) -> None:
        desired = {
            "url": "https://example.com/mcp",
            "headers": {"Authorization": "${MCP_AUTHORIZATION}"},
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

    def test_dsh_cordis_update_creates_new_file(self) -> None:
        desired = {
            "serverName": "atlassian-rovo",
            "transport": "streamable-http",
            "url": "https://mcp.atlassian.com/v1/mcp",
            "headers": {
                "Authorization": "!!js process.env.ATLASSIAN_MCP_AUTHORIZATION"
            },
        }
        updated = update_dsh_cordis_server("", "atlassian-rovo", desired)
        self.assertEqual(get_dsh_cordis_server(updated, "atlassian-rovo"), desired)

    def test_dsh_cordis_update_preserves_unmanaged_rows_and_comments(self) -> None:
        source = """# User cordis patch configuration
- id: custom-plugin
  name: '@my-scope/custom-plugin'
  config:
    enabled: true

- id: aikito-mcp-old
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: old
    transport: streamable-http
    url: https://old.example.com
"""
        desired = {
            "serverName": "github",
            "transport": "stdio",
            "command": "npx",
            "args": ["-y", "@modelcontextprotocol/server-github"],
            "env": {"GITHUB_TOKEN": "!!js process.env.GITHUB_TOKEN"},
        }
        updated = update_dsh_cordis_server(source, "github", desired)
        self.assertIn("custom-plugin", updated)
        self.assertIn("# User cordis patch configuration", updated)
        self.assertEqual(get_dsh_cordis_server(updated, "github"), desired)
        self.assertEqual(
            get_dsh_cordis_server(updated, "old")["url"], "https://old.example.com"
        )

    def test_dsh_cordis_update_modifies_existing_entry(self) -> None:
        source = """- id: aikito-mcp-atlassian-rovo
  name: '@deepseek-ai/dsh-mcp-client'
  config:
    serverName: atlassian-rovo
    transport: streamable-http
    url: https://old.atlassian.com/v1/mcp
"""
        desired = {
            "serverName": "atlassian-rovo",
            "transport": "streamable-http",
            "url": "https://mcp.atlassian.com/v1/mcp",
            "headers": {
                "Authorization": "!!js process.env.ATLASSIAN_MCP_AUTHORIZATION"
            },
            "toolCallTimeoutMs": 45000,
        }
        updated = update_dsh_cordis_server(source, "atlassian-rovo", desired)
        self.assertEqual(get_dsh_cordis_server(updated, "atlassian-rovo"), desired)
        entries = read_all_entries("dsh_cordis", updated)
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries["atlassian-rovo"], desired)


class SynchronizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.aikito_dir.mkdir(parents=True)
        (self.home / ".codex").mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)
        (self.home / ".config/opencode").mkdir(parents=True)
        (self.home / ".gemini/config").mkdir(parents=True)
        (self.aikito_dir / "agents.toml").write_text(AGENTS_TOML)
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "mcps/managed.toml").write_text(
            """
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex", "claude-code", "opencode", "agy"]

[overrides.opencode]
timeout = 30000

[overrides.agy]
enabled = false
reason = "Unsupported test agent"
""".lstrip()
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def install_fake_codex(self, script: str) -> Path:
        executable_directory = self.root / "bin"
        executable_directory.mkdir(exist_ok=True)
        if sys.platform == "win32":
            executable = executable_directory / "codex.cmd"
            py_file = executable_directory / "codex_shim.py"
            py_code = f"""
import os, sys, subprocess
browser = os.environ.get("BROWSER")
script_text = {json.dumps(script)}
if "auth.atlassian.com" in script_text and browser:
    subprocess.run([browser, "https://auth.atlassian.com/authorize?client_id=test&redirect_uri=http%3A%2F%2Flocalhost"])
    print("callback: http://127.0.0.1/callback?code=secret")
elif "No authorization" in script_text:
    print("No authorization required")
"""
            py_file.write_text(py_code.strip() + "\n", encoding="utf-8")
            executable.write_text(
                f'@echo off\r\n"{sys.executable}" "{py_file}" %*\r\n', encoding="utf-8"
            )
        else:
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

    def test_sync_writes_grok_native_mcp_config(self) -> None:
        (self.aikito_dir / "mcps/managed.toml").write_text(
            'transport = "remote"\n'
            'url = "https://example.com/mcp"\n'
            'agents = ["grok"]\n'
            "\n[authentication]\n"
            'method = "basic_api_token"\n'
            'account_email = "user@example.com"\n'
            'token_env = "TEST_MCP_TOKEN"\n'
            'authorization_env = "TEST_MCP_AUTHORIZATION"\n',
            encoding="utf-8",
        )
        grok_dir = self.home / ".grok"
        grok_dir.mkdir()
        grok_config = grok_dir / "config.toml"
        grok_config.write_text('model = "grok-build"\n', encoding="utf-8")

        result = sync_mcp_configs(aikito_dir=self.aikito_dir, home=self.home)

        self.assertTrue(result)
        with grok_config.open("rb") as config_file:
            config = tomllib.load(config_file)
        self.assertEqual(config["model"], "grok-build")
        self.assertEqual(
            config["mcp_servers"]["managed"]["url"], "https://example.com/mcp"
        )
        self.assertEqual(
            config["mcp_servers"]["managed"]["headers"]["Authorization"],
            "${TEST_MCP_AUTHORIZATION}",
        )
        self.assertNotIn("env_http_headers", config["mcp_servers"]["managed"])

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

    def test_missing_agy_token_aborts_before_writing_any_config(self) -> None:
        (self.aikito_dir / "mcps/managed.toml").write_text(
            """
transport = "remote"
url = "https://example.com/mcp"
agents = ["codex", "agy"]

[authentication]
method = "basic_api_token"
account_email = "user@example.com"
token_env = "TEST_MCP_TOKEN"
authorization_env = "TEST_MCP_AUTHORIZATION"
""".lstrip()
        )
        codex_config = self.home / ".codex/config.toml"
        agy_config = self.home / ".gemini/config/mcp_config.json"
        codex_config.write_text('model = "keep-me"\n')
        existing_header = base64.b64encode(b"user@example.com:valid-token").decode()
        agy_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "managed": {
                            "serverUrl": "https://example.com/mcp",
                            "headers": {"Authorization": f"Basic {existing_header}"},
                        }
                    }
                }
            )
        )
        previous = os.environ.pop("TEST_MCP_TOKEN", None)
        output: list[str] = []
        try:
            result = sync_mcp_configs(
                aikito_dir=self.aikito_dir,
                home=self.home,
                output=output.append,
            )
            agy_spec = next(
                spec
                for spec in load_agent_specs(self.aikito_dir, self.home)
                if spec.agent == "agy"
            )
        finally:
            if previous is not None:
                os.environ["TEST_MCP_TOKEN"] = previous

        self.assertFalse(result)
        self.assertEqual(codex_config.read_text(), 'model = "keep-me"\n')
        self.assertIn(existing_header, agy_config.read_text())
        self.assertFalse((self.home / STATE_FILE).exists())
        self.assertEqual(evaluate_spec_status(agy_spec), "OK")
        self.assertTrue(any("TEST_MCP_TOKEN" in line for line in output))

        placeholder_header = base64.b64encode(
            b"user@example.com:placeholder-token-set-environment-variable"
        ).decode()
        agy_config.write_text(
            agy_config.read_text().replace(existing_header, placeholder_header)
        )
        self.assertEqual(evaluate_spec_status(agy_spec), "DRIFT")

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
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "mcps/managed.toml").write_text(body.lstrip())

    def test_load_agents_parses_registry(self) -> None:
        agents = load_agents(self.aikito_dir, self.home)

        self.assertEqual(
            set(agents),
            {"codex", "opencode", "agy", "claude-code", "dsh", "grok"},
        )
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
        self.assertEqual(agents["dsh"].instruction_path, self.home / ".dsh/AGENTS.md")
        self.assertEqual(agents["dsh"].skills_path, self.home / ".agents/skills")
        self.assertTrue(agents["dsh"].supports_mcp)
        self.assertEqual(
            agents["dsh"].mcp_config_path, self.home / ".dsh/cordis.patch.yml"
        )
        self.assertEqual(agents["dsh"].mcp_config_format, "dsh_cordis")

    def test_missing_agents_config_raises(self) -> None:
        (self.aikito_dir / "agents.toml").unlink()
        with self.assertRaises(MCPConfigError):
            load_agents(self.aikito_dir, self.home)

    def test_load_agents_accepts_empty_registry(self) -> None:
        (self.aikito_dir / "agents.toml").write_text("[agents]\n", encoding="utf-8")

        self.assertEqual(load_agents(self.aikito_dir, self.home), {})

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
agents = ["codex", "claude-code", "opencode", "agy", "grok"]

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
            specs["grok"].desired["headers"],
            {"Authorization": "${TEST_MCP_AUTHORIZATION}"},
        )
        self.assertEqual(specs["grok"].target_name, "my-server")
        self.assertEqual(
            specs["opencode"].desired["headers"],
            {"Authorization": "{env:TEST_MCP_AUTHORIZATION}"},
        )
        self.assertEqual(
            specs["claude-code"].desired["headers"],
            {"Authorization": "${TEST_MCP_AUTHORIZATION}"},
        )
        self.assertNotIn("auth", specs["codex"].desired)
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

    def test_copilot_json_get_and_update(self) -> None:
        source = """{
  "mcpServers": {
    "existing": {
      "type": "http",
      "url": "https://mcp.example.com"
    }
  }
}
"""
        desired = {
            "type": "http",
            "url": "https://context7.com/mcp",
            "tools": ["*"],
        }
        updated = update_copilot_json_server(source, "context7", desired)
        server = get_copilot_json_server(updated, "context7")
        self.assertEqual(server, desired)
        self.assertEqual(
            get_copilot_json_server(updated, "existing")["url"],
            "https://mcp.example.com",
        )


class ReadAllEntriesTest(unittest.TestCase):
    def test_read_all_entries_valid(self) -> None:
        toml_content = '[mcp_servers.srv1]\nurl = "https://example.com"\n'
        entries = read_all_entries("toml", toml_content)
        self.assertIn("srv1", entries)
        self.assertEqual(entries["srv1"]["url"], "https://example.com")

        json_content = '{"mcpServers": {"srv2": {"serverUrl": "https://example.com"}}}'
        entries = read_all_entries("agy_json", json_content)
        self.assertIn("srv2", entries)

    def test_read_all_entries_syntax_errors_raise_mcp_config_error(self) -> None:
        invalid_toml = '[mcp_servers.srv1\nurl = "unclosed"'
        with self.assertRaises(MCPConfigError) as ctx:
            read_all_entries("toml", invalid_toml)
        self.assertIn("Invalid Codex TOML config", str(ctx.exception))

        invalid_json = '{"mcpServers": { invalid json'
        with self.assertRaises(MCPConfigError) as ctx:
            read_all_entries("agy_json", invalid_json)
        self.assertIn("Invalid agy JSON config", str(ctx.exception))

    def test_read_all_entries_non_dict_servers(self) -> None:
        invalid_collection = '{"mcpServers": "not a dict"}'
        with self.assertRaises(MCPConfigError) as ctx:
            read_all_entries("agy_json", invalid_collection)
        self.assertIn(
            "Agent MCP server collection must be an object", str(ctx.exception)
        )


class RedactMCPEntryTest(unittest.TestCase):
    def test_redact_headers_container(self) -> None:
        entry = {
            "headers": {
                "Authorization": "Bearer secret123",
                "X-Custom": "custom-value",
                "EnvHeader": "${MY_ENV}",
            }
        }
        redacted = redact_mcp_entry(entry)
        self.assertEqual(redacted["headers"]["Authorization"], "<redacted>")
        self.assertEqual(redacted["headers"]["X-Custom"], "<redacted>")
        self.assertEqual(redacted["headers"]["EnvHeader"], "${MY_ENV}")

    def test_redact_env_http_headers_exemption(self) -> None:
        entry = {"env_http_headers": {"Authorization": "CODEX_AUTH_TOKEN"}}
        redacted = redact_mcp_entry(entry)
        self.assertEqual(
            redacted["env_http_headers"]["Authorization"], "CODEX_AUTH_TOKEN"
        )

    def test_redact_sensitive_url_query_params(self) -> None:
        entry = {
            "serverUrl": "https://example.com/mcp?token=secret123&mode=read",
            "normal_url": "https://example.com/mcp?mode=read",
        }
        redacted = redact_mcp_entry(entry)
        self.assertEqual(
            redacted["serverUrl"], "https://example.com/mcp?token=<redacted>&mode=read"
        )
        self.assertEqual(redacted["normal_url"], "https://example.com/mcp?mode=read")

    def test_redact_sensitive_keys(self) -> None:
        entry = {
            "password": "my_password",
            "user_pat": "ghp_12345",
            "api_key": "sk-12345",
            "command": "npx",
        }
        redacted = redact_mcp_entry(entry)
        self.assertEqual(redacted["password"], "<redacted>")
        self.assertEqual(redacted["user_pat"], "<redacted>")
        self.assertEqual(redacted["api_key"], "<redacted>")
        self.assertEqual(redacted["command"], "npx")

    def test_pat_substring_not_misidentified(self) -> None:
        entry = {
            "path": "/usr/local/bin",
            "patch": "v1.0",
            "pattern": "*.py",
            "compatibility": "full",
            "pat": "ghp_secret",
            "my_pat": "ghp_secret2",
        }
        redacted = redact_mcp_entry(entry)
        self.assertEqual(redacted["path"], "/usr/local/bin")
        self.assertEqual(redacted["patch"], "v1.0")
        self.assertEqual(redacted["pattern"], "*.py")
        self.assertEqual(redacted["compatibility"], "full")
        self.assertEqual(redacted["pat"], "<redacted>")
        self.assertEqual(redacted["my_pat"], "<redacted>")


class DescribeMCPAuthTest(unittest.TestCase):
    def test_describes_environment_and_inline_authorization(self) -> None:
        with patch.dict(
            os.environ,
            {
                "BASIC_AUTH": "Basic secret",
                "BEARER_AUTH": "Bearer secret",
            },
        ):
            self.assertEqual(
                describe_mcp_auth(
                    {"env_http_headers": {"Authorization": "BASIC_AUTH"}}
                ),
                "Basic · env header",
            )
            self.assertEqual(
                describe_mcp_auth({"headers": {"Authorization": "${BEARER_AUTH}"}}),
                "Bearer · env header",
            )
        self.assertEqual(
            describe_mcp_auth({"headers": {"Authorization": "Basic inline-secret"}}),
            "Basic · inline header",
        )

    def test_describes_oauth_and_no_auth(self) -> None:
        self.assertEqual(describe_mcp_auth({"auth": "oauth"}), "OAuth")
        self.assertEqual(describe_mcp_auth({"oauth": True}), "OAuth")
        self.assertEqual(describe_mcp_auth({"url": "https://example.com"}), "None")


class MCPToolProbeTest(unittest.TestCase):
    def test_error_redaction_handles_short_credentials_only(self) -> None:
        redacted = _redact_probe_error(
            "token=x version=1",
            {"Authorization": "Basic x", "X-Version": "1"},
        )

        self.assertEqual(redacted, "token=<redacted> version=1")

    def test_list_remote_tools_initializes_then_lists(self) -> None:
        initialize_response = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "result": {"protocolVersion": "2025-11-25", "capabilities": {}},
            }
        ).encode()
        tools_response = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "result": {"tools": [{"name": "one"}, {"name": "two"}]},
            }
        ).encode()
        with patch(
            "aikito_mcp._post_mcp_message",
            side_effect=[
                (initialize_response, "session-id"),
                (b"", ""),
                (tools_response, ""),
            ],
        ) as post_message:
            tools = _list_remote_mcp_tools(
                "https://example.com/mcp", {"Authorization": "Basic secret"}, 5
            )

        self.assertEqual(tools, ("one", "two"))
        self.assertEqual(post_message.call_count, 3)
        self.assertEqual(
            post_message.call_args_list[1].args[1]["method"],
            "notifications/initialized",
        )
        self.assertEqual(post_message.call_args_list[2].args[1]["method"], "tools/list")
        self.assertEqual(
            post_message.call_args_list[2].kwargs["session_id"], "session-id"
        )

    def test_probe_uses_agent_native_auth_without_exposing_it(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.managed]
url = "https://example.com/mcp"
env_http_headers = { Authorization = "TEST_AUTH" }
""".lstrip()
            )
            spec = AgentSpec(
                agent="codex",
                server="managed",
                config_path=config_path,
                config_format="toml",
                target_name="managed",
                desired={},
            )
            with (
                patch.dict(os.environ, {"TEST_AUTH": "Basic secret"}),
                patch(
                    "aikito_mcp._list_remote_mcp_tools",
                    return_value=("one", "two"),
                ) as list_tools,
            ):
                result = probe_mcp_tools(spec)

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.auth_method, "Basic · env header")
        self.assertEqual(result.tool_names, ("one", "two"))
        self.assertNotIn("secret", repr(result))
        self.assertEqual(
            list_tools.call_args.args[1], {"Authorization": "Basic secret"}
        )

    def test_probe_redacts_credentials_from_all_error_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.managed]
url = "https://example.com/mcp"
env_http_headers = { Authorization = "TEST_AUTH" }
""".lstrip()
            )
            spec = AgentSpec(
                agent="codex",
                server="managed",
                config_path=config_path,
                config_format="toml",
                target_name="managed",
                desired={},
            )
            failures = (
                _MCPProbeError(
                    "server echoed Basic echoed-token-123 and echoed-token-123\x1b[31m"
                ),
                URLError("transport echoed Basic echoed-token-123"),
            )
            with patch.dict(os.environ, {"TEST_AUTH": "Basic echoed-token-123"}):
                for failure in failures:
                    with (
                        self.subTest(failure=type(failure).__name__),
                        patch(
                            "aikito_mcp._list_remote_mcp_tools",
                            side_effect=failure,
                        ),
                    ):
                        result = probe_mcp_tools(spec)

                    self.assertEqual(result.status, "ERROR")
                    self.assertNotIn("echoed-token-123", result.error)
                    self.assertNotIn("\x1b", result.error)
                    self.assertIn("<redacted>", result.error)

    def test_probe_redacts_jsonrpc_error_before_truncating(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.managed]
url = "https://example.com/mcp"
env_http_headers = { Authorization = "TEST_AUTH" }
""".lstrip()
            )
            spec = AgentSpec(
                agent="codex",
                server="managed",
                config_path=config_path,
                config_format="toml",
                target_name="managed",
                desired={},
            )
            error_response = json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "error": {
                        "code": -32000,
                        "message": "x" * 230 + " Basic echoed-token-123",
                    },
                }
            ).encode()
            with (
                patch.dict(os.environ, {"TEST_AUTH": "Basic echoed-token-123"}),
                patch(
                    "aikito_mcp._post_mcp_message",
                    return_value=(error_response, ""),
                ),
            ):
                result = probe_mcp_tools(spec)

        self.assertEqual(result.status, "ERROR")
        self.assertNotIn("echoed-token-123", result.error)
        self.assertNotIn("Basic echoed", result.error)
        self.assertIn("<redacted>", result.error)

    def test_probe_refuses_credentials_over_non_loopback_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.managed]
url = "http://example.com/mcp"
env_http_headers = { Authorization = "TEST_AUTH" }
""".lstrip()
            )
            spec = AgentSpec(
                agent="codex",
                server="managed",
                config_path=config_path,
                config_format="toml",
                target_name="managed",
                desired={},
            )
            with (
                patch.dict(os.environ, {"TEST_AUTH": "Basic secret"}),
                patch("aikito_mcp._list_remote_mcp_tools") as list_tools,
            ):
                result = probe_mcp_tools(spec)

        self.assertEqual(result.status, "SKIP")
        self.assertIn("non-loopback HTTP", result.error)
        list_tools.assert_not_called()

    def test_probe_allows_credentials_over_loopback_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            config_path = Path(tmp_dir) / "config.toml"
            config_path.write_text(
                """
[mcp_servers.managed]
url = "http://127.0.0.2:8080/mcp"
env_http_headers = { Authorization = "TEST_AUTH" }
""".lstrip()
            )
            spec = AgentSpec(
                agent="codex",
                server="managed",
                config_path=config_path,
                config_format="toml",
                target_name="managed",
                desired={},
            )
            with (
                patch.dict(os.environ, {"TEST_AUTH": "Basic secret"}),
                patch(
                    "aikito_mcp._list_remote_mcp_tools",
                    return_value=("one",),
                ) as list_tools,
            ):
                result = probe_mcp_tools(spec)

        self.assertEqual(result.status, "OK")
        self.assertEqual(result.tool_names, ("one",))
        list_tools.assert_called_once()

    def test_probe_sends_user_agent_header(self) -> None:
        with (
            patch("aikito_mcp.build_opener") as mock_opener,
            patch("aikito_mcp.Request") as mock_request,
        ):
            mock_resp = unittest.mock.MagicMock()
            mock_resp.read.return_value = b"{}"
            mock_resp.headers = {}
            mock_opener.return_value.open.return_value.__enter__.return_value = (
                mock_resp
            )
            _post_mcp_message(
                "https://example.com/mcp",
                {"method": "test"},
                {"Authorization": "Bearer token"},
                timeout=5,
            )
            mock_request.assert_called_once()
            headers = (
                mock_request.call_args.kwargs.get("headers")
                or mock_request.call_args.args[2]
            )
            self.assertEqual(headers.get("User-Agent"), "aikito")

    def test_post_mcp_message_retries_on_rate_limit(self) -> None:
        mock_success = MagicMock()
        mock_success.read.return_value = b'{"result": 1}'
        mock_success.headers = {}
        mock_cm = MagicMock()
        mock_cm.__enter__.return_value = mock_success

        err_fp = io.BytesIO(b'{"detail": "Rate limited"}')
        http_429 = HTTPError(
            "https://example.com/mcp", 429, "Too Many Requests", {}, err_fp
        )

        with (
            patch("aikito_mcp.build_opener") as mock_opener,
            patch("aikito_mcp.time.sleep") as mock_sleep,
        ):
            mock_opener.return_value.open.side_effect = [http_429, mock_cm]
            body, _ = _post_mcp_message(
                "https://example.com/mcp",
                {"method": "test"},
                {},
                timeout=5,
            )
            self.assertEqual(body, b'{"result": 1}')
            self.assertEqual(mock_opener.return_value.open.call_count, 2)
            mock_sleep.assert_called_once()

    def test_post_mcp_message_does_not_retry_on_401(self) -> None:
        err_fp = io.BytesIO(b'{"detail": "Unauthorized"}')
        http_401 = HTTPError("https://example.com/mcp", 401, "Unauthorized", {}, err_fp)

        with (
            patch("aikito_mcp.build_opener") as mock_opener,
            patch("aikito_mcp.time.sleep") as mock_sleep,
        ):
            mock_opener.return_value.open.side_effect = http_401
            with self.assertRaises(_MCPProbeError):
                _post_mcp_message(
                    "https://example.com/mcp",
                    {"method": "test"},
                    {},
                    timeout=5,
                )
            self.assertEqual(mock_opener.return_value.open.call_count, 1)
            mock_sleep.assert_not_called()

    def test_response_message_extracts_detail_and_title(self) -> None:
        self.assertEqual(
            _response_message(b'{"detail": "Blocked by browser signature"}'),
            "Blocked by browser signature",
        )
        self.assertEqual(
            _response_message(b'{"title": "Forbidden"}'),
            "Forbidden",
        )
        self.assertEqual(
            _response_message(b'{"error": {"message": "Invalid JSON-RPC"}}'),
            "Invalid JSON-RPC",
        )


class AgentDetectionTest(unittest.TestCase):
    """Canonical install detection must not depend on the config parent dir."""

    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _spec(self, agent: str, config_path: Path) -> AgentSpec:
        return AgentSpec(
            agent=agent,
            server="demo",
            config_path=config_path,
            config_format="toml",
            target_name="demo",
            desired={},
            home=self.home,
        )

    def test_marker_directory_counts_as_installed(self) -> None:
        (self.home / ".grok").mkdir()

        with patch("aikito_mcp.shutil.which", return_value=None):
            self.assertIs(is_agent_installed("grok", self.home), True)

    def test_binary_counts_as_installed(self) -> None:
        with patch("aikito_mcp.shutil.which", return_value="/usr/local/bin/grok"):
            self.assertIs(is_agent_installed("grok", self.home), True)

    def test_absent_agent_is_not_installed(self) -> None:
        with patch("aikito_mcp.shutil.which", return_value=None):
            self.assertIs(is_agent_installed("grok", self.home), False)

    def test_unknown_agent_returns_none(self) -> None:
        self.assertIsNone(is_agent_installed("custom-agent", self.home))

    def test_agent_detected_uses_marker_over_missing_parent(self) -> None:
        (self.home / ".grok").mkdir()
        spec = self._spec("grok", self.home / ".grok" / "rules" / "config.toml")

        with patch("aikito_mcp.shutil.which", return_value=None):
            self.assertTrue(_agent_detected(spec))

    def test_agent_detected_false_when_not_installed(self) -> None:
        spec = self._spec("grok", self.home / ".grok" / "rules" / "config.toml")

        with patch("aikito_mcp.shutil.which", return_value=None):
            self.assertFalse(_agent_detected(spec))

    def test_agent_detected_falls_back_for_unknown_agent(self) -> None:
        spec = self._spec("custom-agent", self.home / ".custom" / "config.toml")

        self.assertFalse(_agent_detected(spec))
        (self.home / ".custom").mkdir()
        self.assertTrue(_agent_detected(spec))

    def test_agent_detected_without_home_keeps_directory_heuristic(self) -> None:
        spec = AgentSpec(
            agent="grok",
            server="demo",
            config_path=self.home / ".grok" / "config.toml",
            config_format="toml",
            target_name="demo",
            desired={},
        )

        self.assertFalse(_agent_detected(spec))
        (self.home / ".grok").mkdir()
        self.assertTrue(_agent_detected(spec))

    def test_live_loading_indicator_animates_dots_with_dim_color(self) -> None:
        stream = io.StringIO()
        with _LiveLoadingIndicator(
            stream=stream, animate=True, use_color=True, interval=0.05
        ) as indicator:
            indicator.add("codex")
            time.sleep(0.18)

        output = stream.getvalue()
        self.assertIn("\033[2mcodex loading .\033[0m", output)
        self.assertIn("\033[2mcodex loading ..\033[0m", output)
        self.assertIn("\033[2mcodex loading ...\033[0m", output)
        self.assertTrue(output.endswith("\r\033[K"))

    def test_live_loading_indicator_no_color(self) -> None:
        stream = io.StringIO()
        with _LiveLoadingIndicator(
            stream=stream, animate=True, use_color=False, interval=0.05
        ) as indicator:
            indicator.add("codex")
            time.sleep(0.08)

        output = stream.getvalue()
        self.assertIn("codex loading .", output)
        self.assertNotIn("\033[2m", output)
        self.assertTrue(output.endswith("\r\033[K"))

    def test_live_loading_indicator_inactive_by_default_on_non_tty(self) -> None:
        stream = io.StringIO()
        with _LiveLoadingIndicator(stream=stream) as indicator:
            indicator.add("codex")
            time.sleep(0.05)

        self.assertEqual(stream.getvalue(), "")

    def test_live_loading_indicator_multiple_agents(self) -> None:
        stream = io.StringIO()
        with _LiveLoadingIndicator(
            stream=stream, animate=True, use_color=False, interval=0.05
        ) as indicator:
            indicator.add("codex")
            indicator.add("claude")
            time.sleep(0.08)

        output = stream.getvalue()
        self.assertIn("claude, codex loading .", output)

    def test_probe_mcp_tools_for_specs_with_animation(self) -> None:
        spec = self._spec("codex", self.home / ".codex" / "config.toml")
        stream = io.StringIO()

        def _slow_probe(_spec: AgentSpec, _timeout: int = 15) -> MCPToolProbeResult:
            time.sleep(0.08)
            return MCPToolProbeResult("codex", "OK", "env", ("tool1",))

        with patch("aikito_mcp.probe_mcp_tools", side_effect=_slow_probe):
            results = probe_mcp_tools_for_specs(
                [spec], animate=True, stream=stream, use_color=True
            )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, "OK")
        output = stream.getvalue()
        self.assertIn("\033[2mcodex loading .\033[0m", output)
        self.assertTrue(output.endswith("\r\033[K"))


if __name__ == "__main__":
    unittest.main()
