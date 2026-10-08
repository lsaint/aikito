"""Adoption discovers sources from Agent definitions and registers built-ins."""

import io
import json
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest
from layout_helpers import write_agents

from aikito.adopt import (
    AdoptSkipError,
    apply_adopt_skips,
    build_adopt_plan,
    execute_adoption,
    summarize_adopt_plan,
)
import aikito.adopt as adoption
from aikito.agents import load_agent_definitions
from aikito.mcp import load_agent_specs
from aikito.templating import load_template

CLAUDE_ONLY = load_template("agents/claude-code.toml")


@pytest.fixture
def env(tmp_path):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    write_agents(ws, CLAUDE_ONLY)
    (ws / "global").mkdir()
    return ws, home


def _quiet(plan, **kwargs):
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return execute_adoption(plan, **kwargs)


def _codex_mcp(home: Path) -> None:
    (home / ".codex").mkdir(exist_ok=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers.docs]\nurl = "https://example.com/mcp"\n'
    )


def test_adopt_registers_unregistered_builtin_agent(env):
    ws, home = env
    _codex_mcp(home)

    plan = build_adopt_plan(ws, home)

    assert [r.agent_name for r in plan.agent_registrations] == ["codex"]
    assert summarize_adopt_plan(plan).agent_registrations == 1
    assert plan.can_apply
    assert _quiet(plan, dry_run=False)
    assert (ws / "agents/codex.toml").read_text() == load_template("agents/codex.toml")
    # The adopted MCP now resolves against a registered Agent.
    assert {spec.agent for spec in load_agent_specs(ws, home)} == {"codex"}


def test_dry_run_reports_registration_without_writing(env):
    ws, home = env
    _codex_mcp(home)
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(io.StringIO()):
        assert execute_adoption(build_adopt_plan(ws, home), dry_run=True)
    assert "Agents:       1 registration(s)" in output.getvalue()
    assert "[DRY-RUN AGENT] Would register Codex" in output.getvalue()
    assert not (ws / "agents/codex.toml").exists()


def test_skipping_registration_blocks_dependent_resources(env):
    ws, home = env
    _codex_mcp(home)

    plan = apply_adopt_skips(build_adopt_plan(ws, home), ["agent/codex"])

    assert not plan.can_apply
    assert any(
        f.code == "adopt.agent_unregistered" and f.resource == "mcp/docs"
        for f in plan.findings
    )
    assert not _quiet(plan, dry_run=False)
    assert not (ws / "agents/codex.toml").exists()
    assert not (ws / "mcps/docs.toml").exists()

    both = apply_adopt_skips(build_adopt_plan(ws, home), ["agent/codex", "mcp/docs"])
    assert both.can_apply and not both.agent_registrations


def test_registration_only_for_created_resources(env):
    ws, home = env
    _codex_mcp(home)
    (ws / "mcps").mkdir()
    existing = 'transport = "remote"\nurl = "https://example.com/mcp"\nagents = ["claude-code"]\n'
    (ws / "mcps/docs.toml").write_text(existing)

    plan = build_adopt_plan(ws, home)

    assert plan.agent_registrations == ()
    assert (ws / "mcps/docs.toml").read_text() == existing


def test_unknown_registration_skip_is_rejected(env):
    ws, home = env
    with pytest.raises(AdoptSkipError, match="agent/codex"):
        apply_adopt_skips(build_adopt_plan(ws, home), ["agent/codex"])


def test_invalid_registry_never_plans_registration(tmp_path):
    home, ws = tmp_path / "home", tmp_path / "ws"
    home.mkdir()
    write_agents(ws, "[agents.codex\n")
    _codex_mcp(home)
    plan = build_adopt_plan(ws, home)
    assert plan.agent_registrations == ()
    assert not plan.can_apply


def test_registered_custom_agent_mcp_is_adopted_through_adapter(env):
    ws, home = env
    (ws / "agents/example.toml").write_text(
        """[agents.example]
display_name = "Example"
[agents.example.mcp]
config_path = ".example/config.toml"
config_format = "toml"
"""
    )
    (home / ".example").mkdir()
    (home / ".example/config.toml").write_text(
        '[mcp_servers.custom]\nurl = "https://custom.example.com"\n'
    )

    plan = build_adopt_plan(ws, home)

    server = next(s for s in plan.mcp_servers if s.server_name == "custom")
    assert server.agents == ["example"]
    assert plan.agent_registrations == ()


def test_non_adoptable_adapters_are_not_scanned(env):
    ws, home = env
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/opencode.jsonc").write_text(
        json.dumps({"mcp": {"oc": {"type": "remote", "url": "https://o.example"}}})
    )
    assert build_adopt_plan(ws, home).mcp_servers == []


def test_instructions_follow_definitions(env):
    ws, home = env
    (home / ".config/opencode").mkdir(parents=True)
    (home / ".config/opencode/AGENTS.md").write_text("Shared Rules\n")
    canonical = ws / "global/AGENTS.md"
    canonical.write_text("Shared Rules\n")
    (home / ".claude").mkdir()
    (home / ".claude/CLAUDE.md").symlink_to(canonical)

    plan = build_adopt_plan(ws, home)

    # OpenCode is discovered from its bundled definition, not a hardcoded list.
    sources = {agent: path for agent, path, _ in plan.instructions.sources}
    assert sources["opencode"] == home / ".config/opencode/AGENTS.md"
    # Instruction-only adoption never registers an Agent.
    assert plan.agent_registrations == ()


def test_legacy_agy_instruction_source_is_preserved(env):
    ws, home = env
    (home / ".gemini/config").mkdir(parents=True)
    (home / ".gemini/config/AGENTS.md").write_text("Legacy Rules\n")
    sources = [
        (agent, path)
        for agent, path, _ in build_adopt_plan(ws, home).instructions.sources
    ]
    assert ("agy", home / ".gemini/config/AGENTS.md") in sources


def test_subagent_from_unregistered_builtin_registers_and_validates(env):
    ws, home = env
    (home / ".copilot/agents").mkdir(parents=True)
    (home / ".copilot/agents/fmt.agent.md").write_text(
        '---\ndescription: Format\ntools: ["read"]\n---\nFormat code.\n'
    )

    plan = build_adopt_plan(ws, home)

    assert [r.agent_name for r in plan.agent_registrations] == ["github-copilot"]
    assert plan.can_apply
    assert _quiet(plan, dry_run=False)
    assert "github-copilot" in load_agent_definitions(ws, home)
    assert (ws / "subagents/fmt.md").is_file()


def test_generated_subagents_are_not_adopted(env):
    ws, home = env
    (home / ".claude/agents").mkdir(parents=True)
    (home / ".claude/agents/gen.md").write_text(
        "---\ndescription: Gen\n---\n<!-- generated by aikito from subagents/gen.md -->\nBody\n"
    )
    assert build_adopt_plan(ws, home).subagents == []


@pytest.mark.parametrize("kind", ["instructions", "mcp", "subagent_prompt"])
def test_partial_failure_reports_registered_agents(env, monkeypatch, kind):
    ws, home = env
    _codex_mcp(home)
    (home / ".codex/AGENTS.md").write_text("Shared rules.\n", encoding="utf-8")
    (home / ".copilot/agents").mkdir(parents=True)
    (home / ".copilot/agents/review.agent.md").write_text(
        "---\ndescription: Review\n---\nReview code.\n", encoding="utf-8"
    )
    plan = build_adopt_plan(ws, home)
    target = next(fp.path for fp in plan.file_plans if fp.resource_kind == kind)
    write = adoption._write_text_atomic

    def fail_resource(path, content):
        if path == target:
            raise OSError("Simulated resource write failure")
        write(path, content)

    monkeypatch.setattr(adoption, "_write_text_atomic", fail_resource)
    result = _quiet(plan, dry_run=False)

    assert not result.success
    assert result.agents == ("codex", "github-copilot")
    assert target in result.unwritten_files
    assert (ws / "agents/codex.toml") in result.written_files
    assert (ws / "agents/github-copilot.toml") in result.written_files
    assert set(result.agents) <= set(load_agent_definitions(ws, home))


def test_registration_created_after_planning_blocks_all_writes(env):
    ws, home = env
    _codex_mcp(home)
    plan = build_adopt_plan(ws, home)
    registration = ws / "agents/codex.toml"
    registration.write_text("[agents.codex]\n", encoding="utf-8")

    result = _quiet(plan, dry_run=False)

    assert not result.success
    assert result.agents == ()
    assert not result.written_files
    assert not (ws / "mcps/docs.toml").exists()
    assert registration.read_text(encoding="utf-8") == "[agents.codex]\n"


@pytest.mark.parametrize("source", [".codex/AGENTS.md", ".claude/agents/broken.md"])
def test_non_utf8_source_blocks_adoption(env, source):
    ws, home = env
    path = home / source
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff")

    plan = build_adopt_plan(ws, home)

    assert not plan.can_apply
    assert any(f.code == "adopt.source_invalid" for f in plan.findings)
    assert not _quiet(plan, dry_run=False)


@pytest.mark.parametrize("removed", [False, True])
def test_unbacked_native_source_changes_block_adoption(env, removed):
    ws, home = env
    source = home / ".claude.json"
    source.write_text(
        json.dumps({"mcpServers": {"docs": {"url": "https://example.com"}}}),
        encoding="utf-8",
    )
    plan = build_adopt_plan(ws, home)
    # Native Claude configs may materialize credentials: fingerprint without copying.
    assert source not in plan.backup_sources
    assert source in dict(plan.source_fingerprints)
    if removed:
        source.unlink()
    else:
        source.write_text("{}", encoding="utf-8")

    result = _quiet(plan, dry_run=False)

    assert not result.success
    assert not result.written_files
    assert not (ws / "mcps/docs.toml").exists()


def test_secondary_merged_subagent_source_changes_block_adoption(env):
    ws, home = env
    for directory, filename in (
        (home / ".claude/agents", "review.md"),
        (home / ".copilot/agents", "review.agent.md"),
    ):
        directory.mkdir(parents=True)
        (directory / filename).write_text(
            "---\ndescription: Review\n---\nReview code.\n", encoding="utf-8"
        )
    plan = build_adopt_plan(ws, home)
    secondary = home / ".copilot/agents/review.agent.md"
    assert plan.subagents[0].target_agents == ["claude-code", "github-copilot"]
    secondary.write_text("Changed after planning.\n", encoding="utf-8")

    result = _quiet(plan, dry_run=False)

    assert not result.success
    assert not result.written_files
    assert not (ws / "agents/github-copilot.toml").exists()
    assert not (ws / "subagents/review.md").exists()


@pytest.mark.parametrize("native_format", ["claude", "codex", "custom"])
def test_adopt_sanitizes_sensitive_headers_for_all_import_adapters(env, native_format):
    ws, home = env
    headers = {
        "Authorization": "Bearer dummy-test-secret",
        "X-ApiKey": "dummy-api-key",
        "Cookie": "session=dummy-cookie",
        "X-Password": "dummy-password",
        "X-API-Version": "2026-10-08",
        "X-Token": "Bearer ${EXISTING_TOKEN}",
        "X-Secret": "$EXISTING_SECRET",
    }
    if native_format == "claude":
        source = home / ".claude.json"
        content = json.dumps(
            {
                "mcpServers": {
                    "private-api": {"url": "https://example.com", "headers": headers}
                }
            }
        )
    else:
        if native_format == "custom":
            (ws / "agents/example.toml").write_text(
                "[agents.example]\n[agents.example.mcp]\n"
                'config_path = ".example/config.toml"\nconfig_format = "toml"\n',
                encoding="utf-8",
            )
        directory = home / (".example" if native_format == "custom" else ".codex")
        directory.mkdir()
        source = directory / "config.toml"
        content = (
            '[mcp_servers.private-api]\nurl = "https://example.com"\n'
            "[mcp_servers.private-api.headers]\n"
            + "".join(
                f"{json.dumps(key)} = {json.dumps(value)}\n"
                for key, value in headers.items()
            )
        )
    source.write_text(content, encoding="utf-8")
    plan = build_adopt_plan(ws, home)
    expected = {
        **headers,
        "Authorization": "${AIKITO_PRIVATE_API_AUTHORIZATION}",
        "X-ApiKey": "${AIKITO_PRIVATE_API_X_APIKEY}",
        "Cookie": "${AIKITO_PRIVATE_API_COOKIE}",
        "X-Password": "${AIKITO_PRIVATE_API_X_PASSWORD}",
    }
    assert plan.can_apply
    assert plan.mcp_servers[0].config_data["headers"] == expected
    file_plan = next(fp for fp in plan.file_plans if fp.resource_kind == "mcp")
    assert tomllib.loads(file_plan.desired_content)["headers"] == expected
    output = io.StringIO()
    with redirect_stdout(output), redirect_stderr(output):
        assert execute_adoption(plan, dry_run=True)
        assert execute_adoption(plan, dry_run=False)
    imported = (ws / "mcps/private-api.toml").read_text(encoding="utf-8")
    assert tomllib.loads(imported)["headers"] == expected
    for secret in (
        "dummy-test-secret",
        "dummy-api-key",
        "dummy-cookie",
        "dummy-password",
    ):
        assert secret not in repr(plan)
        assert secret not in output.getvalue()
        assert secret not in imported
    assert source.read_text(encoding="utf-8") == content
