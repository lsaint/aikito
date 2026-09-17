import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aikito import cli as AIKITO_CLI
from aikito.add import add_skill
from aikito.init import init_project, init_workspace
from aikito.remove import remove_skill


class TestAikitoRemoveValidation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_uninitialized_workspace_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="some-skill",
            )
        self.assertFalse(success)
        self.assertIn("workspace directory not found", err.getvalue())

    def test_empty_skill_name_fails(self) -> None:
        init_workspace(self.ws, self.home)
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="   ",
            )
        self.assertFalse(success)
        self.assertIn("Skill name cannot be empty", err.getvalue())

    def test_cannot_remove_bundled_system_skills(self) -> None:
        init_workspace(self.ws, self.home)
        for bundled_name in ("aikito", "durable-memory"):
            err = io.StringIO()
            with redirect_stderr(err):
                success = remove_skill(
                    aikito_dir=self.ws,
                    home=self.home,
                    name=bundled_name,
                )
            self.assertFalse(success)
            self.assertIn(
                f"Cannot remove bundled system skill '{bundled_name}'",
                err.getvalue(),
            )

    def test_invalid_skill_names_rejected(self) -> None:
        init_workspace(self.ws, self.home)
        invalid_names = [
            ("../memory", "Path separators and traversals are not allowed"),
            ("/tmp/x/victim", "Path separators and traversals are not allowed"),
            ("sub/victim", "Path separators and traversals are not allowed"),
            ("Invalid_Name", "Must be kebab-case"),
            ("-leading-dash", "Must be kebab-case"),
        ]
        for bad_name, expected_err in invalid_names:
            err = io.StringIO()
            with redirect_stderr(err):
                success = remove_skill(
                    aikito_dir=self.ws,
                    home=self.home,
                    name=bad_name,
                )
            self.assertFalse(success)
            self.assertIn(expected_err, err.getvalue())


class TestAikitoRemoveFromProjects(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"
        init_workspace(self.ws, self.home)

        self.proj1_dir = self.home / "proj1"
        self.proj1_dir.mkdir()
        init_project(self.ws, self.proj1_dir, "proj1", home=self.home)

        self.proj2_dir = self.home / "proj2"
        self.proj2_dir.mkdir()
        init_project(self.ws, self.proj2_dir, "proj2", home=self.home)

        # Add a skill to both projects
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="shared-skill",
            projects=["proj1", "proj2"],
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_project_not_found(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="shared-skill",
                projects=["non-existent-proj"],
            )
        self.assertFalse(success)
        self.assertIn("Project 'non-existent-proj' not found", err.getvalue())

    def test_skill_not_registered_in_single_project(self) -> None:
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="only-in-proj1",
            projects=["proj1"],
        )
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="only-in-proj1",
                projects=["proj2"],
            )
        self.assertFalse(success)
        self.assertIn(
            "Skill 'only-in-proj1' is not registered in project 'proj2'",
            err.getvalue(),
        )

    def test_skill_not_registered_in_any_target_project(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="unknown-skill",
                projects=["proj1", "proj2"],
            )
        self.assertFalse(success)
        self.assertIn(
            "Skill 'unknown-skill' is not registered in any of the specified projects",
            err.getvalue(),
        )

    def test_can_unregister_bundled_skill_from_project(self) -> None:
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="durable-memory",
            projects=["proj1"],
        )
        p1_toml = self.ws / "projects" / "proj1" / "agent.toml"
        self.assertIn('"durable-memory"', p1_toml.read_text(encoding="utf-8"))

        success = remove_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="durable-memory",
            projects=["proj1"],
        )
        self.assertTrue(success)
        self.assertNotIn('"durable-memory"', p1_toml.read_text(encoding="utf-8"))
        self.assertTrue((self.ws / "skills" / "durable-memory").is_dir())

    def test_remove_skill_from_single_project_preserves_canonical(self) -> None:
        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="shared-skill",
                projects=["proj1"],
            )
        self.assertTrue(success)

        # Check proj1 agent.toml
        toml1 = tomllib.loads(
            (self.ws / "projects" / "proj1" / "agent.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn("shared-skill", toml1.get("skills", []))

        # Check proj2 agent.toml still has it
        toml2 = tomllib.loads(
            (self.ws / "projects" / "proj2" / "agent.toml").read_text(encoding="utf-8")
        )
        self.assertIn("shared-skill", toml2.get("skills", []))

        # Canonical skill directory in workspace must remain intact
        canonical_dir = self.ws / "skills" / "shared-skill"
        self.assertTrue(canonical_dir.is_dir())
        self.assertTrue((canonical_dir / "SKILL.md").is_file())

    def test_remove_skill_from_multiple_projects_partial_registration(
        self,
    ) -> None:
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="p1-only",
            projects=["proj1"],
        )
        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="p1-only",
                projects=["proj1", "proj2"],
            )
        self.assertTrue(success)
        self.assertIn(
            "Skill 'p1-only' was not registered in project 'proj2'",
            out.getvalue(),
        )

        toml1 = tomllib.loads(
            (self.ws / "projects" / "proj1" / "agent.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn("p1-only", toml1.get("skills", []))

    def test_remove_skill_from_project_with_sync(self) -> None:
        # Create external skill and add with sync
        ext_dir = self.root / "ext-skill"
        ext_dir.mkdir()
        (ext_dir / "SKILL.md").write_text(
            "---\nname: synced-skill\ndescription: Synced\n---\n\n# Synced\n",
            encoding="utf-8",
        )
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="synced-skill",
            from_source=ext_dir,
            projects=["proj1"],
            sync=True,
        )
        link = self.proj1_dir / ".agents" / "skills" / "synced-skill"
        self.assertTrue(link.exists() or link.is_symlink())

        # Unregister with sync
        success = remove_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="synced-skill",
            projects=["proj1"],
            sync=True,
        )
        self.assertTrue(success)
        self.assertFalse(link.exists())
        self.assertFalse(link.is_symlink())

    def test_project_atomic_rollback_on_write_error(self) -> None:
        agent_toml_1 = self.ws / "projects" / "proj1" / "agent.toml"
        agent_toml_2 = self.ws / "projects" / "proj2" / "agent.toml"
        original_1 = agent_toml_1.read_text(encoding="utf-8")
        original_2 = agent_toml_2.read_text(encoding="utf-8")

        call_count = 0

        def failing_atomic_write(target, content, encoding="utf-8"):
            nonlocal call_count
            call_count += 1
            if call_count == 2:
                raise OSError("Disk write simulated failure")
            target.write_text(content, encoding=encoding)

        err = io.StringIO()
        with patch(
            "aikito.remove._atomic_write_text", side_effect=failing_atomic_write
        ):
            with redirect_stderr(err):
                success = remove_skill(
                    aikito_dir=self.ws,
                    home=self.home,
                    name="shared-skill",
                    projects=["proj1", "proj2"],
                )
        self.assertFalse(success)
        self.assertIn("Failed to update project config", err.getvalue())

        # Verify rollback: both files restored to original content
        self.assertEqual(agent_toml_1.read_text(encoding="utf-8"), original_1)
        self.assertEqual(agent_toml_2.read_text(encoding="utf-8"), original_2)


class TestAikitoRemoveGlobally(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"
        init_workspace(self.ws, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_remove_nonexistent_skill_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="ghost-skill",
            )
        self.assertFalse(success)
        self.assertIn("does not exist in workspace", err.getvalue())

    def test_remove_unreferenced_global_skill(self) -> None:
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="global-test-skill",
            description="A test global skill",
        )
        skill_dir = self.ws / "skills" / "global-test-skill"
        self.assertTrue(skill_dir.is_dir())

        skills_toml = self.ws / "skills.toml"
        data = tomllib.loads(skills_toml.read_text(encoding="utf-8"))
        self.assertIn("global-test-skill", data.get("skills", []))

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="global-test-skill",
            )
        self.assertTrue(success)
        self.assertFalse(skill_dir.exists())

        data_after = tomllib.loads(skills_toml.read_text(encoding="utf-8"))
        self.assertNotIn("global-test-skill", data_after.get("skills", []))
        self.assertIn("Removed skill 'global-test-skill'", out.getvalue())

    def test_remove_skill_blocked_by_project_references_without_force(
        self,
    ) -> None:
        proj_dir = self.home / "my-project"
        proj_dir.mkdir()
        init_project(self.ws, proj_dir, "my-project", home=self.home)

        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="proj-bound-skill",
            projects=["my-project"],
        )

        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="proj-bound-skill",
                force=False,
            )
        self.assertFalse(success)
        self.assertIn(
            "is still registered in project(s): 'my-project'",
            err.getvalue(),
        )
        self.assertIn(
            "or use --force to unregister from all projects and delete", err.getvalue()
        )

        # Skill and registration remain intact
        self.assertTrue((self.ws / "skills" / "proj-bound-skill").is_dir())

    def test_remove_skill_with_force_cascades_unregistration(self) -> None:
        proj_dir1 = self.home / "proj1"
        proj_dir1.mkdir()
        init_project(self.ws, proj_dir1, "proj1", home=self.home)

        proj_dir2 = self.home / "proj2"
        proj_dir2.mkdir()
        init_project(self.ws, proj_dir2, "proj2", home=self.home)

        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="cascaded-skill",
            projects=["proj1", "proj2"],
        )

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="cascaded-skill",
                force=True,
            )
        self.assertTrue(success)

        # Canonical directory removed
        self.assertFalse((self.ws / "skills" / "cascaded-skill").exists())

        # Both projects unregistered
        p1_toml = tomllib.loads(
            (self.ws / "projects" / "proj1" / "agent.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn("cascaded-skill", p1_toml.get("skills", []))
        p2_toml = tomllib.loads(
            (self.ws / "projects" / "proj2" / "agent.toml").read_text(encoding="utf-8")
        )
        self.assertNotIn("cascaded-skill", p2_toml.get("skills", []))

    def test_global_removal_rollback_on_write_failure(self) -> None:
        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="rollback-skill",
            description="Rollback test",
        )
        skill_dir = self.ws / "skills" / "rollback-skill"
        self.assertTrue(skill_dir.is_dir())

        skills_toml = self.ws / "skills.toml"
        original_skills_toml = skills_toml.read_text(encoding="utf-8")

        def failing_write(target, content, encoding="utf-8"):
            if target.resolve() == skills_toml.resolve():
                raise OSError("Simulated skills.toml write error")
            target.write_text(content, encoding=encoding)

        err = io.StringIO()
        with patch("aikito.remove._atomic_write_text", side_effect=failing_write):
            with redirect_stderr(err):
                success = remove_skill(
                    aikito_dir=self.ws,
                    home=self.home,
                    name="rollback-skill",
                )
        self.assertFalse(success)

        # Canonical skill directory restored
        self.assertTrue(skill_dir.is_dir())
        self.assertTrue((skill_dir / "SKILL.md").is_file())
        self.assertEqual(skills_toml.read_text(encoding="utf-8"), original_skills_toml)

    def test_global_removal_sync_project_failure_returns_false(self) -> None:
        proj_dir = self.home / "proj1"
        proj_dir.mkdir()
        init_project(self.ws, proj_dir, "proj1", home=self.home)

        add_skill(
            aikito_dir=self.ws,
            home=self.home,
            name="proj-sync-fail-skill",
            projects=["proj1"],
        )

        with patch("aikito.cli.sync_project_by_name", return_value=False):
            success = remove_skill(
                aikito_dir=self.ws,
                home=self.home,
                name="proj-sync-fail-skill",
                force=True,
                sync=True,
            )
        self.assertFalse(success)


class TestAikitoRemoveCLI(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"
        init_workspace(self.ws, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cli_rm_and_remove_parser_wiring(self) -> None:
        parser = AIKITO_CLI.build_parser()

        # Check 'rm skill'
        args_rm = parser.parse_args(["rm", "skill", "test-skill"])
        self.assertEqual(args_rm.rm_target, "skill")
        self.assertEqual(args_rm.name, "test-skill")
        self.assertFalse(args_rm.force)
        self.assertFalse(args_rm.sync)
        self.assertIsNone(args_rm.project)

        # Check 'rm skills' alias
        args_rm_alias = parser.parse_args(["rm", "skills", "test-skill"])
        self.assertEqual(args_rm_alias.rm_target, "skills")
        self.assertEqual(args_rm_alias.name, "test-skill")

        # Check 'remove skill'
        args_remove = parser.parse_args(
            ["remove", "skill", "test-skill", "--project", "p1,p2", "--force", "--sync"]
        )
        self.assertEqual(args_remove.remove_target, "skill")
        self.assertEqual(args_remove.name, "test-skill")
        self.assertEqual(args_remove.project, "p1,p2")
        self.assertTrue(args_remove.force)
        self.assertTrue(args_remove.sync)


if __name__ == "__main__":
    unittest.main()
