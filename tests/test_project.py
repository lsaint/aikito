from layout_helpers import write_agents
import aikito
import os
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import (
    AmbiguousProjectPathError,
    InvalidProjectConfigError,
    InvalidWorkspaceError,
    NoAvailableProjectPathError,
    Project,
    ProjectError,
    ProjectNotFoundError,
    ProjectPrepareConflictError,
    UnsupportedProjectAgentError,
    WorkspaceError,
    WorkspaceNotFoundError,
    __version__,
)
from aikito.project_config import (
    append_candidate_path_to_config,
    resolve_project_binding,
)
from aikito.project import (
    ProjectResourceDetail,
    ProjectSummary,
    classify_project_skill_state,
    collect_project_skill_states,
    collect_project_summaries,
    collect_single_project_skill_states,
    find_selected_runtime_conflicts,
    map_skill_operation_to_project_state,
    plan_runtime_cleanup,
)
from aikito.render import render_project_detail, render_projects_table
from aikito.skill_plan import (
    ObservedSkill,
    SkillOperation,
    SkillTarget,
    plan_single_skill,
)
from aikito.skill_runtime import inspect_skill_target
from aikito.skill_state import (
    ProjectSkillStateDocument,
    SkillStateRecord,
    save_project_skill_state,
)

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
        (self.definition / "AGENTS.md").write_text(
            "# Project instructions\n", encoding="utf-8"
        )
        (self.definition / "agent.toml").write_text(
            f'name = "demo"\npath = "{self.project_path.as_posix()}"\n'
            'sync_mode = "link"\nskills = ["demo-skill"]\n'
            'memory = ["shared"]\n',
            encoding="utf-8",
        )
        write_agents(
            self.workspace,
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n',
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
            (self.project_path / ".agents" / "memory" / "notes").resolve(),
            (self.definition / "memory" / "notes").resolve(),
        )

    def test_prepare_rejects_unsupported_agent(self) -> None:
        project = self.load_project()

        with self.assertRaises(UnsupportedProjectAgentError):
            project.prepare(agent="codex")

    def test_prepare_supports_any_configured_agent(self) -> None:
        write_agents(
            self.workspace,
            '[agents.pi]\ndisplay_name = "Pi"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n'
            '[agents.codex]\ndisplay_name = "Codex"\n'
            'project_instruction_path = "AGENTS.md"\n'
            'skills_path = ".agents/skills"\n',
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
        self.assertTrue((custom_path / ".agents" / "memory" / "notes").is_symlink())
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

    def test_public_api_exports_and_exception_hierarchy(self) -> None:
        expected_exports = [
            "Project",
            "PreparedProject",
            "ProjectError",
            "ProjectNotFoundError",
            "InvalidProjectConfigError",
            "NoAvailableProjectPathError",
            "AmbiguousProjectPathError",
            "UnsupportedProjectAgentError",
            "ProjectPrepareConflictError",
            "Workspace",
            "WorkspaceError",
            "WorkspaceFinding",
            "WorkspaceInspection",
            "WorkspaceNotFoundError",
            "InvalidWorkspaceError",
            "WorkspaceOperationView",
            "WorkspaceProjectView",
            "WorkspaceSyncPreview",
            "__version__",
        ]
        self.assertEqual(set(aikito.__all__), set(expected_exports))
        self.assertIsInstance(__version__, str)

        exception_classes = [
            ProjectNotFoundError,
            InvalidProjectConfigError,
            NoAvailableProjectPathError,
            AmbiguousProjectPathError,
            UnsupportedProjectAgentError,
            ProjectPrepareConflictError,
        ]
        for exc_cls in exception_classes:
            self.assertTrue(issubclass(exc_cls, ProjectError))
            self.assertTrue(issubclass(exc_cls, RuntimeError))

        workspace_exception_classes = [
            WorkspaceNotFoundError,
            InvalidWorkspaceError,
        ]
        for exc_cls in workspace_exception_classes:
            self.assertTrue(issubclass(exc_cls, WorkspaceError))
            self.assertTrue(issubclass(exc_cls, RuntimeError))

    def test_prepared_project_immutability_and_dataclass_contract(self) -> None:
        project = self.load_project()
        prepared = project.prepare(agent="pi")

        self.assertEqual(prepared.name, "demo")
        self.assertEqual(prepared.agent, "pi")
        self.assertEqual(prepared.cwd, self.project_path.resolve())
        self.assertEqual(dict(prepared.env_overrides), {})

        with self.assertRaises((AttributeError, TypeError)):
            prepared.name = "mutated"  # type: ignore[misc]
        with self.assertRaises((AttributeError, TypeError)):
            prepared.env_overrides["KEY"] = "VAL"  # type: ignore[index]

    def test_prepare_repeated_preserves_result(self) -> None:
        project = self.load_project()
        prep1 = project.prepare(agent="pi")
        prep2 = project.prepare(agent="pi")

        self.assertEqual(prep1.name, prep2.name)
        self.assertEqual(prep1.agent, prep2.agent)
        self.assertEqual(prep1.cwd, prep2.cwd)
        self.assertEqual(dict(prep1.env_overrides), dict(prep2.env_overrides))
        self.assertTrue((self.project_path / "AGENTS.md").is_symlink())
        self.assertTrue(
            (self.project_path / ".agents" / "skills" / "demo-skill").is_symlink()
        )


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

            write_agents(
                workspace,
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
                '[agents.claude-code]\nproject_instruction_path = ".claude/CLAUDE.md"\n',
            )
            (definition / "AGENTS.md").write_text("Project rules\n", encoding="utf-8")
            (notes / "one.md").write_text("# One\n", encoding="utf-8")
            (project / "AGENTS.md").symlink_to(definition / "AGENTS.md")
            (project / ".claude").mkdir()
            (project / ".claude" / "CLAUDE.md").symlink_to(definition / "AGENTS.md")
            (runtime / "skills" / "example").symlink_to(skill)
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
        self.assertIn("Instr", rendered)
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
            write_agents(
                root, '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
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
            write_agents(root, "[agents]\n")
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
            write_agents(
                root, '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
            )
            (definition / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
            (definition / "memory").mkdir()

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        self.assertIn("MISSING", detail)
        self.assertIn("Issue:", detail)
        self.assertIn("Instructions (codex) [MISSING]: Missing", detail)
        self.assertIn("Fix:", detail)
        self.assertIn(
            "Run 'aikito sync project demo' (or 'aikito sync') to reconcile runtime",
            detail,
        )

    def test_detail_renders_conflict_fix_hint(self) -> None:
        summary = ProjectSummary(
            name="demo",
            path="~/demo",
            sync_mode="symlink",
            instructions_status="DRIFT",
            skills_count=0,
            memory_notes_count=0,
            runtime_status="DRIFT",
            config_path=Path("/workspace/projects/demo/agent.toml"),
            details=(
                ProjectResourceDetail(
                    resource="Instructions (codex)",
                    canonical_path=Path("/workspace/projects/demo/AGENTS.md"),
                    runtime_path=Path("~/demo/AGENTS.md"),
                    status="CONFLICT",
                    detail="File exists but does not point to workspace instructions",
                ),
            ),
        )
        detail = render_project_detail(summary, False, False)
        self.assertIn("Issue:", detail)
        self.assertIn("Fix:", detail)
        self.assertIn(
            "Remove unmanaged files from .agents/ or reconcile conflicting resources",
            detail,
        )

    def test_detail_renders_copied_skill_drift_fix_hint(self) -> None:
        summary = ProjectSummary(
            name="demo",
            path="~/demo",
            sync_mode="copy",
            instructions_status="OK",
            skills_count=1,
            memory_notes_count=0,
            runtime_status="DRIFT",
            config_path=Path("/workspace/projects/demo/agent.toml"),
            details=(
                ProjectResourceDetail(
                    resource="Skills",
                    canonical_path=Path("/workspace/skills"),
                    runtime_path=Path("~/demo/.agents/skills"),
                    status="DRIFT",
                    detail="my-skill: Copied project skill drifted from workspace skill",
                ),
            ),
        )
        detail = render_project_detail(summary, False, False)
        self.assertIn("Issue:", detail)
        self.assertIn("Fix:", detail)
        self.assertIn(
            "Run 'aikito diff' to review changes, then 'aikito sync project demo --force' after review",
            detail,
        )

    def test_real_workspace_copy_drift_integration(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project_path = root / "demo"
            canonical_skill = workspace / "skills" / "my-skill"
            runtime_skill = project_path / ".agents" / "skills" / "my-skill"
            project_dir = workspace / "projects" / "demo"
            canonical_skill.mkdir(parents=True)
            runtime_skill.mkdir(parents=True)
            project_dir.mkdir(parents=True)
            write_agents(workspace, "[agents]\n")
            (project_dir / "agent.toml").write_text(
                f'path = "{project_path.as_posix()}"\nsync_mode = "copy"\nskills = ["my-skill"]\n',
                encoding="utf-8",
            )
            (canonical_skill / "SKILL.md").write_text("canonical\n", encoding="utf-8")
            (runtime_skill / "SKILL.md").write_text("modified\n", encoding="utf-8")

            summaries = collect_project_summaries(workspace, root)
            self.assertEqual(len(summaries), 1)
            summary = summaries[0]

            self.assertEqual(summary.runtime_status, "DRIFT")
            self.assertTrue(summary.has_copied_skill_drift)
            self.assertFalse(summary.is_sync_fixable)
            self.assertEqual(summary.fix_action, "aikito diff")
            self.assertIn(
                "Run 'aikito diff' to review changes, then 'aikito sync project demo --force' after review",
                summary.fix_hint,
            )

            detail = render_project_detail(summary, False, False)
            self.assertIn("Issue:", detail)
            self.assertIn("Fix:", detail)
            self.assertIn(
                "Run 'aikito diff' to review changes, then 'aikito sync project demo --force' after review",
                detail,
            )

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

            write_agents(root, "[agents]\n")
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            memory = definition / "memory"
            memory.mkdir()
            (memory / "notes").mkdir()
            runtime_memory = project / ".agents" / "memory"
            runtime_memory.mkdir(parents=True)
            (runtime_memory / "notes").mkdir()

            summary = collect_project_summaries(root, root)[0]
            detail = render_project_detail(summary, False, False)

        memory_lines = [line for line in detail.splitlines() if "Memory [" in line]
        self.assertEqual(len(memory_lines), 1)
        self.assertIn("notes:", memory_lines[0])

    def test_project_memory_ignores_non_notes_files_in_canonical_memory(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "project"
            definition = root / "projects" / "demo"
            project.mkdir()
            definition.mkdir(parents=True)
            (definition / "agent.toml").write_text(
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
            )
            write_agents(root, "[agents]\n")
            (definition / "AGENTS.md").write_text("", encoding="utf-8")
            memory = definition / "memory"
            memory.mkdir()
            (memory / "notes").mkdir()
            (memory / "README.md").write_text(
                "# Project memory docs\n", encoding="utf-8"
            )
            runtime_memory = project / ".agents" / "memory"
            runtime_memory.mkdir(parents=True)
            (runtime_memory / "notes").symlink_to(memory / "notes")

            summary = collect_project_summaries(root, root)[0]
            self.assertEqual(summary.runtime_status, "OK")

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
            write_agents(
                workspace, '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
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
            self.assertIn("2/2", table)

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
            write_agents(
                workspace, '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
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

    def test_collect_project_skill_states_distinguishes_update_from_drift(self) -> None:
        from aikito.skill_state import (
            SkillStateRecord,
            ProjectSkillStateDocument,
            calculate_directory_fingerprint,
            save_project_skill_state,
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            aikito_dir = root / "aikito"
            project_dir = root / "p1"
            project_dir.mkdir()
            p1_agents_skills = project_dir / ".agents" / "skills" / "demo"
            p1_agents_skills.mkdir(parents=True)
            (p1_agents_skills / "SKILL.md").write_text("version 1", encoding="utf-8")

            proj_def = aikito_dir / "projects" / "p1"
            proj_def.mkdir(parents=True)
            (proj_def / "agent.toml").write_text(
                f'path = "{project_dir.as_posix()}"\nsync_mode = "copy"\nskills = ["demo"]\n',
                encoding="utf-8",
            )
            canon = aikito_dir / "skills" / "demo"
            canon.mkdir(parents=True)
            (canon / "SKILL.md").write_text(
                "version 2 (upstream updated)", encoding="utf-8"
            )

            # Case 1: Active record with baseline == version 1 (R == B != C) -> UPDATE
            b_fp, _ = calculate_directory_fingerprint(p1_agents_skills)
            doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=aikito_dir.as_posix(),
                project_name="p1",
                physical_checkout=project_dir.as_posix(),
                records={
                    "demo": SkillStateRecord(
                        skill_name="demo",
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint=b_fp,
                        baseline_origin="write",
                        last_observed_selected=True,
                    )
                },
            )
            save_project_skill_state(root, doc)

            states = collect_project_skill_states(aikito_dir, root)
            self.assertEqual(len(states), 1)
            self.assertEqual(states[0].status, "UPDATE")

            # Case 2: Local modification (R != B and R != C) -> DRIFT
            (p1_agents_skills / "SKILL.md").write_text(
                "version 1 (locally modified)", encoding="utf-8"
            )
            states_drift = collect_project_skill_states(aikito_dir, root)
            self.assertEqual(len(states_drift), 1)
            self.assertEqual(states_drift[0].status, "DRIFT")

    def test_uninstalled_agent_instruction_target_skipped_when_parent_missing(
        self,
    ) -> None:
        from aikito.project_runtime import sync_project_path

        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "demo"
            definition = workspace / "projects" / "demo"
            workspace.mkdir()
            project.mkdir()
            definition.mkdir(parents=True)
            write_agents(
                workspace,
                '[agents.codex]\nproject_instruction_path = "AGENTS.md"\n'
                '[agents.claude-code]\nproject_instruction_path = ".claude/CLAUDE.md"\n',
            )
            (definition / "agent.toml").write_text(
                f'path = "{project.as_posix()}"\nskills = []\n', encoding="utf-8"
            )
            (definition / "AGENTS.md").write_text("Rules\n", encoding="utf-8")
            (project / "AGENTS.md").symlink_to(definition / "AGENTS.md")

            with patch("shutil.which", return_value=None):
                # Neither codex nor claude-code is installed in root, and project/.claude does not exist.
                # .claude/CLAUDE.md should not be considered MISSING.
                summaries = collect_project_summaries(workspace, root)
                self.assertEqual(len(summaries), 1)
                self.assertEqual(summaries[0].runtime_status, "OK")
                self.assertFalse((project / ".claude").exists())

                # sync_project_path should skip creating .claude/CLAUDE.md
                sync_project_path(workspace, "demo", project, {"skills": []}, root)
                self.assertFalse((project / ".claude").exists())

                # Now mock claude-code being installed in root.
                (root / ".claude").mkdir()
                summaries = collect_project_summaries(workspace, root)
                self.assertEqual(summaries[0].runtime_status, "MISSING")

                # With claude installed, sync_project_path provisions .claude/CLAUDE.md
                sync_project_path(workspace, "demo", project, {"skills": []}, root)
                self.assertTrue((project / ".claude" / "CLAUDE.md").is_symlink())


class ExactSymlinkOwnershipTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name).resolve()
        self.ws = (self.root / "workspace").resolve()
        self.co = (self.root / "checkout").resolve()
        self.ws.mkdir()
        self.co.mkdir()
        self.canonical_skills = self.ws / "skills"
        self.canonical_skills.mkdir()
        self.canonical_mem = self.ws / "memory"
        self.canonical_mem.mkdir()
        self.runtime_skills = self.co / ".agents" / "skills"
        self.runtime_skills.mkdir(parents=True)
        self.runtime_mem = self.co / ".agents" / "memory"
        self.runtime_mem.mkdir(parents=True)

        # Create two canonical skills
        (self.canonical_skills / "skill-a").mkdir()
        (self.canonical_skills / "skill-a" / "SKILL.md").write_text(
            "# A\n", encoding="utf-8"
        )
        (self.canonical_skills / "skill-b").mkdir()
        (self.canonical_skills / "skill-b" / "SKILL.md").write_text(
            "# B\n", encoding="utf-8"
        )

        # Create two canonical memory files
        (self.canonical_mem / "notes.md").write_text("# Notes\n", encoding="utf-8")
        (self.canonical_mem / "other.md").write_text("# Other\n", encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_deselected_skill_symlink_pointing_to_another_skill_is_preserved_as_conflict(
        self,
    ) -> None:
        # runtime/skill-a points to canonical/skill-b
        target = self.runtime_skills / "skill-a"
        target.symlink_to(self.canonical_skills / "skill-b")

        # Deselected: selected_names does not include "skill-a"
        plan = plan_runtime_cleanup(
            self.runtime_skills,
            selected_names=set(),
            canonical_roots=(self.canonical_skills,),
            allow_matching_copies=True,
        )
        self.assertEqual(plan.cleanup, ())
        self.assertEqual(plan.conflicts, (target,))

    def test_deselected_broken_symlink_pointing_to_another_skill_is_preserved(
        self,
    ) -> None:
        target = self.runtime_skills / "skill-a"
        # Points to a non-existent skill-c in canonical root
        target.symlink_to(self.canonical_skills / "skill-c")

        plan = plan_runtime_cleanup(
            self.runtime_skills,
            selected_names=set(),
            canonical_roots=(self.canonical_skills,),
            allow_matching_copies=True,
        )
        self.assertEqual(plan.cleanup, ())
        self.assertEqual(plan.conflicts, (target,))

    def test_deselected_skill_symlink_pointing_to_own_canonical_is_cleaned_up(
        self,
    ) -> None:
        target = self.runtime_skills / "skill-a"
        target.symlink_to(self.canonical_skills / "skill-a")

        plan = plan_runtime_cleanup(
            self.runtime_skills,
            selected_names=set(),
            canonical_roots=(self.canonical_skills,),
            allow_matching_copies=True,
        )
        self.assertEqual(plan.cleanup, (target,))
        self.assertEqual(plan.conflicts, ())

    def test_selected_skill_symlink_pointing_to_another_skill_is_conflict(self) -> None:
        target = self.runtime_skills / "skill-a"
        target.symlink_to(self.canonical_skills / "skill-b")

        conflicts = find_selected_runtime_conflicts(
            self.runtime_skills,
            selected_names={"skill-a"},
            canonical_root=self.canonical_skills,
            allow_drifted_copies=False,
        )
        self.assertEqual(conflicts, (target,))

    def test_deselected_memory_symlink_pointing_to_another_memory_is_preserved(
        self,
    ) -> None:
        # runtime/notes.md points to canonical/other.md
        target = self.runtime_mem / "notes.md"
        target.symlink_to(self.canonical_mem / "other.md")

        plan = plan_runtime_cleanup(
            self.runtime_mem,
            selected_names=set(),
            canonical_roots=(self.canonical_mem,),
            allow_matching_copies=False,
        )
        self.assertEqual(plan.cleanup, ())
        self.assertEqual(plan.conflicts, (target,))


class ClassifyProjectSkillStateMappingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dummy_target = SkillTarget(
            workspace_root=Path("/ws"),
            workspace_id="ws",
            project_name="proj",
            physical_checkout=Path("/checkout"),
            skill_name="my-skill",
            target_path=Path("/checkout/.agents/skills/my-skill"),
        )

    def _dummy_observed(
        self,
        entry_type: str = "dir",
        canonical_valid: bool = True,
        canonical_error: str | None = None,
    ) -> ObservedSkill:
        return ObservedSkill(
            target=self.dummy_target,
            entry_type=entry_type,
            canonical_valid=canonical_valid,
            canonical_error=canonical_error,
        )

    def test_map_noop_and_reconcile_to_ok(self) -> None:
        obs = self._dummy_observed()
        for rule in ("INV-TR-07", "INV-TR-11", "INV-TR-18"):
            op = SkillOperation(
                action="NOOP",
                rule_id=rule,
                target=self.dummy_target,
                reason="all good",
            )
            status, reason = map_skill_operation_to_project_state(op, obs)
            self.assertEqual(status, "OK")
            self.assertEqual(reason, "")

        reconcile_op = SkillOperation(
            action="RECONCILE_STATE",
            rule_id="INV-TR-10",
            target=self.dummy_target,
            reason="reconcile",
        )
        status, reason = map_skill_operation_to_project_state(reconcile_op, obs)
        self.assertEqual(status, "OK")
        self.assertEqual(reason, "")

    def test_map_create_to_missing_runtime(self) -> None:
        obs = self._dummy_observed(entry_type="missing")
        op = SkillOperation(
            action="CREATE",
            rule_id="INV-TR-01",
            target=self.dummy_target,
            reason="create copy",
        )
        status, reason = map_skill_operation_to_project_state(op, obs)
        self.assertEqual(status, "MISSING")
        self.assertEqual(reason, "Runtime skill is missing")

    def test_map_update_cases(self) -> None:
        obs = self._dummy_observed()
        # Upstream canonical change
        op_upstream = SkillOperation(
            action="UPDATE",
            rule_id="INV-TR-08",
            target=self.dummy_target,
            reason="upstream changed",
        )
        status, reason = map_skill_operation_to_project_state(op_upstream, obs)
        self.assertEqual(status, "UPDATE")
        self.assertEqual(
            reason, "Canonical skill updated upstream; safe to sync without --force"
        )

        # Mode transition
        op_mode = SkillOperation(
            action="UPDATE",
            rule_id="INV-TR-20",
            target=self.dummy_target,
            reason="Switch mode from link to copy",
        )
        status, reason = map_skill_operation_to_project_state(op_mode, obs)
        self.assertEqual(status, "UPDATE")
        self.assertEqual(reason, "Switch mode from link to copy")

    def test_map_drift_cases(self) -> None:
        obs = self._dummy_observed()
        for rule in ("INV-TR-09", "INV-TR-12", "INV-TR-19"):
            op = SkillOperation(
                action="CONFLICT",
                rule_id=rule,
                target=self.dummy_target,
                reason="drift occurred",
            )
            status, reason = map_skill_operation_to_project_state(op, obs)
            self.assertEqual(status, "DRIFT")
            self.assertEqual(
                reason, "Copied project skill drifted from workspace skill"
            )

    def test_map_conflict_cases(self) -> None:
        # Missing canonical
        obs_missing = self._dummy_observed(
            canonical_valid=False,
            canonical_error="Canonical skill source does not exist: /ws/skills/my-skill",
        )
        op_canon_missing = SkillOperation(
            action="CONFLICT",
            rule_id="INV-TR-14",
            target=self.dummy_target,
            reason="missing canon",
        )
        status, reason = map_skill_operation_to_project_state(
            op_canon_missing, obs_missing
        )
        self.assertEqual(status, "MISSING")
        self.assertEqual(reason, "Canonical skill is missing")

        # Invalid canonical (e.g. boundary escape)
        obs_invalid = self._dummy_observed(
            canonical_valid=False,
            canonical_error="Canonical skill escapes boundary",
        )
        op_canon_invalid = SkillOperation(
            action="CONFLICT",
            rule_id="INV-TR-14",
            target=self.dummy_target,
            reason="escapes boundary",
        )
        status, reason = map_skill_operation_to_project_state(
            op_canon_invalid, obs_invalid
        )
        self.assertEqual(status, "CONFLICT")
        self.assertEqual(reason, "escapes boundary")

        # Unsupported runtime entry (file instead of dir)
        obs_unsupported = self._dummy_observed(entry_type="unsupported")
        op_unsupported = SkillOperation(
            action="CONFLICT",
            rule_id="INV-TR-13",
            target=self.dummy_target,
            reason="unsupported file",
        )
        status, reason = map_skill_operation_to_project_state(
            op_unsupported, obs_unsupported
        )
        self.assertEqual(status, "CONFLICT")
        self.assertEqual(reason, "Runtime skill is not a directory")

        # Unauthorized symlink
        obs_sym = self._dummy_observed(entry_type="symlink")
        op_sym = SkillOperation(
            action="CONFLICT",
            rule_id="INV-TR-20",
            target=self.dummy_target,
            reason="Unauthorized link destination",
        )
        status, reason = map_skill_operation_to_project_state(op_sym, obs_sym)
        self.assertEqual(status, "CONFLICT")
        self.assertEqual(reason, "Unauthorized link destination")

    def test_classify_project_skill_state_invalid_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            ws.mkdir()
            (ws / "skills" / "demo").mkdir(parents=True)

            # project_path is None
            st = classify_project_skill_state(ws, "p1", None, "demo", home=root)
            self.assertEqual(st.status, "CONFLICT")
            self.assertEqual(st.reason, "Project path is not configured")

            # invalid skill names
            st = classify_project_skill_state(ws, "p1", root / "proj", "..", home=root)
            self.assertEqual(st.status, "CONFLICT")
            self.assertEqual(st.reason, "Skill name must be a single path component")

            st = classify_project_skill_state(
                ws, "p1", root / "proj", "sub/dir", home=root
            )
            self.assertEqual(st.status, "CONFLICT")
            self.assertEqual(st.reason, "Skill name must be a single path component")

    def test_classify_project_skill_state_strictly_read_only(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            proj = root / "project"
            canon = ws / "skills" / "demo"
            runtime = proj / ".agents" / "skills" / "demo"
            canon.mkdir(parents=True)
            runtime.mkdir(parents=True)
            (canon / "SKILL.md").write_text("v1", encoding="utf-8")
            (runtime / "SKILL.md").write_text("v1", encoding="utf-8")

            with patch("aikito.skill_state.run_recovery_pass") as mock_recovery:
                st = classify_project_skill_state(ws, "p1", proj, "demo", home=root)
                self.assertEqual(st.status, "OK")
                mock_recovery.assert_not_called()

            # Ensure zero state files were written to home
            state_dir = root / ".aikito" / "state"
            self.assertFalse(state_dir.exists())

    def test_classify_project_skill_state_equivalence_with_planner(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            proj = root / "project"
            canon = ws / "skills" / "demo"
            runtime = proj / ".agents" / "skills" / "demo"
            canon.mkdir(parents=True)
            runtime.mkdir(parents=True)
            (canon / "SKILL.md").write_text("v1", encoding="utf-8")
            (runtime / "SKILL.md").write_text("v1", encoding="utf-8")

            # 1. Matching copy
            st = classify_project_skill_state(ws, "p1", proj, "demo", home=root)
            target = SkillTarget(ws, ws.name, "p1", proj, "demo", runtime)
            obs, des = inspect_skill_target(target, "copy", root)
            op = plan_single_skill(target, des, obs, force=False)
            self.assertEqual(st.status, "OK")
            self.assertEqual(op.action, "NOOP")

            # 2. Missing runtime
            runtime_missing = proj / ".agents" / "skills" / "other"
            (ws / "skills" / "other").mkdir(parents=True)
            (ws / "skills" / "other" / "SKILL.md").write_text("v1", encoding="utf-8")
            st_miss = classify_project_skill_state(ws, "p1", proj, "other", home=root)
            t_miss = SkillTarget(ws, ws.name, "p1", proj, "other", runtime_missing)
            obs_m, des_m = inspect_skill_target(t_miss, "copy", root)
            op_m = plan_single_skill(t_miss, des_m, obs_m, force=False)
            self.assertEqual(st_miss.status, "MISSING")
            self.assertEqual(op_m.action, "CREATE")

            # 3. Upstream updated
            doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=ws.as_posix(),
                project_name="p1",
                physical_checkout=proj.as_posix(),
                records={
                    "demo": SkillStateRecord(
                        skill_name="demo",
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint=obs.runtime_fingerprint,
                        baseline_origin="write",
                        last_observed_selected=True,
                    )
                },
            )
            save_project_skill_state(root, doc)
            (canon / "SKILL.md").write_text("v2 upstream", encoding="utf-8")
            st_up = classify_project_skill_state(ws, "p1", proj, "demo", home=root)
            obs_u, des_u = inspect_skill_target(target, "copy", root)
            op_u = plan_single_skill(target, des_u, obs_u, force=False)
            self.assertEqual(st_up.status, "UPDATE")
            self.assertEqual(op_u.action, "UPDATE")
            self.assertEqual(op_u.rule_id, "INV-TR-08")

            # 4. Drifted copy
            (runtime / "SKILL.md").write_text("v_drift local", encoding="utf-8")
            st_dr = classify_project_skill_state(ws, "p1", proj, "demo", home=root)
            obs_d, des_d = inspect_skill_target(target, "copy", root)
            op_d = plan_single_skill(target, des_d, obs_d, force=False)
            self.assertEqual(st_dr.status, "DRIFT")
            self.assertEqual(op_d.action, "CONFLICT")
            self.assertEqual(op_d.rule_id, "INV-TR-09")


if __name__ == "__main__":
    unittest.main()
