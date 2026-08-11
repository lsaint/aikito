"""Tests for aikito_config module."""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_config import (  # noqa: E402
    DEFAULT_STALE_MEMORY_DAYS,
    get_project_memory_stale_days,
    get_workspace_config_path,
    load_workspace_config,
)


class AikitoConfigTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_default_config_when_no_file(self) -> None:
        cfg_path = get_workspace_config_path(self.root)
        self.assertIsNone(cfg_path)

        cfg = load_workspace_config(self.root)
        self.assertEqual(cfg.memory.stale_days, DEFAULT_STALE_MEMORY_DAYS)

    def test_load_workspace_config_toml(self) -> None:
        config_file = self.root / "config.toml"
        config_file.write_text("[memory]\nstale_days = 14\n", encoding="utf-8")

        self.assertEqual(get_workspace_config_path(self.root), config_file)
        cfg = load_workspace_config(self.root)
        self.assertEqual(cfg.memory.stale_days, 14)

    def test_get_project_memory_stale_days_default(self) -> None:
        proj_dir = self.root / "my-project"
        proj_dir.mkdir(parents=True)

        days = get_project_memory_stale_days(proj_dir, default_stale_days=30)
        self.assertEqual(days, 30)

    def test_get_project_memory_stale_days_override(self) -> None:
        proj_dir = self.root / "my-project"
        proj_dir.mkdir(parents=True)
        agent_toml = proj_dir / "agent.toml"
        agent_toml.write_text('path = "~/my-proj"\n[memory]\nstale_days = 7\n')

        days = get_project_memory_stale_days(proj_dir, default_stale_days=30)
        self.assertEqual(days, 7)


if __name__ == "__main__":
    unittest.main()
