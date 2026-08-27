import sys
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_init import (  # noqa: E402
    DEFAULT_MEMORY_INSTRUCTION,
    init_project,
    init_workspace,
    project_sync_validation_error,
)
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
        self.assertTrue((self.target_path / "mcps").is_dir())
        self.assertTrue((self.target_path / ".git").is_dir())

        # Check files
        self.assertTrue((self.target_path / "config.toml").is_file())
        self.assertTrue((self.target_path / "agents.toml").is_file())
        self.assertTrue((self.target_path / "subagents.toml").is_file())
        self.assertTrue((self.target_path / "memory" / "index.md").is_file())
        self.assertTrue((self.target_path / "skills.toml").is_file())
        self.assertTrue((self.target_path / "global" / "AGENTS.md").is_file())
        for skill_name in ("aikito", "durable-memory"):
            self.assertTrue(
                (self.target_path / "skills" / skill_name / "SKILL.md").is_file()
            )

        with (self.target_path / "skills.toml").open("rb") as skills_file:
            self.assertEqual(
                tomllib.load(skills_file)["skills"], ["aikito", "durable-memory"]
            )
        self.assertIn(
            DEFAULT_MEMORY_INSTRUCTION,
            (self.target_path / "global" / "AGENTS.md").read_text(encoding="utf-8"),
        )

        # Check leading slash rule in .gitignore
        gitignore_content = (self.target_path / ".gitignore").read_text(
            encoding="utf-8"
        )
        self.assertIn("/.DS_Store", gitignore_content)
        self.assertIn("/__pycache__/", gitignore_content)
        self.assertIn("/.venv/", gitignore_content)

    def test_init_workspace_creates_a_usable_default_configuration(self) -> None:
        for marker in (
            ".codex",
            ".claude",
            ".gemini/config",
            ".config/opencode",
            ".copilot",
            ".dsh",
            ".grok",
        ):
            (self.fake_home / marker).mkdir(parents=True)
        init_workspace(self.target_path, self.fake_home)

        agents = load_agents(self.target_path, self.fake_home)
        self.assertIn("codex", agents)
        self.assertIn("github-copilot", agents)
        self.assertEqual(
            agents["github-copilot"].skills_path,
            self.fake_home / ".agents/skills",
        )
        self.assertEqual(
            agents["opencode"].skills_path,
            self.fake_home / ".agents/skills",
        )
        self.assertEqual(
            agents["codex"].project_instruction_path,
            Path("AGENTS.md"),
        )
        self.assertEqual(
            agents["claude-code"].project_instruction_path,
            Path(".claude/CLAUDE.md"),
        )
        self.assertEqual(
            agents["agy"].skills_path,
            self.fake_home / ".gemini/antigravity-cli/skills",
        )
        self.assertEqual(load_agent_specs(self.target_path, self.fake_home), [])

        report = get_status_report_data(self.target_path, self.fake_home)
        self.assertTrue(report.agents)

        with (self.target_path / "agents.toml").open("rb") as config_file:
            agent_config = tomllib.load(config_file)["agents"]
        self.assertEqual(
            set(agent_config),
            {
                "codex",
                "claude-code",
                "agy",
                "opencode",
                "github-copilot",
                "dsh",
                "grok",
            },
        )
        for agent_name in agent_config:
            self.assertTrue(agent_config[agent_name]["runner"]["command"])

    def test_init_workspace_registers_only_detected_agents(self) -> None:
        (self.fake_home / ".claude").mkdir()

        with patch("aikito_init.shutil.which", return_value=None):
            init_workspace(self.target_path, self.fake_home)

        with (self.target_path / "agents.toml").open("rb") as config_file:
            agents = tomllib.load(config_file)["agents"]
        self.assertEqual(set(agents), {"claude-code"})

    def test_init_workspace_skip_existing_unless_force(self) -> None:
        (self.fake_home / ".codex").mkdir()
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

    def test_init_workspace_preserves_existing_bundled_skill(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        skill_files = [
            self.target_path / "skills" / name / "SKILL.md"
            for name in ("aikito", "durable-memory")
        ]
        for skill_file in skill_files:
            skill_file.write_text("customized\n", encoding="utf-8")

        init_workspace(self.target_path, self.fake_home, force=True)

        for skill_file in skill_files:
            self.assertEqual(skill_file.read_text(encoding="utf-8"), "customized\n")

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

    def test_init_workspace_requires_all_bundled_skills_before_writing(self) -> None:
        with patch("aikito_init.BUNDLED_SKILL_NAMES", ("aikito", "missing-skill")):
            success = init_workspace(self.target_path, self.fake_home)

        self.assertFalse(success)
        self.assertFalse(self.target_path.exists())

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

    def test_init_project_creates_canonical_skeleton(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        project_path = Path(self.tmp_dir.name) / "example"
        project_path.mkdir()

        with patch("pathlib.Path.home", return_value=self.fake_home):
            project_name = init_project(self.target_path, project_path)

        self.assertEqual(project_name, "example")
        project_dir = self.target_path / "projects" / "example"
        self.assertTrue((project_dir / "AGENTS.md").is_file())
        self.assertEqual((project_dir / "AGENTS.md").read_text(encoding="utf-8"), "")
        self.assertTrue((project_dir / "memory" / "index.md").is_file())
        self.assertTrue((project_dir / "memory" / "notes").is_dir())
        config = (project_dir / "agent.toml").read_text(encoding="utf-8")
        self.assertIn('name = "example"', config)
        self.assertIn('sync_mode = "link"', config)
        self.assertNotIn("agents =", config)

    def test_project_operations_allow_workspace_at_cli_source_root(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        project_path = Path(self.tmp_dir.name) / "example"
        project_path.mkdir()
        project_path = project_path.resolve()

        with patch("aikito_init.CLI_SOURCE_ROOT", self.target_path):
            self.assertEqual(
                init_project(self.target_path, project_path, "example"), "example"
            )
            self.assertIsNone(
                project_sync_validation_error(self.target_path, "example", project_path)
            )

    def test_init_project_is_idempotent(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        project_path = Path(self.tmp_dir.name) / "example"
        project_path.mkdir()

        self.assertEqual(init_project(self.target_path, project_path), "example")
        agents_md = self.target_path / "projects" / "example" / "AGENTS.md"
        self.assertEqual(agents_md.read_text(encoding="utf-8"), "")
        agents_md.write_text("# Custom\n", encoding="utf-8")

        self.assertEqual(init_project(self.target_path, project_path), "example")
        self.assertEqual(agents_md.read_text(encoding="utf-8"), "# Custom\n")

    def test_init_project_rejects_same_name_for_different_path(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        first_path = Path(self.tmp_dir.name) / "first"
        second_path = Path(self.tmp_dir.name) / "second"
        first_path.mkdir()
        second_path.mkdir()

        self.assertEqual(
            init_project(self.target_path, first_path, "example"), "example"
        )
        self.assertIsNone(init_project(self.target_path, second_path, "example"))

    def test_init_project_rejects_unmanaged_runtime_resources(self) -> None:
        init_workspace(self.target_path, self.fake_home)
        project_path = Path(self.tmp_dir.name) / "example"
        unmanaged_memory = project_path / ".agents" / "memory"
        unmanaged_memory.mkdir(parents=True)
        (unmanaged_memory / "note.md").write_text("Keep me\n", encoding="utf-8")

        self.assertIsNone(init_project(self.target_path, project_path))
        self.assertFalse((self.target_path / "projects" / "example").exists())


if __name__ == "__main__":
    unittest.main()
