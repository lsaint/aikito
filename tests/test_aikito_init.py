import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_init import init_workspace  # noqa: E402
from aikito_mcp import load_agent_specs, load_agents  # noqa: E402
from aikito_status import get_status_report_data  # noqa: E402


class AikitoInitTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.target_path = Path(self.tmp_dir.name) / "test_workspace"
        self.fake_home = Path(self.tmp_dir.name) / "fake_home"
        self.fake_home.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_init_workspace_creates_structure_and_gitignore(self) -> None:
        success = init_workspace(self.target_path, self.fake_home)
        self.assertTrue(success)

        # Check directories
        self.assertTrue((self.target_path / "memory" / "notes").is_dir())
        self.assertTrue((self.target_path / "projects").is_dir())
        self.assertTrue((self.target_path / "skills").is_dir())
        self.assertTrue((self.target_path / ".git").is_dir())

        # Check files
        self.assertTrue((self.target_path / "agents.toml").is_file())
        self.assertTrue((self.target_path / "mcps.toml").is_file())
        self.assertTrue((self.target_path / "subagents.toml").is_file())
        self.assertTrue((self.target_path / "memory" / "index.md").is_file())
        self.assertTrue((self.target_path / "skills.toml").is_file())
        self.assertTrue((self.target_path / "global" / "AGENTS.md").is_file())

        # Check leading slash rule in .gitignore
        gitignore_content = (self.target_path / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn("/.DS_Store", gitignore_content)
        self.assertIn("/__pycache__/", gitignore_content)
        self.assertIn("/.venv/", gitignore_content)

    def test_init_workspace_creates_a_usable_empty_configuration(self) -> None:
        init_workspace(self.target_path, self.fake_home)

        agents = load_agents(self.target_path, self.fake_home)
        self.assertIn("codex", agents)
        self.assertEqual(
            agents["agy"].skills_path,
            self.fake_home / ".gemini/antigravity-cli/skills",
        )
        self.assertEqual(load_agent_specs(self.target_path, self.fake_home), [])

        report = get_status_report_data(self.target_path, self.fake_home)
        self.assertTrue(report.agents)

    def test_init_workspace_skip_existing_unless_force(self) -> None:
        # Initial run
        init_workspace(self.target_path, self.fake_home)
        agents_toml = self.target_path / "agents.toml"
        agents_toml.write_text("# Custom User Edit\n", encoding="utf-8")

        # Second run without force -> should skip
        init_workspace(self.target_path, self.fake_home, force=False)
        self.assertEqual(
            agents_toml.read_text(encoding="utf-8"), "# Custom User Edit\n"
        )

        # Run with force -> should overwrite
        init_workspace(self.target_path, self.fake_home, force=True)
        self.assertIn("[agents.codex]", agents_toml.read_text(encoding="utf-8"))

    def test_init_workspace_rejects_cli_source_tree_before_writing(self) -> None:
        source_root = Path(self.tmp_dir.name) / "source"
        source_root.mkdir()

        for target in (source_root, source_root / "nested-workspace"):
            with (
                self.subTest(target=target),
                patch("aikito_init.CLI_SOURCE_ROOT", source_root),
            ):
                success = init_workspace(target, self.fake_home, force=True)

            self.assertFalse(success)
            self.assertFalse((target / "agents.toml").exists())

    def test_init_workspace_rejects_source_checkout_markers(self) -> None:
        self.target_path.mkdir()
        (self.target_path / "LICENSE").write_text("MIT\n", encoding="utf-8")
        (self.target_path / "README.md").write_text("# Aikito\n", encoding="utf-8")
        (self.target_path / "bin").mkdir()
        (self.target_path / "bin" / "aikito").write_text("", encoding="utf-8")

        success = init_workspace(self.target_path, self.fake_home, force=True)

        self.assertFalse(success)
        self.assertFalse((self.target_path / "agents.toml").exists())

    def test_init_workspace_rejects_unknown_non_empty_directory(self) -> None:
        self.target_path.mkdir()
        existing_file = self.target_path / "notes.txt"
        existing_file.write_text("Keep me\n", encoding="utf-8")

        success = init_workspace(self.target_path, self.fake_home, force=True)

        self.assertFalse(success)
        self.assertEqual(existing_file.read_text(encoding="utf-8"), "Keep me\n")
        self.assertFalse((self.target_path / "agents.toml").exists())


if __name__ == "__main__":
    unittest.main()
