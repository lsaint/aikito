import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_project import collect_project_summaries  # noqa: E402
from aikito_render import render_project_detail, render_projects_table  # noqa: E402


class ProjectSummaryTest(unittest.TestCase):
    def test_collects_project_resources_and_runtime_health(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "project"
            definition = workspace / "projects" / "demo"
            skill = workspace / "skills" / "example"
            notes = definition / "memory" / "notes"
            runtime = project / ".agents"
            skill.mkdir(parents=True)
            notes.mkdir(parents=True)
            (runtime / "skills").mkdir(parents=True)
            (runtime / "memory").mkdir()
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nsync_mode = "link"\n'
                'skills = ["example"]\nmemory = []\n',
                encoding="utf-8",
            )
            (workspace / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
                '[agents.claude-code]\nproject_instruction_path = ".claude/CLAUDE.md"\n',
                encoding="utf-8",
            )
            (definition / "AGENTS.md").write_text("Project rules\n", encoding="utf-8")
            (definition / "memory" / "index.md").write_text(
                "# Index\n", encoding="utf-8"
            )
            (notes / "one.md").write_text("# One\n", encoding="utf-8")
            (project / "AGENTS.md").symlink_to(definition / "AGENTS.md")
            (project / ".claude").mkdir()
            (project / ".claude" / "CLAUDE.md").symlink_to(definition / "AGENTS.md")
            (runtime / "skills" / "example").symlink_to(skill)
            (runtime / "memory" / "index.md").symlink_to(
                definition / "memory" / "index.md"
            )
            (runtime / "memory" / "notes").symlink_to(notes)

            summaries = collect_project_summaries(workspace, root)

        self.assertEqual(len(summaries), 1)
        summary = summaries[0]
        self.assertEqual(summary.name, "demo")
        self.assertEqual(summary.instructions_status, "OK")
        self.assertEqual(summary.skills_count, 1)
        self.assertEqual(summary.memory_notes_count, 1)
        self.assertEqual(summary.runtime_status, "OK")
        rendered = render_projects_table(summaries, False, False)
        detail = render_project_detail(summary, False, False)
        self.assertIn("Instructions", rendered)
        self.assertIn("| 1      |", rendered)
        self.assertIn(f"Canonical directory: {definition}", detail)
        self.assertIn("Project directory:", detail)
        self.assertIn("Instructions: configured", detail)
        self.assertIn("Selected skills: example", detail)
        self.assertIn("Memory: 1 notes | 0 references", detail)
        self.assertNotIn("runtime OK\nSelected skills", detail)
        self.assertIn("Sync: OK", detail)
        self.assertNotIn("Issues:", detail)
        self.assertNotIn("Resource     | Canonical", detail)

    def test_reports_missing_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            definition = root / "projects" / "missing"
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{root / "gone"}"\nskills = []\n', encoding="utf-8"
            )

            summary = collect_project_summaries(root, root)[0]

        self.assertEqual(summary.runtime_status, "PATH MISSING")
        detail = render_project_detail(summary, False, False)
        self.assertIn("Sync: PATH MISSING", detail)
        self.assertIn("Project: directory does not exist:", detail)

    def test_empty_canonical_instructions_only_notice_project_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (root / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            (definition / "memory").mkdir()
            (project / "AGENTS.md").write_text("Repository rules\n", encoding="utf-8")

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        self.assertEqual(summary.instructions_status, "EMPTY")
        self.assertEqual(summary.runtime_status, "OK")
        self.assertIn("Notice: Project-owned AGENTS.md detected:", detail)
        self.assertNotIn("Issues:", detail)

    def test_project_owned_unselected_skill_is_only_a_notice(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project_owned = project / ".agents" / "skills" / "local-skill"
            project_owned.mkdir(parents=True)
            definition.mkdir(parents=True)
            (project_owned / "SKILL.md").write_text("Local\n", encoding="utf-8")
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (root / "agents.toml").write_text("[agents]\n", encoding="utf-8")
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            (definition / "memory").mkdir()

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        self.assertEqual(summary.runtime_status, "OK")
        self.assertIn("Notice: Project-owned skills detected: local-skill", detail)
        self.assertNotIn("Issues:", detail)

    def test_detail_explains_resource_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (root / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            (definition / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
            (definition / "memory").mkdir()
            (definition / "memory" / "index.md").write_text(
                "# Index\n", encoding="utf-8"
            )

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        self.assertIn("Sync: MISSING", detail)
        self.assertIn("Issues:", detail)
        self.assertIn("Instructions (codex) [MISSING]: Missing", detail)
        self.assertIn("Memory [MISSING]", detail)

    def test_detail_renders_each_resource_problem_on_its_own_line(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project}"\nskills = []\n', encoding="utf-8"
            )
            (root / "agents.toml").write_text("[agents]\n", encoding="utf-8")
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            memory = definition / "memory"
            memory.mkdir()
            (memory / "index.md").write_text("# Index\n", encoding="utf-8")
            (memory / "notes").mkdir()
            runtime_memory = project / ".agents" / "memory"
            runtime_memory.mkdir(parents=True)
            (runtime_memory / "index.md").write_text("Local\n", encoding="utf-8")
            (runtime_memory / "notes").mkdir()

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        memory_lines = [
            line for line in detail.splitlines() if line.startswith("  Memory [")
        ]
        self.assertEqual(len(memory_lines), 2)
        self.assertIn("notes:", memory_lines[0])
        self.assertIn("index.md:", memory_lines[1])


if __name__ == "__main__":
    unittest.main()
