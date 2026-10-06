"""Platform schemas use the complete incoming batch before local mutation."""

from pathlib import Path
from unittest.mock import patch

import pytest
from layout_helpers import write_agents
from workspace_reconcile_smoke import _workspace

from aikito.workspace.payload_io import capture_resources, prepare_payload_writes
from aikito.workspace.resource_write import ResourceContent, ResourceWrite
from aikito.workspace.resources import snapshot_workspace
from aikito.workspace.transactions import PathPolicy, WorkspaceCoreError


@pytest.mark.parametrize(
    "platform,options,valid",
    [
        ("example", '{"model":"example"}', True),
        ("example", '{"unknown":"bad"}', False),
        ("example", '{"maxTokens":true}', False),
        # Platforms without a definition in the resulting workspace stay portable.
        ("undefined", "{}", True),
    ],
)
def test_remote_batch_validates_new_custom_definition(
    tmp_path, platform, options, valid
):
    source, target, staging = (
        tmp_path / name for name in ("source", "target", "staging")
    )
    _workspace(source)
    _workspace(target)
    for root in (source, target):
        (root / "config.toml").write_text('[inbox]\npath = "inbox"\n')
    staging.mkdir()
    write_agents(
        source,
        """[agents.example]
[agents.example.subagents]
config_path = ".example/agents"
config_format = "claude_markdown"
""",
    )
    write_agents(target, "[agents]\n")
    (source / "subagents/review.md").write_text(
        '---\ndescription: "Review"\nagents: ["example"]\n'
        + platform
        + ": "
        + options
        + "\n---\nReview.\n"
    )
    before = snapshot_workspace(target)
    incoming = snapshot_workspace(source)
    keys = ["agent:example", "subagent:review"]
    resources = {key: incoming.resources[key] for key in keys}
    content = ResourceContent.from_workspace(incoming)
    payloads = capture_resources(content, keys)
    writes = [
        ResourceWrite(
            Path(resources[key].parts[0].path),
            resources[key].kind,
            resources[key].fingerprint,
            resources[key].name,
        )
        for key in keys
    ]
    if valid:
        with patch(
            "pathlib.Path.home", side_effect=AssertionError("Ambient home was used")
        ):
            changes, expected = prepare_payload_writes(
                resources,
                payloads,
                before,
                writes,
                staging,
                policy=PathPolicy(),
                home=tmp_path / "explicit-home",
            )
        assert len(changes) == 2
        assert set(expected) >= set(keys)
    else:
        with pytest.raises(
            WorkspaceCoreError, match="Invalid subagent platform configuration"
        ):
            prepare_payload_writes(
                resources, payloads, before, writes, staging, policy=PathPolicy()
            )
    assert not (target / "subagents/review.md").exists()
    assert not (target / "agents/example.toml").exists()
    assert snapshot_workspace(target).resources == before.resources


def _batch(tmp_path, local_subagent, source_agents, target_agents, keys_filter):
    source, target, staging = (
        tmp_path / name for name in ("source", "target", "staging")
    )
    for root, agents in ((source, source_agents), (target, target_agents)):
        _workspace(root)
        (root / "config.toml").write_text('[inbox]\npath = "inbox"\n')
        write_agents(root, agents)
        (root / "subagents/review.md").write_text(local_subagent)
    staging.mkdir()
    (source / "memory/notes").mkdir(parents=True, exist_ok=True)
    (source / "memory/notes/hello.md").write_text("# Hello\n")
    before, incoming = snapshot_workspace(target), snapshot_workspace(source)
    keys = [
        key
        for key, resource in incoming.resources.items()
        if keys_filter(key)
        and (key not in before.resources or before.resources[key] != resource)
    ]
    resources = {key: incoming.resources[key] for key in keys}
    payloads = capture_resources(ResourceContent.from_workspace(incoming), keys)
    writes = [
        ResourceWrite(
            Path(resources[key].parts[0].path),
            resources[key].kind,
            resources[key].fingerprint,
            resources[key].name,
            before=(
                before.resources[key].fingerprint if key in before.resources else None
            ),
        )
        for key in keys
    ]
    return lambda: prepare_payload_writes(
        resources,
        payloads,
        before,
        writes,
        staging,
        policy=PathPolicy(),
        home=tmp_path / "home",
    )


CLAUDE = """[agents.claude-code]
[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"
"""


def test_unrelated_write_ignores_untouched_foreign_platform(tmp_path):
    prepare = _batch(
        tmp_path,
        '---\ndescription: "Review"\nagents: ["claude-code"]\n'
        'codex: {"model":123}\n---\nReview.\n',
        CLAUDE,
        CLAUDE,
        lambda key: key.startswith("memory"),
    )
    changes, _ = prepare()
    assert len(changes) == 1


def test_changed_definition_revalidates_untouched_subagent(tmp_path):
    codex = """[agents.codex]
[agents.codex.subagents]
config_path = ".codex/agents"
config_format = "codex_toml"
"""
    prepare = _batch(
        tmp_path,
        '---\ndescription: "Review"\nagents: ["claude-code"]\n'
        'codex: {"unknown":"bad"}\n---\nReview.\n',
        CLAUDE + codex,
        CLAUDE,
        lambda key: key == "agent:codex",
    )
    with pytest.raises(WorkspaceCoreError, match="unknown field 'unknown'"):
        prepare()
