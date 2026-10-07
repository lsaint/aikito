"""MCP adapters distinguish payload semantics without Agent identity branches."""

import pytest

from aikito.agents import load_agent_definitions
from aikito.mcp.adapters import get_mcp_adapter, read_all_entries
from aikito.mcp.model import BasicTokenAuth, MCPConfigError
from aikito.mcp.loader import load_agent_specs
from layout_helpers import write_agents


@pytest.mark.parametrize(
    "format",
    [
        "toml",
        "grok_toml",
        "jsonc",
        "claude_json",
        "agy_json",
        "copilot_json",
        "dsh_cordis",
    ],
)
def test_adapter_round_trip(format):
    adapter = get_mcp_adapter(format)
    desired, _, missing = adapter.build_desired(
        "https://example.com/mcp", {}, None, None, "docs"
    )
    assert not missing
    text = adapter.update_entry("", "docs", desired)
    assert adapter.read_entry(text, "docs") == desired
    assert read_all_entries(format, text) == {"docs": desired}
    assert read_all_entries(format, adapter.remove_entry(text, "docs")) == {}


def test_toml_header_semantics_are_adapter_bound():
    auth = BasicTokenAuth("user@example.com", "TOKEN", "AUTH")
    codex, *_ = get_mcp_adapter("toml").build_desired(
        "https://example.com", {}, auth, None, "docs"
    )
    grok, *_ = get_mcp_adapter("grok_toml").build_desired(
        "https://example.com", {}, auth, None, "docs"
    )
    assert codex["env_http_headers"] == {"Authorization": "AUTH"}
    assert grok["headers"] == {"Authorization": "${AUTH}"}
    headers = {"Authorization": "${TOKEN}", "X-Static": "plain"}
    codex, *_ = get_mcp_adapter("toml").build_desired(
        "https://example.com", {}, None, headers, "docs"
    )
    grok, *_ = get_mcp_adapter("grok_toml").build_desired(
        "https://example.com", {}, None, headers, "docs"
    )
    assert codex["headers"] == {"X-Static": "plain"}
    assert codex["env_http_headers"] == {"Authorization": "TOKEN"}
    assert grok["headers"] == headers


def test_legacy_grok_normalization_and_custom_reuse(tmp_path):
    home = tmp_path / "home"
    ws = tmp_path / "ws"
    write_agents(
        ws,
        """[agents.grok]
[agents.grok.mcp]
config_path = ".grok/config.toml"
config_format = "toml"
[agents.example]
[agents.example.detect]
commands = ["example"]
paths = [".example"]
[agents.example.mcp]
config_path = ".example/mcp.jsonc"
config_format = "jsonc"
""",
    )
    (ws / "mcps").mkdir()
    (ws / "mcps/docs.toml").write_text(
        'transport = "remote"\nurl = "https://example.com/mcp"\nagents = ["grok", "example"]\n'
    )
    definitions = load_agent_definitions(ws, home)
    # Legacy portable Grok definitions inherit the bundled semantic adapter.
    assert definitions["grok"].mcp.config_format == "toml"
    assert definitions["grok"].mcp.adapter == "grok_toml"
    assert definitions["example"].mcp.config_format == "jsonc"
    specs = {s.agent: s for s in load_agent_specs(ws, home)}
    assert specs["example"].desired["type"] == "remote"
    assert specs["example"].definition.detect.commands == ("example",)
    assert 'config_format = "toml"' in (ws / "agents/grok.toml").read_text()


def test_unknown_adapter_fails_closed():
    with pytest.raises(MCPConfigError, match="Unsupported"):
        get_mcp_adapter("unknown")


def _mcp_capability(tmp_path, body):
    ws = tmp_path / "ws"
    write_agents(ws, body)
    return load_agent_definitions(ws, tmp_path / "home")


def test_explicit_adapter_overrides_bundled_default(tmp_path):
    definitions = _mcp_capability(
        tmp_path,
        """[agents.grok]
[agents.grok.mcp]
config_path = ".grok/config.toml"
config_format = "toml"
adapter = "toml"
[agents.example]
[agents.example.mcp]
config_path = ".example/config.toml"
config_format = "toml"
adapter = "grok_toml"
""",
    )
    assert definitions["grok"].mcp.adapter == "toml"
    # Custom Agents reuse a semantic adapter without any identity branch.
    assert definitions["example"].mcp.adapter == "grok_toml"


def test_changed_format_does_not_inherit_bundled_adapter(tmp_path):
    definitions = _mcp_capability(
        tmp_path,
        """[agents.grok]
[agents.grok.mcp]
config_path = ".grok/mcp.jsonc"
config_format = "jsonc"
""",
    )
    assert definitions["grok"].mcp.adapter == "jsonc"


def test_invalid_adapter_value_is_rejected(tmp_path):
    from aikito.agents import AgentRegistryError

    with pytest.raises(AgentRegistryError, match="mcp.adapter"):
        _mcp_capability(
            tmp_path,
            """[agents.example]
[agents.example.mcp]
config_path = ".example/config.toml"
config_format = "toml"
adapter = ""
""",
        )
