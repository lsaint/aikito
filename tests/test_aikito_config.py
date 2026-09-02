import tempfile
import unittest
from pathlib import Path

from aikito_config import (
    DEFAULT_STALE_MEMORY_DAYS,
    get_inbox_path,
    get_project_memory_stale_days,
    get_workspace_config_path,
    load_workspace_config,
)

ROOT = Path(__file__).resolve().parents[1]


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

    def test_inbox_config_default(self) -> None:
        cfg = load_workspace_config(self.root)
        self.assertEqual(cfg.inbox.path, "inbox")

        inbox_dir = get_inbox_path(self.root)
        self.assertEqual(inbox_dir, (self.root / "inbox").resolve())

    def test_legacy_default_inbox_path_uses_active_workspace(self) -> None:
        config_file = self.root / "config.toml"
        config_file.write_text('[inbox]\npath = "~/aikito/inbox"\n', encoding="utf-8")

        inbox_dir = get_inbox_path(self.root)
        self.assertEqual(inbox_dir, (self.root / "inbox").resolve())

    def test_inbox_config_custom_path(self) -> None:
        config_file = self.root / "config.toml"
        custom_target = self.root / "custom_inbox"
        config_file.write_text(
            f'[inbox]\npath = "{custom_target.as_posix()}"\n', encoding="utf-8"
        )


        cfg = load_workspace_config(self.root)
        self.assertEqual(cfg.inbox.path, custom_target.as_posix())


        inbox_dir = get_inbox_path(self.root)
        self.assertEqual(inbox_dir, custom_target.resolve())

    def test_inbox_config_empty_fallback(self) -> None:
        config_file = self.root / "config.toml"
        config_file.write_text('[inbox]\npath = "   "\n', encoding="utf-8")

        inbox_dir = get_inbox_path(self.root)
        self.assertEqual(inbox_dir, (self.root / "inbox").resolve())


if __name__ == "__main__":
    unittest.main()
