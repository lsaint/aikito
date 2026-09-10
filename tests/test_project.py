import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import (
    AmbiguousProjectPathError,
    InvalidProjectConfigError,
    NoAvailableProjectPathError,
    Project,
    ProjectNotFoundError,
    ProjectPrepareConflictError,
    UnsupportedProjectAgentError,
)

from aikito.project import (
    append_candidate_path_to_config,
    collect_project_skill_states,
    collect_project_summaries,
    collect_single_project_skill_states,
    resolve_project_binding,
)
from aikito.render import render_project_detail, render_projects_table

ROOT = Path(__file__).resolve().parents[1]


class ProjectApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.home = (self.root / "home").resolve()
        self.workspace = (self.root / "workspace").resolve()
        self.project_path = (self.root / "code").resolve()
        self.definition = (self.workspace / "projects" / "demo").resolve()
        self.home.mkdir()
        self.project_path.mkdir()
        (self.definition / "memory" / "notes").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill").mkdir(parents=True)
        (self.workspace / "memory" / "shared").mkdir(parents=True)
        (self.workspace / "skills" / "demo-skill" / "SKILL.md").write_text(
            "# Demo skill\n", encoding="utf-8"
        )
        (self.workspace / "memory" / "shared" / "index.md").write_text(
            "# Shared memory\n", encoding="utf-8"
        )
        (self.definition / "memory" / "index.md").write_text(
            "# Project memory\n", encoding="utf-8"
        )
        (self.definition / "AGENTS.md").write_text(
            "# Project instructions\n", encoding="utf-8"
        )
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\n'
            'sync_mode = "link"\nskills = ["demo-skill"]\n'
            'memory = ["shared"]\n',
            encoding="utf-8",
        )
        (self.workspace / "agents.toml").write_text(
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def load_project(self) -> Project:
        return Project.load("demo", workspace=self.workspace, home=self.home)

    def test_loads_and_resolves_the_only_active_path(self) -> None:
        project = self.load_project()

        self.assertEqual(project.name, "demo")
        self.assertEqual(project.paths, (self.project_path.resolve(),))
        self.assertEqual(project.resolve_path(), self.project_path.resolve())

    def test_uses_the_active_workspace_when_not_explicit(self) -> None:
        pointer = self.home / ".config" / "aikito" / "workspace"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(f"{self.workspace}\n", encoding="utf-8")

        with (
            patch("pathlib.Path.home", return_value=self.home),
            patch.dict(os.environ, {}, clear=True),
        ):
            project = Project.load("demo")

        self.assertEqual(project.workspace, self.workspace.resolve())

    def test_rejects_relative_workspace_and_missing_project(self) -> None:
        with self.assertRaises(InvalidProjectConfigError):
            Project.load("demo", workspace="relative")
        with self.assertRaises(ProjectNotFoundError):
            Project.load("missing", workspace=self.workspace)

    def test_rejects_invalid_project_resource_lists(self) -> None:
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\nskills = "bad"\n',
            encoding="utf-8",
        )

        with self.assertRaises(InvalidProjectConfigError):
            self.load_project()

    def test_reports_no_active_path(self) -> None:
        self.project_path.rmdir()
        project = self.load_project()

        with self.assertRaises(NoAvailableProjectPathError):
            project.resolve_path()

    def test_reports_ambiguous_active_paths(self) -> None:
        second_path = self.root / "second-code"
        second_path.mkdir()
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.project_path.as_posix()}", "{second_path.as_posix()}"]\n'
            "skills = []\n",
            encoding="utf-8",
        )
        project = self.load_project()

        with self.assertRaises(AmbiguousProjectPathError) as raised:
            project.resolve_path()

        self.assertEqual(
            raised.exception.paths,
            (self.project_path.resolve(), second_path.resolve()),
        )

    def test_prepare_syncs_project_resources_for_pi(self) -> None:
        project = self.load_project()

        prepared = project.prepare(agent="pi")

        self.assertEqual(prepared.name, "demo")
        self.assertEqual(prepared.agent, "pi")
        self.assertEqual(prepared.cwd, self.project_path.resolve())
        self.assertEqual(dict(prepared.env_overrides), {})
        self.assertEqual(
            (self.project_path / "AGENTS.md").resolve(),
            (self.definition / "AGENTS.md").resolve(),
        )
        self.assertEqual(
            (self.project_path / ".agents" / "skills" / "demo-skill").resolve(),
            (self.workspace / "skills" / "demo-skill").resolve(),
        )
        self.assertEqual(
            (self.project_path / ".agents" / "memory" / "shared").resolve(),
            (self.workspace / "memory" / "shared").resolve(),
        )
        self.assertEqual(
            (self.project_path / ".agents" / "memory" / "index.md").resolve(),
            (self.definition / "memory" / "index.md").resolve(),
        )

    def test_prepare_rejects_unsupported_agent(self) -> None:
        project = self.load_project()

        with self.assertRaises(UnsupportedProjectAgentError):
            project.prepare(agent="codex")

    def test_prepare_supports_any_configured_agent(self) -> None:
        (self.workspace / "agents.toml").write_text(
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            '[agents.codex]\ndisplay_name = "Codex"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n',
            encoding="utf-8",
        )
        project = self.load_project()
        prepared = project.prepare(agent="codex")

        self.assertEqual(prepared.name, "demo")
        self.assertEqual(prepared.agent, "codex")
        self.assertEqual(prepared.cwd, self.project_path.resolve())

    def test_prepare_preserves_unmanaged_project_instructions(self) -> None:
        unmanaged = self.project_path / "AGENTS.md"
        unmanaged.write_text("# Existing\n", encoding="utf-8")
        project = self.load_project()

        with self.assertRaises(ProjectPrepareConflictError):
            project.prepare(agent="pi")

        self.assertEqual(unmanaged.read_text(encoding="utf-8"), "# Existing\n")

    def test_prepare_rejects_drifted_copied_skill(self) -> None:
        config_path = self.definition / "agent.toml"
        config_path.write_text(
            config_path.read_text(encoding="utf-8").replace(
                'sync_mode = "link"', 'sync_mode = "copy"'
            ),
            encoding="utf-8",
        )
        runtime_skill = self.project_path / ".agents" / "skills" / "demo-skill"
        runtime_skill.mkdir(parents=True)
        (runtime_skill / "SKILL.md").write_text("# Drifted\n", encoding="utf-8")
        project = self.load_project()

        with self.assertRaises(ProjectPrepareConflictError):
            project.prepare(agent="pi")

        self.assertEqual(
            (runtime_skill / "SKILL.md").read_text(encoding="utf-8"),
            "# Drifted\n",
        )

    def test_prepare_preflights_missing_sources_before_writing(self) -> None:
        (self.workspace / "skills" / "demo-skill" / "SKILL.md").unlink()
        (self.workspace / "skills" / "demo-skill").rmdir()
        project = self.load_project()

        with self.assertRaises(ProjectPrepareConflictError):
            project.prepare(agent="pi")

        self.assertFalse((self.project_path / ".agents").exists())

    def test_prepare_does_not_modify_workspace_pointer_or_global_instructions(
        self,
    ) -> None:
        pointer = self.home / ".config" / "aikito" / "workspace"
        pointer.parent.mkdir(parents=True)
        pointer.write_text(f"{self.workspace}\n", encoding="utf-8")
        global_instructions = self.workspace / "AGENTS.md"
        global_instructions.write_text("# Global instructions\n", encoding="utf-8")
        project = self.load_project()

        project.prepare(agent="pi")

        self.assertEqual(pointer.read_text(encoding="utf-8"), f"{self.workspace}\n")
        self.assertEqual(
            global_instructions.read_text(encoding="utf-8"),
            "# Global instructions\n",
        )

    def test_prepare_with_explicit_path(self) -> None:
        custom_path = self.home / "other-checkout"
        custom_path.mkdir(parents=True)
        project = self.load_project()

        prepared = project.prepare(agent="pi", path=custom_path)

        self.assertEqual(prepared.name, "demo")
        self.assertEqual(prepared.agent, "pi")
        self.assertEqual(prepared.cwd, custom_path.resolve())
        self.assertTrue((custom_path / "AGENTS.md").is_symlink())
        self.assertTrue(
            (custom_path / ".agents" / "skills" / "demo-skill").is_symlink()
        )
        self.assertTrue((custom_path / ".agents" / "memory" / "index.md").is_symlink())
        # Ensure agent.toml was not modified
        self.assertEqual(project.paths, (self.project_path.resolve(),))

    def test_prepare_with_explicit_path_disambiguates(self) -> None:
        second_path = self.home / "second-checkout"
        second_path.mkdir(parents=True)
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npaths = ["{self.project_path.as_posix()}", "{second_path.as_posix()}"]\n'
            "skills = []\n",
            encoding="utf-8",
        )
        project = self.load_project()

        # Without path argument, it fails with ambiguity
        with self.assertRaises(AmbiguousProjectPathError):
            project.prepare(agent="pi")

        # With explicit path, it disambiguates and succeeds
        prepared = project.prepare(agent="pi", path=second_path)
        self.assertEqual(prepared.cwd, second_path.resolve())

    def test_prepare_rejects_nonexistent_or_invalid_explicit_path(self) -> None:
        project = self.load_project()
        missing = self.home / "nonexistent-checkout"

        with self.assertRaises(NoAvailableProjectPathError):
            project.prepare(agent="pi", path=missing)

        file_path = self.home / "a-file"
        file_path.write_text("hello", encoding="utf-8")
        with self.assertRaises(InvalidProjectConfigError):
            project.prepare(agent="pi", path=file_path)

        with self.assertRaises(InvalidProjectConfigError):
            project.prepare(agent="pi", path="")

        with self.assertRaises(InvalidProjectConfigError):
            project.prepare(agent="pi", path=123)  # type: ignore[arg-type]

    def test_explicit_paths_expand_against_the_loaded_project_home(self) -> None:
        checkout = self.home / "checkout"
        checkout.mkdir()
        project = self.load_project()

        prepared = project.prepare(agent="pi", path="~/checkout")
        updated = project.add_path("~/checkout")

        self.assertEqual(prepared.cwd, checkout.resolve())
        self.assertIn(checkout.resolve(), updated.paths)

    def test_add_path_registers_candidate_path_and_reloads(self) -> None:
        new_path = self.home / "new-machine-checkout"
        new_path.mkdir(parents=True)
        project = self.load_project()

        updated_project = project.add_path(new_path)

        self.assertIn(new_path.resolve(), updated_project.paths)
        config_text = (self.definition / "agent.toml").read_text(encoding="utf-8")
        self.assertIn("new-machine-checkout", config_text)

    def test_add_path_rejects_nonexistent_or_invalid_path(self) -> None:
        project = self.load_project()
        missing = self.home / "missing-path"

        with self.assertRaises(NoAvailableProjectPathError):
            project.add_path(missing)

        file_path = self.home / "not-a-dir"
        file_path.write_text("content", encoding="utf-8")
        with self.assertRaises(InvalidProjectConfigError):
            project.add_path(file_path)

        with self.assertRaises(InvalidProjectConfigError):
            project.add_path("")

        with self.assertRaises(InvalidProjectConfigError):
            project.add_path(None)  # type: ignore[arg-type]


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
                f'path = "{project.as_posix()}"\ndescription = "Demo service"\n'
                'sync_mode = "link"\n'
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
        self.assertEqual(summary.description, "Demo service")
        self.assertEqual(summary.instructions_status, "OK")
        self.assertEqual(summary.skills_count, 1)
        self.assertEqual(summary.memory_notes_count, 1)
        self.assertEqual(summary.runtime_status, "OK")
        rendered = render_projects_table(summaries, False, False)
        detail = render_project_detail(summary, False, False)
        self.assertIn("Instructions", rendered)
        self.assertIn("| 1      |", rendered)
        self.assertIn(f"Canonical path:  {definition}", detail)
        self.assertIn("Project paths:", detail)
        self.assertNotIn("Project directory:", detail)
        self.assertNotIn("Active paths:", detail)
        self.assertIn("Description:  Demo service", detail)
        self.assertIn("configured", detail)
        self.assertIn("Selected skills:", detail)
        self.assertIn("1 notes | 0 references", detail)
        value_start = len("Selected skills:") + 2
        self.assertTrue(
            all(
                line[value_start - 2 : value_start] == " " * 2
                for line in detail.splitlines()
            )
        )
        self.assertNotIn("runtime OK\nSelected skills", detail)
        self.assertIn("OK", detail)
        self.assertNotIn("Issue:", detail)
        self.assertNotIn("Resource     | Canonical", detail)

    def test_rejects_non_string_project_description(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            definition = root / "projects" / "demo"
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                'path = "/tmp/demo"\ndescription = 42\n', encoding="utf-8"
            )

            summary = collect_project_summaries(root, root)[0]

        self.assertEqual(summary.runtime_status, "INVALID CONFIG")
        self.assertEqual(summary.error, "Project description must be a string")

    def test_reports_missing_project_path(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            definition = root / "projects" / "missing"
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{(root / "gone").as_posix()}"\nskills = []\n',
                encoding="utf-8",
            )

            summary = collect_project_summaries(root, root)[0]

        self.assertEqual(summary.runtime_status, "OFFLINE")
        detail = render_project_detail(summary, False, False)
        self.assertIn("OFFLINE", detail)
        self.assertIn("Project is offline on this host", detail)

    def test_empty_canonical_instructions_only_notice_project_owned_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
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
        self.assertIn("Project-owned AGENTS.md detected:", detail)
        self.assertNotIn("Issue:", detail)

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
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
            )
            (root / "agents.toml").write_text("[agents]\n", encoding="utf-8")
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            (definition / "memory").mkdir()

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        self.assertEqual(summary.runtime_status, "OK")
        self.assertIn("Project-owned skills detected: local-skill", detail)
        self.assertNotIn("Issue:", detail)

    def test_detail_explains_resource_sync_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
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

        self.assertIn("MISSING", detail)
        self.assertIn("Issue:", detail)
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
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
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

        memory_lines = [line for line in detail.splitlines() if "Memory [" in line]
        self.assertEqual(len(memory_lines), 2)
        self.assertTrue(any("index.md:" in line for line in memory_lines))
        self.assertTrue(any("notes:" in line for line in memory_lines))

    def test_resolve_project_binding_named_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            p1 = root / "mac"
            p1.mkdir()
            config = {
                "paths": {
                    "mac": p1.as_posix(),
                    "win": "D:/nonexistent/win",
                }
            }
            binding = resolve_project_binding(config, root)
            self.assertEqual(len(binding.entries), 2)
            self.assertEqual(len(binding.active_entries), 1)
            self.assertEqual(binding.active_entries[0].label, "mac")
            self.assertEqual(binding.active_entries[0].resolved_path, p1.resolve())
            self.assertEqual(len(binding.offline_entries), 1)
            self.assertEqual(binding.offline_entries[0].label, "win")

    def test_resolve_project_binding_paths_list(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            p1 = root / "p1"
            p2 = root / "p2"
            p1.mkdir()
            p2.mkdir()
            config = {"paths": [p1.as_posix(), p2.as_posix()]}
            binding = resolve_project_binding(config, root)
            self.assertEqual(len(binding.active_entries), 2)
            self.assertEqual(binding.active_entries[0].label, "1")
            self.assertEqual(binding.active_entries[1].label, "2")

    def test_multi_active_paths_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            p1 = root / "main"
            p2 = root / "worktree"
            definition = workspace / "projects" / "multi"
            p1.mkdir()
            p2.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'paths = ["{p1.as_posix()}", "{p2.as_posix()}"]\nskills = []\n',
                encoding="utf-8",
            )
            (workspace / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            (definition / "AGENTS.md").write_text("Multi rules\n", encoding="utf-8")
            (definition / "memory").mkdir()
            (p1 / "AGENTS.md").symlink_to(definition / "AGENTS.md")
            (p2 / "AGENTS.md").symlink_to(definition / "AGENTS.md")

            summary = collect_project_summaries(workspace, root)[0]
            self.assertEqual(summary.runtime_status, "OK")
            self.assertIn("~/main", summary.path)
            self.assertIn("~/worktree", summary.path)
            self.assertNotIn("(+1 active)", summary.path)
            self.assertEqual(len(summary.active_paths), 2)
            detail = render_project_detail(summary, False, False)
            unicode_detail = render_project_detail(summary, True, False)
            self.assertIn("Project paths:", detail)
            self.assertIn("[1]v ~/main", detail)
            self.assertIn("[2]v ~/worktree", detail)
            self.assertIn("[1]✓ ~/main", unicode_detail)
            self.assertIn("[2]✓ ~/worktree", unicode_detail)
            self.assertNotIn("Active paths:", detail)
            table = render_projects_table([summary], False, False)
            self.assertIn("[1]v", table)

    def test_partially_offline_paths_summary(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            workspace = root / "workspace"
            p1 = root / "main"
            definition = workspace / "projects" / "roam"
            p1.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'[paths]\nmac = "{p1.as_posix()}"\nwin = "D:/offline/win"\n',
                encoding="utf-8",
            )
            (workspace / "agents.toml").write_text(
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n',
                encoding="utf-8",
            )
            (definition / "AGENTS.md").write_text("Roam rules\n", encoding="utf-8")
            (definition / "memory").mkdir()
            (p1 / "AGENTS.md").symlink_to(definition / "AGENTS.md")

            summary = collect_project_summaries(workspace, root)[0]
            self.assertEqual(summary.runtime_status, "OK")
            self.assertIn("~/main", summary.path)
            self.assertIn("D:/offline/win", summary.path)
            self.assertNotIn("(1 offline)", summary.path)
            self.assertEqual(len(summary.active_paths), 1)
            self.assertEqual(len(summary.offline_paths), 1)
            detail = render_project_detail(summary, False, False)
            unicode_detail = render_project_detail(summary, True, False)
            self.assertIn("Project paths:", detail)
            self.assertIn("[mac]v ~/main", detail)
            self.assertIn("[win]- D:/offline/win", detail)
            self.assertIn("[mac]✓ ~/main", unicode_detail)
            self.assertIn("[win]- D:/offline/win", unicode_detail)
            self.assertNotIn("Offline paths:", detail)

    def test_append_candidate_path_to_config(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            toml_path = root / "agent.toml"
            toml_path.write_text('name = "demo"\npath = "~/p1"\n', encoding="utf-8")

            # Appending a new path upgrades path = ... to paths = [...]
            appended = append_candidate_path_to_config(toml_path, "~/p2", root)
            self.assertTrue(appended)
            content = toml_path.read_text(encoding="utf-8")
            self.assertIn("paths =", content)
            self.assertIn('"~/p1"', content)
            self.assertIn('"~/p2"', content)

            # Duplicate should not be appended
            appended_again = append_candidate_path_to_config(toml_path, "~/p2", root)
            self.assertFalse(appended_again)

    def test_resolve_project_binding_deduplication(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            p1 = root / "main"
            p1.mkdir()
            config = {
                "paths": {
                    "mac": p1.as_posix(),
                    "alias": p1.as_posix(),
                }
            }
            binding = resolve_project_binding(config, root)
            self.assertEqual(len(binding.entries), 1)
            self.assertEqual(binding.entries[0].label, "mac")

    def test_append_candidate_path_to_config_inline_table(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            toml_path = root / "agent.toml"
            toml_path.write_text('paths = {mac = "~/a"}\n', encoding="utf-8")

            appended = append_candidate_path_to_config(toml_path, "~/b", root)
            self.assertTrue(appended)
            content = toml_path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            self.assertIn("paths", data)
            self.assertEqual(len(data["paths"]), 2)
            self.assertIn("~/b", data["paths"].values())

    def test_append_candidate_path_to_config_named_table_section(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            toml_path = root / "agent.toml"
            toml_path.write_text('[paths]\nmac = "~/a"\n', encoding="utf-8")

            appended = append_candidate_path_to_config(toml_path, "~/b", root)
            self.assertTrue(appended)
            content = toml_path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            self.assertIn("paths", data)
            self.assertEqual(data["paths"]["mac"], "~/a")
            self.assertEqual(len(data["paths"]), 2)
            self.assertIn("~/b", data["paths"].values())

    def test_append_candidate_path_to_config_pathological_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            toml_path = root / "agent.toml"
            toml_path.write_text('pathological = "x"\npath = "~/a"\n', encoding="utf-8")

            appended = append_candidate_path_to_config(toml_path, "~/b", root)
            self.assertTrue(appended)
            content = toml_path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            self.assertEqual(data.get("pathological"), "x")
            self.assertEqual(data.get("paths"), ["~/a", "~/b"])

    def test_append_candidate_path_to_config_section_context(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            toml_path = root / "agent.toml"
            toml_path.write_text('[other]\npath = "~/z"\n', encoding="utf-8")

            appended = append_candidate_path_to_config(toml_path, "~/b", root)
            self.assertTrue(appended)
            content = toml_path.read_text(encoding="utf-8")
            data = tomllib.loads(content)
            self.assertEqual(data.get("other", {}).get("path"), "~/z")
            self.assertEqual(data.get("paths"), ["~/b"])

    def test_append_candidate_path_to_config_errors(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            missing_path = root / "missing.toml"
            with self.assertRaises(FileNotFoundError):
                append_candidate_path_to_config(missing_path, "~/b", root)

            corrupt_path = root / "corrupt.toml"
            corrupt_path.write_text("bad toml = =", encoding="utf-8")
            with self.assertRaises(ValueError):
                append_candidate_path_to_config(corrupt_path, "~/b", root)

    def test_collect_single_project_skill_states_nested_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            aikito_dir = root / "aikito"
            (aikito_dir / "skills" / "demo").mkdir(parents=True)
            (aikito_dir / "skills" / "demo" / "SKILL.md").write_text(
                "ok", encoding="utf-8"
            )

            main_repo = root / "main"
            worktree = main_repo / "worktree"
            main_repo.mkdir()
            worktree.mkdir()

            (main_repo / ".agents" / "skills" / "demo").mkdir(parents=True)
            (main_repo / ".agents" / "skills" / "demo" / "SKILL.md").write_text(
                "ok", encoding="utf-8"
            )

            states_main = collect_single_project_skill_states(
                aikito_dir, "myproj", main_repo, ["demo"]
            )
            states_wt = collect_single_project_skill_states(
                aikito_dir, "myproj", worktree, ["demo"]
            )

            self.assertEqual(states_main[0].status, "OK")
            self.assertEqual(states_wt[0].status, "MISSING")
            self.assertEqual(
                states_main[0].runtime_path,
                main_repo / ".agents" / "skills" / "demo",
            )
            self.assertEqual(
                states_wt[0].runtime_path,
                worktree / ".agents" / "skills" / "demo",
            )

    def test_collect_project_skill_states_ignores_offline_project(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            aikito_dir = root / "aikito"
            project_dir = aikito_dir / "projects" / "p1"
            project_dir.mkdir(parents=True)
            (project_dir / "agent.toml").write_text(
                'path = "D:/nonexistent/p1"\nsync_mode = "copy"\nskills = ["demo"]\n',
                encoding="utf-8",
            )
            # Offline project has no active entries, should yield no states
            states = collect_project_skill_states(aikito_dir, root)
            self.assertEqual(states, [])

            # Active project yields states
            active_proj = root / "p2"
            active_proj.mkdir()
            project2_dir = aikito_dir / "projects" / "p2"
            project2_dir.mkdir(parents=True)
            (project2_dir / "agent.toml").write_text(
                f'path = "{active_proj.as_posix()}"\nsync_mode = "copy"\nskills = ["demo"]\n',
                encoding="utf-8",
            )
            (aikito_dir / "skills" / "demo").mkdir(parents=True)
            (aikito_dir / "skills" / "demo" / "SKILL.md").write_text(
                "ok", encoding="utf-8"
            )
            states2 = collect_project_skill_states(aikito_dir, root)
            self.assertEqual(len(states2), 1)
            self.assertEqual(states2[0].project_name, "p2")
            self.assertEqual(states2[0].status, "MISSING")


if __name__ == "__main__":
    unittest.main()
