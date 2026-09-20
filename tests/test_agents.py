"""Unit tests for agents.py: Agent, AgentRegistry, AgentAvailability."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.agents import (
    Agent,
    AgentRegistry,
    check_agent_availability,
    is_agent_installed,
)
from aikito.mcp import load_agents
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

    def test_agent_registry_load_equivalence_with_load_agents(self) -> None:
        registry = AgentRegistry.load(self.ws, self.home)
        mcp_defs = load_agents(self.ws, self.home)

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


if __name__ == "__main__":
    unittest.main()
