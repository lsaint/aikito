"""Unit tests for agents.py: Agent, AgentRegistry, AgentAvailability."""

from __future__ import annotations

import dataclasses
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.agents import (
    Agent,
    AgentDefinition,
    AgentRegistry,
    AgentRegistryError,
    Target,
    check_agent_availability,
    check_target_availability,
    is_agent_installed,
    load_agent_definitions,
    resolve_targets,
)
from aikito.templating import load_agents_template


class AgentsModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.home.mkdir()
        self.ws.mkdir()
        (self.ws / "agents.toml").write_text(load_agents_template(), encoding="utf-8")

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_agent_registry_load_equivalence_with_definitions(self) -> None:
        registry = AgentRegistry.load(self.ws, self.home)
        mcp_defs = load_agent_definitions(self.ws, self.home)

        self.assertEqual(len(registry), len(mcp_defs))
        self.assertEqual(set(registry), set(mcp_defs.keys()))

        for name, agent in registry.items():
            mcp_def = mcp_defs[name]
            self.assertEqual(agent.name, mcp_def.name)
            self.assertEqual(agent.display_name, mcp_def.display_name)
            self.assertEqual(agent.instruction_path, mcp_def.instruction_path)
            self.assertEqual(
                agent.project_instruction_path, mcp_def.project_instruction_path
            )
            self.assertEqual(agent.skills_path, mcp_def.skills_path)
            # Verify AgentDefinition is an instance of Agent
            self.assertIsInstance(mcp_def, Agent)

    def test_agent_registry_queries(self) -> None:
        registry = AgentRegistry.load(self.ws, self.home)
        self.assertIn("claude-code", registry)
        self.assertNotIn("non-existent-agent", registry)
        self.assertIsNone(registry.get("non-existent-agent"))
        self.assertIsNotNone(registry["claude-code"])
        agent = registry.get("claude-code")
        self.assertIsNotNone(agent)
        assert agent is not None
        self.assertEqual(agent.name, "claude-code")
        self.assertEqual(len(list(registry.values())), len(registry))

    def test_availability_tri_state_for_bundled_agent(self) -> None:
        # Case 1: Binary on path -> installed
        with (
            patch("shutil.which", return_value="/usr/local/bin/claude"),
            patch("pathlib.Path.exists", return_value=False),
        ):
            avail = check_agent_availability("claude-code", self.home)
            self.assertTrue(avail.is_installed)
            self.assertEqual(avail.status, "installed")
            self.assertEqual(avail.evidence, "binary_on_path")
            self.assertTrue(is_agent_installed("claude-code", self.home))

        # Case 2: Marker directory exists -> installed
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        with patch("shutil.which", return_value=None):
            avail = check_agent_availability("claude-code", self.home)
            self.assertTrue(avail.is_installed)
            self.assertEqual(avail.status, "installed")
            self.assertEqual(avail.evidence, "marker_directory")
            self.assertTrue(is_agent_installed("claude-code", self.home))

        # Case 3: Neither binary nor marker exists -> not_installed
        with patch("shutil.which", return_value=None):
            avail = check_agent_availability("codex", self.home)
            self.assertTrue(avail.is_not_installed)
            self.assertEqual(avail.status, "not_installed")
            self.assertEqual(avail.evidence, "marker_not_found")
            self.assertFalse(is_agent_installed("codex", self.home))

    def test_availability_tri_state_for_custom_agent(self) -> None:
        custom_target = self.home / ".mycustom" / "skills"

        # Case 1: Target parent exists -> installed
        custom_target.parent.mkdir(parents=True, exist_ok=True)
        avail = check_agent_availability(
            "custom-agent", self.home, target_path=custom_target
        )
        self.assertTrue(avail.is_installed)
        self.assertEqual(avail.status, "installed")
        self.assertEqual(avail.evidence, "target_parent_exists")
        self.assertTrue(
            is_agent_installed("custom-agent", self.home, target_path=custom_target)
        )

        # Case 2: Target parent does not exist -> unknown
        missing_target = self.home / ".nonexistent_custom" / "skills"
        avail_missing = check_agent_availability(
            "custom-agent", self.home, target_path=missing_target
        )
        self.assertTrue(avail_missing.is_unknown)
        self.assertEqual(avail_missing.status, "unknown")
        self.assertEqual(avail_missing.evidence, "cannot_determine")
        self.assertIsNone(
            is_agent_installed("custom-agent", self.home, target_path=missing_target)
        )

    def test_agent_registry_missing_or_corrupt_file(self) -> None:
        empty_dir = self.root / "empty"
        empty_dir.mkdir()
        self.assertEqual(len(AgentRegistry.load(empty_dir, self.home)), 0)

        bad_dir = self.root / "bad"
        bad_dir.mkdir()
        (bad_dir / "agents.toml").write_text("not toml = = =", encoding="utf-8")
        self.assertEqual(len(AgentRegistry.load(bad_dir, self.home)), 0)


class TargetResolutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.home = self.root / "home"
        self.ws = self.root / "workspace"
        self.home.mkdir()
        self.ws.mkdir()
        (self.ws / "agents.toml").write_text(load_agents_template(), encoding="utf-8")
        (self.ws / "skills.toml").write_text(
            'skills = ["skill-1", "skill-2"]\n', encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_global_skills_deduplication_and_metrics(self) -> None:
        targets = resolve_targets("global_skills", self.ws, self.home)

        # 8 bundled agents declare skills_path
        total_consumers = sum(len(t.consumers) for t in targets)
        self.assertEqual(total_consumers, 8)

        # Deduplicates into exactly 3 physical targets:
        # 1. ~/.agents/skills (shared by 6 agents)
        # 2. ~/.claude/skills (claude-code)
        # 3. ~/.gemini/antigravity-cli/skills (agy)
        self.assertEqual(len(targets), 3)

        # Count metrics verification:
        resource_count = 2  # from skills.toml
        consumer_count = total_consumers  # 8
        operation_count = len(targets)  # 3
        self.assertEqual(resource_count, 2)
        self.assertEqual(consumer_count, 8)
        self.assertEqual(operation_count, 3)

        # Check path distributions
        target_map = {t.path: t for t in targets}
        shared_target = target_map[self.home / ".agents" / "skills"]
        self.assertEqual(len(shared_target.consumers), 6)
        self.assertTrue(shared_target.is_same_object)

        claude_target = target_map[self.home / ".claude" / "skills"]
        self.assertEqual(claude_target.consumers, ("claude-code",))
        self.assertFalse(claude_target.is_same_object)

        agy_target = target_map[self.home / ".gemini" / "antigravity-cli" / "skills"]
        self.assertEqual(agy_target.consumers, ("agy",))
        self.assertFalse(agy_target.is_same_object)

    def test_target_deduplication_uses_physical_identity(self) -> None:
        physical_parent = self.home / "shared-agent"
        physical_parent.mkdir()
        alias_parent = self.home / "shared-agent-alias"
        alias_parent.symlink_to(physical_parent, target_is_directory=True)
        physical = physical_parent / "skills"
        alias = alias_parent / "skills"
        registry = AgentRegistry(
            {
                "agent-a": Agent("agent-a", "Agent A", skills_path=physical),
                "agent-b": Agent("agent-b", "Agent B", skills_path=alias),
            }
        )

        targets = resolve_targets(
            "global_skills", self.ws, self.home, registry=registry
        )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].path, physical)
        self.assertEqual(targets[0].consumers, ("agent-a", "agent-b"))

    def test_distinct_consumer_links_are_not_collapsed_by_common_destination(
        self,
    ) -> None:
        canonical = self.home / ".agents" / "skills"
        canonical.mkdir(parents=True)
        first = self.home / ".first" / "skills"
        second = self.home / ".second" / "skills"
        first.parent.mkdir()
        second.parent.mkdir()
        first.symlink_to(canonical, target_is_directory=True)
        second.symlink_to(canonical, target_is_directory=True)
        registry = AgentRegistry(
            {
                "agent-a": Agent("agent-a", "Agent A", skills_path=first),
                "agent-b": Agent("agent-b", "Agent B", skills_path=second),
            }
        )

        targets = resolve_targets(
            "global_skills", self.ws, self.home, registry=registry
        )

        self.assertEqual(len(targets), 2)

    def test_target_deduplication_honors_case_insensitive_identity(self) -> None:
        registry = AgentRegistry(
            {
                "agent-a": Agent(
                    "agent-a", "Agent A", skills_path=self.home / "Shared" / "skills"
                ),
                "agent-b": Agent(
                    "agent-b", "Agent B", skills_path=self.home / "shared" / "skills"
                ),
            }
        )

        with patch("aikito.agents.is_directory_case_sensitive", return_value=False):
            targets = resolve_targets(
                "global_skills", self.ws, self.home, registry=registry
            )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].consumers, ("agent-a", "agent-b"))

    def test_target_kinds(self) -> None:
        entry_target = Target(
            kind="managed_entry",
            scope="global",
            path=self.home / ".agents" / "skills" / "demo",
            canonical_source=self.ws / "skills" / "demo",
        )
        self.assertEqual(entry_target.kind, "managed_entry")

        container_target = Target(
            kind="managed_container",
            scope="global",
            path=self.home / ".agents" / "skills",
            canonical_source=self.ws / "skills",
        )
        self.assertEqual(container_target.kind, "managed_container")

        consumer_target = Target(
            kind="consumer_link",
            scope="global",
            path=self.home / ".claude" / "skills",
            canonical_source=self.home / ".agents" / "skills",
            consumers=("claude-code",),
        )
        self.assertEqual(consumer_target.kind, "consumer_link")

    def test_target_same_object_evaluation(self) -> None:
        dir_a = self.home / "dir_a"
        dir_a.mkdir()

        # Exact same path
        t1 = Target(
            kind="consumer_link", scope="global", path=dir_a, canonical_source=dir_a
        )
        self.assertTrue(t1.is_same_object)

        # Symlink pointing to source
        link_b = self.home / "link_b"
        link_b.symlink_to(dir_a)
        t2 = Target(
            kind="consumer_link", scope="global", path=link_b, canonical_source=dir_a
        )
        self.assertTrue(t2.is_same_object)

        # Distinct directory
        dir_c = self.home / "dir_c"
        dir_c.mkdir()
        t3 = Target(
            kind="consumer_link", scope="global", path=dir_c, canonical_source=dir_a
        )
        self.assertFalse(t3.is_same_object)

    def test_global_instructions_resolution(self) -> None:
        targets = resolve_targets("global_instructions", self.ws, self.home)
        self.assertGreater(len(targets), 0)
        for t in targets:
            self.assertEqual(t.kind, "consumer_link")
            self.assertEqual(t.scope, "global")
            self.assertEqual(t.canonical_source, self.ws / "global" / "AGENTS.md")

    def test_project_instructions_resolution(self) -> None:
        proj_path = self.root / "myproject"
        proj_path.mkdir()
        targets = resolve_targets(
            "project_instructions",
            self.ws,
            self.home,
            project_path=proj_path,
            project_name="myproj",
        )
        self.assertGreater(len(targets), 0)
        for t in targets:
            self.assertEqual(t.kind, "consumer_link")
            self.assertEqual(t.scope, "project")
            self.assertEqual(
                t.canonical_source, self.ws / "projects" / "myproj" / "AGENTS.md"
            )

    def test_uninstalled_agent_skip_does_not_create_directory(self) -> None:
        fake_agent_target = Target(
            kind="consumer_link",
            scope="global",
            path=self.home / ".nonexistent_agent" / "skills",
            canonical_source=self.home / ".agents" / "skills",
            consumers=("nonexistent-agent",),
        )
        avail = check_target_availability(fake_agent_target, self.home)
        self.assertFalse(avail.is_installed)
        self.assertFalse(fake_agent_target.path.exists())
        self.assertFalse(fake_agent_target.path.parent.exists())

    def test_shared_path_special_case_needs_no_management(self) -> None:
        target_dir = self.home / ".agents" / "skills"
        target_dir.mkdir(parents=True)
        t = Target(
            kind="consumer_link",
            scope="global",
            path=target_dir,
            canonical_source=target_dir,
            consumers=("codex",),
        )
        self.assertTrue(t.is_same_object)

    def test_global_binding_multi_workspace_ownership_unknown(self) -> None:
        from aikito.compat import resolve_symlink_target

        ws_b = self.root / "ws_b"
        ws_b.mkdir()
        (ws_b / "skills" / "shared_skill").mkdir(parents=True)
        (ws_b / "skills" / "shared_skill" / "SKILL.md").write_text(
            "ws_b\n", encoding="utf-8"
        )

        shared_skills_dir = self.home / ".agents" / "skills"
        shared_skills_dir.mkdir(parents=True)
        runtime_link = shared_skills_dir / "shared_skill"
        runtime_link.symlink_to(ws_b / "skills" / "shared_skill")

        ws_a_canonical = self.ws / "skills" / "shared_skill"
        ws_a_canonical.mkdir(parents=True)
        (ws_a_canonical / "SKILL.md").write_text("ws_a\n", encoding="utf-8")

        resolved = resolve_symlink_target(runtime_link)
        self.assertEqual(resolved, (ws_b / "skills" / "shared_skill").resolve())
        self.assertNotEqual(resolved, ws_a_canonical.resolve())
        self.assertTrue(runtime_link.is_symlink())


def _definition(name: str, display_name: str, **fields: object) -> dict[str, object]:
    """Build an expected AgentDefinition dict with v1.50.0 defaults."""
    expected: dict[str, object] = {
        "name": name,
        "display_name": display_name,
        "instruction_path": None,
        "project_instruction_path": None,
        "skills_path": None,
        "mcp_config_path": None,
        "mcp_config_format": "unsupported",
        "mcp_name_style": "verbatim",
        "mcp_reason": "",
        "mcp_live_command": (),
        "mcp_auth_command": (),
        "mcp_builtin_servers": (),
    }
    expected.update(fields)
    return expected


class AgentDefinitionGoldenTests(unittest.TestCase):
    """Freeze the v1.50.0 parsing behavior previously owned by mcp.load_agents."""

    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.ws = Path(self.td.name) / "workspace"
        self.ws.mkdir()
        self.home = Path("/HOME")

    def tearDown(self) -> None:
        self.td.cleanup()

    def _write(self, body: str) -> None:
        (self.ws / "agents.toml").write_text(body, encoding="utf-8")

    def _load(self) -> dict[str, dict[str, object]]:
        return {
            name: dataclasses.asdict(definition)
            for name, definition in load_agent_definitions(self.ws, self.home).items()
        }

    def test_bundled_template(self) -> None:
        self._write(load_agents_template())
        h = self.home
        expected = {
            "codex": _definition(
                "codex",
                "Codex",
                instruction_path=h / ".codex/AGENTS.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
                mcp_config_path=h / ".codex/config.toml",
                mcp_config_format="toml",
                mcp_name_style="underscore",
                mcp_live_command=("codex", "mcp", "list"),
                mcp_auth_command=("codex", "mcp", "login", "{target}"),
                mcp_builtin_servers=("openaiDeveloperDocs",),
            ),
            "claude-code": _definition(
                "claude-code",
                "Claude Code",
                instruction_path=h / ".claude/CLAUDE.md",
                project_instruction_path=Path(".claude/CLAUDE.md"),
                skills_path=h / ".claude/skills",
                mcp_config_path=h / ".claude.json",
                mcp_config_format="claude_json",
                mcp_live_command=("claude", "mcp", "list"),
                mcp_auth_command=("claude", "mcp", "login", "{target}"),
            ),
            "agy": _definition(
                "agy",
                "Antigravity CLI",
                instruction_path=h / ".gemini/GEMINI.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".gemini/antigravity-cli/skills",
                mcp_config_path=h / ".gemini/config/mcp_config.json",
                mcp_config_format="agy_json",
            ),
            "opencode": _definition(
                "opencode",
                "OpenCode",
                instruction_path=h / ".config/opencode/AGENTS.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
                mcp_config_path=h / ".config/opencode/opencode.jsonc",
                mcp_config_format="jsonc",
                mcp_live_command=("opencode", "mcp", "list"),
                mcp_auth_command=("opencode", "mcp", "auth", "{target}"),
            ),
            "github-copilot": _definition(
                "github-copilot",
                "GitHub Copilot CLI",
                instruction_path=h / ".copilot/copilot-instructions.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
                mcp_config_path=h / ".copilot/mcp-config.json",
                mcp_config_format="copilot_json",
                mcp_live_command=("copilot", "mcp", "list"),
            ),
            "dsh": _definition(
                "dsh",
                "DeepSeek Harness",
                instruction_path=h / ".dsh/AGENTS.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
                mcp_config_path=h / ".dsh/cordis.patch.yml",
                mcp_config_format="dsh_cordis",
            ),
            "grok": _definition(
                "grok",
                "Grok Build",
                instruction_path=h / ".grok/rules/aikito.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
                mcp_config_path=h / ".grok/config.toml",
                mcp_config_format="toml",
                mcp_live_command=("grok", "mcp", "list"),
            ),
            "pi": _definition(
                "pi",
                "Pi",
                instruction_path=h / ".pi/agent/AGENTS.md",
                project_instruction_path=Path("AGENTS.md"),
                skills_path=h / ".agents/skills",
            ),
        }
        self.assertEqual(self._load(), expected)
        for definition in load_agent_definitions(self.ws, self.home).values():
            self.assertIsInstance(definition, AgentDefinition)
            self.assertIsInstance(definition, Agent)

    def test_custom_agents_and_legacy_coercion(self) -> None:
        self._write(
            """
[agents.custom]
display_name = "Custom"
[agents.custom.mcp]
config_path = "/abs/cfg.json"
config_format = 123
name_style = "underscore"
reason = 7
live_command = ["c", "mcp", "list"]
auth_command = ["c", "login", "{target}"]
builtin_mcps = ["a"]
[agents.nullcmd]
[agents.nullcmd.mcp]
config_path = "x.toml"
[agents.bare]
"""
        )
        self.assertEqual(
            self._load(),
            {
                "custom": _definition(
                    "custom",
                    "Custom",
                    mcp_config_path=Path("/abs/cfg.json"),
                    mcp_config_format="123",
                    mcp_name_style="underscore",
                    mcp_reason="7",
                    mcp_live_command=("c", "mcp", "list"),
                    mcp_auth_command=("c", "login", "{target}"),
                    mcp_builtin_servers=("a",),
                ),
                "nullcmd": _definition(
                    "nullcmd", "nullcmd", mcp_config_path=self.home / "x.toml"
                ),
                "bare": _definition("bare", "bare"),
            },
        )
        definitions = load_agent_definitions(self.ws, self.home)
        self.assertTrue(definitions["nullcmd"].supports_mcp)
        self.assertFalse(definitions["bare"].supports_mcp)

    def test_empty_registry(self) -> None:
        self._write("[agents]\n")
        self.assertEqual(load_agent_definitions(self.ws, self.home), {})

    def test_missing_agents_config_raises(self) -> None:
        with self.assertRaises(AgentRegistryError):
            load_agent_definitions(self.ws, self.home)

    def test_error_messages_are_frozen(self) -> None:
        cases = {
            "[agents.a]\nmcp = 1\n": "Agent 'a' mcp section must be a table",
            '[agents.a.mcp]\nconfig_format = "toml"\n': (
                "Agent 'a' requires a string 'mcp.config_path'"
            ),
            '[agents.a.mcp]\nconfig_path = "x"\nbuiltin_mcps = [1]\n': (
                "Agent 'a' mcp.builtin_mcps must be a list of strings"
            ),
        }
        for body, message in cases.items():
            with self.subTest(body=body):
                self._write(body)
                with self.assertRaises(AgentRegistryError) as ctx:
                    load_agent_definitions(self.ws, self.home)
                self.assertEqual(str(ctx.exception), message)


if __name__ == "__main__":
    unittest.main()
