import io
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from aikito import cli as AIKITO_CLI
from aikito.add import add_mcp, add_skill, add_subagent
from aikito.init import init_project, init_workspace
from aikito.remove import remove_mcp, remove_skill, remove_subagent


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


class TestAikitoRemoveSubagentValidation(unittest.TestCase):
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
            success = remove_subagent(
                aikito_dir=self.ws,
                home=self.home,
                name="test-agent",
            )
        self.assertFalse(success)
        self.assertIn("workspace directory not found", err.getvalue())

    def test_empty_subagent_name_fails(self) -> None:
        init_workspace(self.ws, self.home)
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_subagent(
                aikito_dir=self.ws,
                home=self.home,
                name="   ",
            )
        self.assertFalse(success)
        self.assertIn("Subagent name cannot be empty", err.getvalue())

    def test_invalid_subagent_names_rejected(self) -> None:
        init_workspace(self.ws, self.home)
        invalid_names = [
            ("../agent", "Path separators and traversals are not allowed"),
            ("/tmp/agent", "Path separators and traversals are not allowed"),
            ("Invalid_Subagent", "Must be kebab-case"),
            ("-leading-dash", "Must be kebab-case"),
        ]
        for bad_name, expected_err in invalid_names:
            err = io.StringIO()
            with redirect_stderr(err):
                success = remove_subagent(
                    aikito_dir=self.ws,
                    home=self.home,
                    name=bad_name,
                )
            self.assertFalse(success)
            self.assertIn(expected_err, err.getvalue())


class TestAikitoRemoveSubagentLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"
        init_workspace(self.ws, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nonexistent_subagent_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_subagent(
                aikito_dir=self.ws,
                home=self.home,
                name="nonexistent-subagent",
            )
        self.assertFalse(success)
        self.assertIn("does not exist in workspace", err.getvalue())

    def test_remove_subagent_success(self) -> None:
        add_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="reviewer",
            description="Code reviewer subagent",
        )
        sub_file = self.ws / "subagents" / "reviewer.md"
        self.assertTrue(sub_file.is_file())

        subagents_toml = self.ws / "subagents.toml"
        data = tomllib.loads(subagents_toml.read_text(encoding="utf-8"))
        self.assertIn("reviewer", data.get("subagents", {}))

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_subagent(
                aikito_dir=self.ws,
                home=self.home,
                name="reviewer",
            )
        self.assertTrue(success)
        self.assertFalse(sub_file.exists())

        new_data = tomllib.loads(subagents_toml.read_text(encoding="utf-8"))
        self.assertNotIn("reviewer", new_data.get("subagents", {}))
        self.assertIn("[DELETE FILE]", out.getvalue())
        self.assertIn("[UPDATE FILE]", out.getvalue())
        self.assertIn("[SUCCESS] Removed subagent 'reviewer'.", out.getvalue())

    def test_remove_subagent_preserves_other_subagents(self) -> None:
        add_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="sub-alpha",
            description="Alpha",
        )
        add_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="sub-beta",
            description="Beta",
        )

        success = remove_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="sub-alpha",
        )
        self.assertTrue(success)

        self.assertFalse((self.ws / "subagents" / "sub-alpha.md").exists())
        self.assertTrue((self.ws / "subagents" / "sub-beta.md").exists())

        subagents_toml = self.ws / "subagents.toml"
        data = tomllib.loads(subagents_toml.read_text(encoding="utf-8"))
        self.assertNotIn("sub-alpha", data.get("subagents", {}))
        self.assertIn("sub-beta", data.get("subagents", {}))
        self.assertEqual(data["subagents"]["sub-beta"]["description"], "Beta")

    def test_remove_subagent_rollback_on_write_failure(self) -> None:
        add_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="rollback-sub",
            description="Rollback test",
        )
        sub_file = self.ws / "subagents" / "rollback-sub.md"
        self.assertTrue(sub_file.is_file())

        subagents_toml = self.ws / "subagents.toml"
        orig_toml = subagents_toml.read_text(encoding="utf-8")

        def failing_write(target, content, encoding="utf-8"):
            if target.resolve() == subagents_toml.resolve():
                raise OSError("Simulated subagents.toml write error")
            target.write_text(content, encoding=encoding)

        err = io.StringIO()
        with patch("aikito.remove._atomic_write_text", side_effect=failing_write):
            with redirect_stderr(err):
                success = remove_subagent(
                    aikito_dir=self.ws,
                    home=self.home,
                    name="rollback-sub",
                )
        self.assertFalse(success)
        self.assertTrue(sub_file.is_file())
        self.assertEqual(subagents_toml.read_text(encoding="utf-8"), orig_toml)

    def test_remove_subagent_with_sync_prunes_agent_runtime(self) -> None:
        from aikito.subagent import sync_subagent_configs

        claude_agents_dir = self.home / ".claude" / "agents"
        claude_agents_dir.mkdir(parents=True)

        add_subagent(
            aikito_dir=self.ws,
            home=self.home,
            name="synced-sub",
            description="Synced subagent",
            agents=["claude-code"],
        )

        sync_subagent_configs(self.ws, self.home)
        target_file = claude_agents_dir / "synced-sub.md"
        self.assertTrue(target_file.is_file())

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_subagent(
                aikito_dir=self.ws,
                home=self.home,
                name="synced-sub",
                sync=True,
            )
        self.assertTrue(success)
        self.assertFalse((self.ws / "subagents" / "synced-sub.md").exists())
        self.assertFalse(target_file.exists())


class TestAikitoRemoveMCPValidation(unittest.TestCase):
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
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="test-mcp",
            )
        self.assertFalse(success)
        self.assertIn("workspace directory not found", err.getvalue())

    def test_empty_mcp_name_fails(self) -> None:
        init_workspace(self.ws, self.home)
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="   ",
            )
        self.assertFalse(success)
        self.assertIn("MCP server name cannot be empty", err.getvalue())

    def test_invalid_mcp_names_rejected(self) -> None:
        init_workspace(self.ws, self.home)
        invalid_names = [
            ("../server", "Path separators and traversals are not allowed"),
            ("/tmp/server", "Path separators and traversals are not allowed"),
            ("Invalid_Server", "Must be kebab-case"),
            ("-leading-dash", "Must be kebab-case"),
        ]
        for bad_name, expected_err in invalid_names:
            err = io.StringIO()
            with redirect_stderr(err):
                success = remove_mcp(
                    aikito_dir=self.ws,
                    home=self.home,
                    name=bad_name,
                )
            self.assertFalse(success)
            self.assertIn(expected_err, err.getvalue())


class TestAikitoRemoveMCPLifecycle(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.ws = self.root / "workspace"
        init_workspace(self.ws, self.home)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_nonexistent_mcp_fails(self) -> None:
        err = io.StringIO()
        with redirect_stderr(err):
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="nonexistent-mcp",
            )
        self.assertFalse(success)
        self.assertIn("does not exist in workspace", err.getvalue())

    def test_remove_mcp_success(self) -> None:
        add_mcp(
            aikito_dir=self.ws,
            home=self.home,
            name="context-server",
            transport="remote",
            url="https://example.com/mcp",
        )
        mcp_file = self.ws / "mcps" / "context-server.toml"
        self.assertTrue(mcp_file.is_file())

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="context-server",
            )
        self.assertTrue(success)
        self.assertFalse(mcp_file.exists())
        self.assertIn("[DELETE FILE]", out.getvalue())
        self.assertIn("[SUCCESS] Removed MCP server 'context-server'.", out.getvalue())

    def test_remove_mcp_with_sync_removes_from_agent_and_state(self) -> None:
        import json
        from aikito.mcp import _load_state, sync_mcp_configs

        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_config = self.home / ".claude.json"
        claude_config.write_text('{"mcpServers": {}}', encoding="utf-8")

        codex_dir = self.home / ".codex"
        codex_dir.mkdir(parents=True)
        codex_config = codex_dir / "config.toml"
        codex_config.write_text('model = "gpt-4"\n', encoding="utf-8")

        add_mcp(
            aikito_dir=self.ws,
            home=self.home,
            name="synced-mcp",
            transport="remote",
            url="https://mcp.example.com",
            agents=["claude-code", "codex"],
        )

        sync_mcp_configs(aikito_dir=self.ws, home=self.home)

        claude_data = json.loads(claude_config.read_text(encoding="utf-8"))
        self.assertIn("synced-mcp", claude_data.get("mcpServers", {}))

        codex_text = codex_config.read_text(encoding="utf-8")
        self.assertIn("[mcp_servers.synced_mcp]", codex_text)

        state = _load_state(self.home)
        self.assertTrue(any(k.endswith(":synced-mcp") for k in state["entries"]))

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="synced-mcp",
                sync=True,
            )
        self.assertTrue(success)
        self.assertFalse((self.ws / "mcps" / "synced-mcp.toml").exists())

        claude_data = json.loads(claude_config.read_text(encoding="utf-8"))
        self.assertNotIn("synced-mcp", claude_data.get("mcpServers", {}))

        codex_text = codex_config.read_text(encoding="utf-8")
        self.assertNotIn("[mcp_servers.synced_mcp]", codex_text)
        self.assertIn('model = "gpt-4"', codex_text)

        state = _load_state(self.home)
        self.assertFalse(any(k.endswith(":synced-mcp") for k in state["entries"]))

    def test_remove_mcp_conflict_blocks_sync_and_preserves_canonical(self) -> None:
        import json

        claude_dir = self.home / ".claude"
        claude_dir.mkdir(parents=True)
        claude_config = self.home / ".claude.json"
        # User manually added an unmanaged MCP entry
        claude_config.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "unmanaged-mcp": {"type": "http", "url": "https://custom.com"}
                    }
                }
            ),
            encoding="utf-8",
        )

        add_mcp(
            aikito_dir=self.ws,
            home=self.home,
            name="unmanaged-mcp",
            transport="remote",
            url="https://mcp.example.com",
            agents=["claude-code"],
        )

        out = io.StringIO()
        with redirect_stdout(out):
            success = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="unmanaged-mcp",
                sync=True,
                force=False,
            )
        self.assertFalse(success)
        self.assertIn("[CONFLICT]", out.getvalue())
        # Canonical file must be preserved
        self.assertTrue((self.ws / "mcps" / "unmanaged-mcp.toml").is_file())
        # Agent config must NOT be touched
        claude_data = json.loads(claude_config.read_text(encoding="utf-8"))
        self.assertIn("unmanaged-mcp", claude_data["mcpServers"])

        # With force=True, removal succeeds
        out2 = io.StringIO()
        with redirect_stdout(out2):
            success2 = remove_mcp(
                aikito_dir=self.ws,
                home=self.home,
                name="unmanaged-mcp",
                sync=True,
                force=True,
            )
        self.assertTrue(success2)
        self.assertFalse((self.ws / "mcps" / "unmanaged-mcp.toml").exists())
        claude_data2 = json.loads(claude_config.read_text(encoding="utf-8"))
        self.assertNotIn("unmanaged-mcp", claude_data2.get("mcpServers", {}))


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

        # Check 'rm subagent'
        args_rm_sub = parser.parse_args(["rm", "subagent", "test-agent", "--sync"])
        self.assertEqual(args_rm_sub.rm_target, "subagent")
        self.assertEqual(args_rm_sub.name, "test-agent")
        self.assertTrue(args_rm_sub.sync)

        # Check 'remove subagents' alias
        args_remove_sub = parser.parse_args(["remove", "subagents", "test-agent"])
        self.assertEqual(args_remove_sub.remove_target, "subagents")
        self.assertEqual(args_remove_sub.name, "test-agent")
        self.assertFalse(args_remove_sub.sync)

        # Check 'rm mcp'
        args_rm_mcp = parser.parse_args(["rm", "mcp", "test-mcp", "--sync", "--force"])
        self.assertEqual(args_rm_mcp.rm_target, "mcp")
        self.assertEqual(args_rm_mcp.name, "test-mcp")
        self.assertTrue(args_rm_mcp.sync)
        self.assertTrue(args_rm_mcp.force)

        # Check 'remove mcps' alias
        args_remove_mcp = parser.parse_args(["remove", "mcps", "test-mcp"])
        self.assertEqual(args_remove_mcp.remove_target, "mcps")
        self.assertEqual(args_remove_mcp.name, "test-mcp")
        self.assertFalse(args_remove_mcp.sync)
        self.assertFalse(args_remove_mcp.force)


if __name__ == "__main__":
    unittest.main()
