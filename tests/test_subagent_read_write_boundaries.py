"""Portable platform metadata is strict on writes and tolerant on runtime reads."""

from unittest.mock import patch

import pytest
from layout_helpers import write_agents

from aikito.add import add_subagent
from aikito.adopt import InstructionsAdoption, SubagentAdoption, _build_file_plans
from aikito.agents import Agent, DetectionCapability
from aikito.doctor import check_config_syntax
from aikito.frontmatter import _parse_markdown_frontmatter
from aikito.render import get_consumer_display_name
from aikito.subagent import build_subagent_plan, load_subagent_definitions
from aikito.subagent_adapters import SubagentConfigError
from aikito.workspace.layout import parse_subagent_file


@pytest.fixture
def workspace(tmp_path):
    ws, home = tmp_path / "ws", tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    write_agents(
        ws,
        """[agents.claude-code]
[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"
""",
    )
    (ws / "skills.toml").write_text("skills = []\n")
    (ws / "mcps").mkdir()
    return ws, home


def test_frontmatter_only_parses_declared_platform_tables():
    source = """---
description:
  Use when: reviewing code
metadata:
  author: me
example:
  model: custom
---
Review code.
"""
    metadata, _ = _parse_markdown_frontmatter(source, platform_names={"example"})
    assert metadata["description"] == "Use when: reviewing code"
    assert metadata["metadata"] == "author: me"
    assert metadata["example"] == {"model": "custom"}
    default, _ = _parse_markdown_frontmatter(source)
    assert default["example"] == "model: custom"


def test_add_import_preserves_colon_description_and_metadata(workspace, tmp_path):
    ws, home = workspace
    source = tmp_path / "review.md"
    source.write_text("""---
description:
  Use when: reviewing code
metadata:
  author: me
claude-code:
  model: example
---
Review code.
""")
    assert add_subagent(ws, home, from_source=source, agents=["claude-code"])
    metadata, _ = parse_subagent_file(ws / "subagents/review.md")
    assert metadata["description"] == "Use when: reviewing code"
    assert metadata["claude-code"] == {"model": "example"}
    assert "metadata" not in metadata


def test_add_does_not_treat_reserved_dict_values_as_platforms(workspace, tmp_path):
    ws, home = workspace
    source = tmp_path / "reserved.md"
    source.write_text(
        '---\ndescription: {"hint":"review code"}\nmetadata: {"author":"me"}\n---\nReview code.\n'
    )
    assert add_subagent(ws, home, from_source=source, agents=["claude-code"])


def test_runtime_skips_unregistered_platform_but_preserves_source(workspace):
    ws, home = workspace
    source = ws / "subagents/review.md"
    text = '---\ndescription: "Review"\nagents: ["claude-code"]\ncodex: {"model":123}\n---\nReview.\n'
    source.write_text(text)
    with patch(
        "pathlib.Path.home", side_effect=AssertionError("Ambient home was used")
    ):
        definitions = load_subagent_definitions(ws, home=home)
        plan = build_subagent_plan(ws, home)
    assert definitions["review"].platform_configs["codex"] == {"model": 123}
    assert plan.can_apply
    assert any(
        op.action == "CREATE" and op.target.agent == "claude-code"
        for op in plan.operations
    )
    assert source.read_text() == text
    findings = check_config_syntax(ws, home).findings
    assert not any(f.status == "FAIL" and "subagent" in f.message for f in findings)
    assert any(f.status == "WARN" and "platform 'codex'" in f.message for f in findings)


def test_runtime_still_rejects_invalid_registered_platform(workspace):
    ws, home = workspace
    (ws / "subagents/review.md").write_text(
        '---\ndescription: "Review"\nagents: ["claude-code"]\nclaude-code: {"model":123}\n---\nReview.\n'
    )
    with pytest.raises(SubagentConfigError, match="invalid value"):
        build_subagent_plan(ws, home)


def test_add_still_rejects_unregistered_platform(workspace, tmp_path):
    ws, home = workspace
    source = tmp_path / "foreign.md"
    source.write_text(
        '---\ndescription: "Review"\ncodex: {"model":"example"}\n---\nReview.\n'
    )
    assert not add_subagent(ws, home, from_source=source, agents=["claude-code"])
    assert not (ws / "subagents/foreign.md").exists()


def test_consumer_label_uses_loaded_custom_definition():
    agent = Agent(
        "example", "Example", detect=DetectionCapability(("example-cli",), ())
    )
    with patch(
        "pathlib.Path.home", side_effect=AssertionError("Ambient home was used")
    ):
        assert get_consumer_display_name(agent) == "example-cli"


def test_adopt_loads_definitions_once_with_supplied_home(workspace):
    from aikito.agents import load_agent_definitions

    ws, home = workspace
    subagents = [
        SubagentAdoption(
            name,
            "Review",
            name,
            "Review code.",
            ["claude-code"],
            home / f"{name}.md",
            {"claude-code": {"model": "example"}},
        )
        for name in ("first", "second")
    ]
    with (
        patch("pathlib.Path.home", side_effect=AssertionError("Ambient home was used")),
        patch(
            "aikito.adopt.load_agent_definitions", wraps=load_agent_definitions
        ) as loader,
    ):
        plans = _build_file_plans(
            ws, InstructionsAdoption([]), [], subagents, home=home
        )
    assert len(plans) == 2
    loader.assert_called_once_with(ws, home)


def test_adopt_preserves_colon_description(workspace):
    from aikito.adopt import scan_subagents

    ws, home = workspace
    directory = home / ".claude/agents"
    directory.mkdir()
    (directory / "review.md").write_text(
        "---\ndescription:\n  Use when: reviewing code\nmetadata:\n  author: me\n---\nReview code.\n"
    )
    subagents = scan_subagents(ws, home)
    assert subagents[0].description == "Use when: reviewing code"
