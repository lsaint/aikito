import io
import json
import os
import shutil
import stat
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import aikito.add
from aikito.add import (
    _atomic_write_text,
    _parse_markdown_frontmatter,
    add_mcp,
    add_skill,
    add_subagent,
    validate_resource_name,
)
from aikito.compat import is_windows
from aikito.init import init_project, init_workspace
from aikito.skill_state import SkillWriterLock
from aikito.templating import load_agents_template

ROOT = Path(__file__).resolve().parents[1]


class TestAikitoAddValidation(unittest.TestCase):
    def test_validate_resource_name_valid(self) -> None:
        valid_names = [
            "formatter",
            "my-skill",
            "mcp-server-1",
            "subagent2",
            "a",
            "tool-v2-test",
        ]
        for name in valid_names:
            err = validate_resource_name(name, "skill")
            self.assertIsNone(err, f"Expected '{name}' to be valid, got error: {err}")

    def test_validate_resource_name_invalid(self) -> None:
        invalid_cases = [
            ("", "empty"),
            ("   ", "whitespace"),
            ("Formatter", "uppercase"),
            ("-leading-hyphen", "leading hyphen"),
            ("trailing-hyphen-", "trailing hyphen"),
            ("path/separator", "slash"),
            ("back\\slash", "backslash"),
            ("../traversal", "dotdot"),
            ("has space", "space"),
            ("has:colon", "colon"),
            ("has_underscore", "underscore"),
        ]
        for name, reason in invalid_cases:
            err = validate_resource_name(name, "skill")
            self.assertIsNotNone(
                err, f"Expected '{name}' ({reason}) to fail validation."
            )


class TestAikitoAddSkill(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.aikito_dir = self.home / "aikito"
        init_workspace(self.aikito_dir, self.home)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_add_skill_global(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="code-formatter",
                description="Formats code according to standards.",
            )
        self.assertTrue(success)

        skill_file = self.aikito_dir / "skills" / "code-formatter" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        content = skill_file.read_text(encoding="utf-8")
        self.assertIn("name: code-formatter", content)
        self.assertIn("description: Formats code according to standards.", content)
        self.assertIn("# Code Formatter", content)

        out = stdout_buf.getvalue()
        self.assertIn(
            "1. Update instructions in ~/aikito/skills/code-formatter/SKILL.md (or run 'aikito edit skill code-formatter')",
            out,
        )
        self.assertIn("2. Synchronize to agents: aikito sync global", out)

        # Check skills.toml
        skills_toml = (self.aikito_dir / "skills.toml").resolve()
        with skills_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("code-formatter", data.get("skills", []))

    def test_add_skill_with_sync_holds_outer_writer_lock(self) -> None:
        source = self.home / "external-skill"
        source.mkdir()
        (source / "SKILL.md").write_text(
            "---\nname: external-skill\ndescription: Test\n---\n",
            encoding="utf-8",
        )
        observed_depths: list[int] = []

        def sync_while_locked(*args, **kwargs) -> bool:
            observed_depths.append(SkillWriterLock._lock_depth)
            return True

        with patch("aikito.cli.sync_global_resources", side_effect=sync_while_locked):
            success = add_skill(
                self.aikito_dir,
                self.home,
                from_source=source,
                sync=True,
            )

        self.assertTrue(success)
        self.assertEqual(observed_depths, [1])
        self.assertEqual(SkillWriterLock._lock_depth, 0)

    def test_add_skill_duplicate_rejected(self) -> None:
        add_skill(self.aikito_dir, self.home, name="test-skill")

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(self.aikito_dir, self.home, name="test-skill")
        self.assertFalse(success)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_add_skill_project(self) -> None:
        proj_path = self.home / "my-project"
        proj_path.mkdir(parents=True)
        init_project(self.aikito_dir, proj_path, "my-project")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="project-linter",
                description="Lint project files.",
                project_name="my-project",
            )
        self.assertTrue(success)

        skill_file = self.aikito_dir / "skills" / "project-linter" / "SKILL.md"
        self.assertTrue(skill_file.is_file())

        # Check project agent.toml
        agent_toml = self.aikito_dir / "projects" / "my-project" / "agent.toml"
        with agent_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("project-linter", data.get("skills", []))

        # Check global skills.toml does NOT contain project skill
        skills_toml = self.aikito_dir / "skills.toml"
        with skills_toml.open("rb") as f:
            global_data = tomllib.load(f)
        self.assertNotIn("project-linter", global_data.get("skills", []))

    def test_add_skill_project_with_multiline_agent_toml(self) -> None:
        proj_dir = self.aikito_dir / "projects" / "blog"
        proj_dir.mkdir(parents=True, exist_ok=True)
        agent_toml = proj_dir / "agent.toml"
        agent_toml.write_text(
            'name = "blog"\n'
            'path = "~/source/blog"\n'
            'sync_mode = "link"\n\n'
            "skills = [\n"
            '    "nginx-ssl-update",\n'
            '    "wechat-to-blog-deploy"\n'
            "]\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="new-blog-skill",
                project_name="blog",
            )
        self.assertTrue(success)

        # Ensure agent.toml parses without error and contains all 3 skills
        with agent_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["name"], "blog")
        self.assertEqual(data["path"], "~/source/blog")
        self.assertEqual(data["sync_mode"], "link")
        self.assertEqual(
            data["skills"],
            ["nginx-ssl-update", "wechat-to-blog-deploy", "new-blog-skill"],
        )

    def test_add_skill_project_with_nested_tables_and_comments(self) -> None:
        proj_dir = self.aikito_dir / "projects" / "advanced-proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        agent_toml = proj_dir / "agent.toml"
        agent_toml.write_text(
            "# Main project configuration\n"
            'name = "advanced-proj"\n'
            'path = "~/source/advanced"\n'
            'sync_mode = "copy"\n\n'
            "skills = [\n"
            '    "existing-skill"\n'
            "]\n\n"
            "[overrides.codex]\n"
            'model = "gpt-4o"\n'
            "temperature = 0.2\n\n"
            "[tools.linter]\n"
            "enabled = true\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="another-skill",
                project_name="advanced-proj",
            )
        self.assertTrue(success)

        # Verify raw text preserves comments and table structure
        content = agent_toml.read_text(encoding="utf-8")
        self.assertIn("# Main project configuration", content)
        self.assertIn("[overrides.codex]", content)
        self.assertIn("[tools.linter]", content)

        # Verify parsed TOML dictionary structure is completely preserved
        with agent_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["name"], "advanced-proj")
        self.assertEqual(data["skills"], ["existing-skill", "another-skill"])
        self.assertEqual(data["overrides"]["codex"]["model"], "gpt-4o")
        self.assertEqual(data["overrides"]["codex"]["temperature"], 0.2)
        self.assertEqual(data["tools"]["linter"]["enabled"], True)

    def test_add_skill_project_inserts_before_tables_when_skills_missing(self) -> None:
        proj_dir = self.aikito_dir / "projects" / "table-only"
        proj_dir.mkdir(parents=True, exist_ok=True)
        agent_toml = proj_dir / "agent.toml"
        agent_toml.write_text(
            'name = "table-only"\n'
            'path = "~/source/table-only"\n\n'
            "[overrides.claude-code]\n"
            'model = "claude-3-5-sonnet"\n',
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="first-skill",
                project_name="table-only",
            )
        self.assertTrue(success)

        with agent_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["name"], "table-only")
        self.assertEqual(data["skills"], ["first-skill"])
        self.assertEqual(data["overrides"]["claude-code"]["model"], "claude-3-5-sonnet")

    def test_add_skill_project_duplicate_rejected(self) -> None:
        proj_path = self.home / "my-project"
        proj_path.mkdir(parents=True)
        init_project(self.aikito_dir, proj_path, "my-project")
        add_skill(
            self.aikito_dir, self.home, name="dup-skill", project_name="my-project"
        )

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="dup-skill",
                project_name="my-project",
            )
        self.assertFalse(success)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_add_skill_nonexistent_project_fails(self) -> None:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="some-skill",
                project_name="no-such-project",
            )
        self.assertFalse(success)
        self.assertIn("not found", stderr_buf.getvalue())

    def test_add_skill_multi_project(self) -> None:
        proj_a = self.home / "project-a"
        proj_b = self.home / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        init_project(self.aikito_dir, proj_a, "project-a")
        init_project(self.aikito_dir, proj_b, "project-b")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="multi-skill",
                projects=["project-a", "project-b"],
            )
        self.assertTrue(success)

        # Both projects should have the skill registered
        for proj in ("project-a", "project-b"):
            toml_path = self.aikito_dir / "projects" / proj / "agent.toml"
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            self.assertIn("multi-skill", data.get("skills", []))

        skill_file = self.aikito_dir / "skills" / "multi-skill" / "SKILL.md"
        self.assertTrue(skill_file.is_file())

    def test_add_skill_multi_project_comma_string(self) -> None:
        proj_a = self.home / "project-a"
        proj_b = self.home / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        init_project(self.aikito_dir, proj_a, "project-a")
        init_project(self.aikito_dir, proj_b, "project-b")

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="comma-skill",
            project_name="project-a, project-b",
        )
        self.assertTrue(success)

        for proj in ("project-a", "project-b"):
            toml_path = self.aikito_dir / "projects" / proj / "agent.toml"
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            self.assertIn("comma-skill", data.get("skills", []))

    def test_add_skill_existing_canonical_registered_to_new_project(self) -> None:
        proj_a = self.home / "project-a"
        proj_b = self.home / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        init_project(self.aikito_dir, proj_a, "project-a")
        init_project(self.aikito_dir, proj_b, "project-b")

        # Step 1: Add skill to project A
        success = add_skill(
            self.aikito_dir,
            self.home,
            name="reuse-skill",
            project_name="project-a",
        )
        self.assertTrue(success)

        # Step 2: Register existing skill to project B
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success_b = add_skill(
                self.aikito_dir,
                self.home,
                name="reuse-skill",
                project_name="project-b",
            )
        self.assertTrue(success_b)
        self.assertIn(
            "Using existing canonical skill 'reuse-skill'", stdout_buf.getvalue()
        )

        toml_b = self.aikito_dir / "projects" / "project-b" / "agent.toml"
        with toml_b.open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("reuse-skill", data.get("skills", []))

    def test_add_skill_force_updates_already_registered_project_skill(self) -> None:
        project_dir = self.home / "project-a"
        project_dir.mkdir()
        init_project(self.aikito_dir, project_dir, "project-a")
        source_dir = self.home / "project-skill-source"
        source_dir.mkdir()
        source_file = source_dir / "SKILL.md"
        source_file.write_text(
            "---\nname: project-skill\ndescription: Initial.\n---\n\n# Initial\n",
            encoding="utf-8",
        )
        self.assertTrue(
            add_skill(
                self.aikito_dir,
                self.home,
                from_source=source_dir,
                project_name="project-a",
            )
        )
        source_file.write_text(
            "---\nname: project-skill\ndescription: Updated.\n---\n\n# Updated\n",
            encoding="utf-8",
        )

        success = add_skill(
            self.aikito_dir,
            self.home,
            from_source=source_dir,
            project_name="project-a",
            force=True,
        )

        self.assertTrue(success)
        canonical_file = self.aikito_dir / "skills" / "project-skill" / "SKILL.md"
        self.assertIn("# Updated", canonical_file.read_text(encoding="utf-8"))

    def test_add_skill_from_external_directory(self) -> None:
        proj_a = self.home / "project-a"
        proj_b = self.home / "project-b"
        proj_a.mkdir(parents=True)
        proj_b.mkdir(parents=True)
        init_project(self.aikito_dir, proj_a, "project-a")
        init_project(self.aikito_dir, proj_b, "project-b")

        # Create external skill directory
        ext_dir = self.home / "ext-skill"
        ext_dir.mkdir()
        (ext_dir / "references").mkdir()
        (ext_dir / "references" / "ref.md").write_text(
            "reference guide", encoding="utf-8"
        )
        (ext_dir / "SKILL.md").write_text(
            "---\nname: imported-skill\ndescription: An imported external skill.\n---\n\n# Imported Skill\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                projects=["project-a", "project-b"],
                from_source=ext_dir,
            )
        self.assertTrue(success)

        # Verify canonical skill created
        skill_dir = self.aikito_dir / "skills" / "imported-skill"
        self.assertTrue(skill_dir.is_dir())
        self.assertTrue((skill_dir / "SKILL.md").is_file())
        self.assertTrue((skill_dir / "references" / "ref.md").is_file())
        self.assertEqual(
            (skill_dir / "references" / "ref.md").read_text(encoding="utf-8"),
            "reference guide",
        )

        # Verify projects registered
        for proj in ("project-a", "project-b"):
            toml_path = self.aikito_dir / "projects" / proj / "agent.toml"
            with toml_path.open("rb") as f:
                data = tomllib.load(f)
            self.assertIn("imported-skill", data.get("skills", []))

    def test_add_skill_from_external_markdown_file(self) -> None:
        ext_file = self.home / "single-file-skill.md"
        ext_file.write_text(
            "---\nname: single-skill\ndescription: A single file skill.\n---\n\n# Single Skill\n",
            encoding="utf-8",
        )

        success = add_skill(
            self.aikito_dir,
            self.home,
            from_source=ext_file,
        )
        self.assertTrue(success)

        skill_file = self.aikito_dir / "skills" / "single-skill" / "SKILL.md"
        self.assertTrue(skill_file.is_file())
        self.assertIn("name: single-skill", skill_file.read_text(encoding="utf-8"))

        skills_toml = self.aikito_dir / "skills.toml"
        with skills_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("single-skill", data.get("skills", []))

    def test_add_skill_from_missing_source_fails(self) -> None:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="some-skill",
                from_source=self.home / "nonexistent-skill-dir",
            )
        self.assertFalse(success)
        self.assertIn("Source path does not exist", stderr_buf.getvalue())

    def test_add_skill_from_directory_without_skill_md_fails(self) -> None:
        empty_dir = self.home / "no-skill-md"
        empty_dir.mkdir()
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="some-skill",
                from_source=empty_dir,
            )
        self.assertFalse(success)
        self.assertIn("does not contain a SKILL.md file", stderr_buf.getvalue())

    def test_add_skill_from_non_md_file_fails(self) -> None:
        txt_file = self.home / "skill.txt"
        txt_file.write_text("not a markdown file", encoding="utf-8")
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="some-skill",
                from_source=txt_file,
            )
        self.assertFalse(success)
        self.assertIn("must be a markdown (.md) file", stderr_buf.getvalue())

    def test_add_skill_from_source_duplicate_rejected(self) -> None:
        ext_dir = self.home / "dup-ext"
        ext_dir.mkdir()
        (ext_dir / "SKILL.md").write_text(
            "---\nname: dup-canonical\ndescription: Duplicate canonical.\n---\n",
            encoding="utf-8",
        )
        add_skill(self.aikito_dir, self.home, from_source=ext_dir)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(self.aikito_dir, self.home, from_source=ext_dir)
        self.assertFalse(success)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_add_skill_force_requires_source(self) -> None:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="forced-skill",
                force=True,
            )

        self.assertFalse(success)
        self.assertIn("--force requires --from", stderr_buf.getvalue())

    def test_add_skill_force_replaces_complete_imported_snapshot(self) -> None:
        ext_dir = self.home / "updated-ext"
        ext_dir.mkdir()
        source_file = ext_dir / "SKILL.md"
        source_file.write_text(
            "---\nname: updated-skill\ndescription: Initial.\n---\n\n# Initial\n",
            encoding="utf-8",
        )
        (ext_dir / "removed.txt").write_text("old", encoding="utf-8")
        self.assertTrue(add_skill(self.aikito_dir, self.home, from_source=ext_dir))

        source_file.write_text(
            "---\nname: updated-skill\ndescription: Updated.\n---\n\n# Updated\n",
            encoding="utf-8",
        )
        (ext_dir / "removed.txt").unlink()
        (ext_dir / "added.txt").write_text("new", encoding="utf-8")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                from_source=ext_dir,
                force=True,
            )

        self.assertTrue(success)
        canonical_dir = self.aikito_dir / "skills" / "updated-skill"
        self.assertIn("# Updated", (canonical_dir / "SKILL.md").read_text())
        self.assertFalse((canonical_dir / "removed.txt").exists())
        self.assertEqual((canonical_dir / "added.txt").read_text(), "new")
        self.assertIn(
            "[SUCCESS] Updated global skill 'updated-skill'.", stdout_buf.getvalue()
        )

        with (self.aikito_dir / "skills.toml").open("rb") as handle:
            registered = tomllib.load(handle).get("skills", [])
        self.assertEqual(registered.count("updated-skill"), 1)

    def test_add_skill_cannot_overwrite_bundled_skills(self) -> None:
        ext_dir = self.home / "bundled-overwrite-ext"
        ext_dir.mkdir()
        (ext_dir / "SKILL.md").write_text(
            "---\nname: aikito\ndescription: Fake aikito.\n---\n\n# Fake Aikito\n",
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="aikito",
                from_source=ext_dir,
                force=True,
            )
        self.assertFalse(success)
        self.assertIn(
            "Cannot overwrite bundled system skill 'aikito'",
            stderr_buf.getvalue(),
        )

        # Check durable-memory too
        stderr_buf2 = io.StringIO()
        with redirect_stderr(stderr_buf2):
            success2 = add_skill(
                self.aikito_dir,
                self.home,
                name="durable-memory",
                from_source=ext_dir,
                force=True,
            )
        self.assertFalse(success2)
        self.assertIn(
            "Cannot overwrite bundled system skill 'durable-memory'",
            stderr_buf2.getvalue(),
        )

    def test_add_skill_cannot_create_reserved_bundled_name(self) -> None:
        aikito_skill_dir = self.aikito_dir / "skills" / "aikito"
        if aikito_skill_dir.exists():
            shutil.rmtree(aikito_skill_dir)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="aikito",
                description="Custom aikito",
            )
        self.assertFalse(success)
        self.assertIn(
            "Cannot create custom skill with reserved bundled system skill name 'aikito'",
            stderr_buf.getvalue(),
        )

    def test_add_bundled_skill_to_project_shows_info_notice(self) -> None:
        proj_dir = self.home / "bundled-notice-proj"
        proj_dir.mkdir()
        init_project(self.aikito_dir, proj_dir, "bundled-notice-proj")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="durable-memory",
                project_name="bundled-notice-proj",
            )
        self.assertTrue(success)
        self.assertIn(
            "[INFO] 'durable-memory' is a built-in skill and already active globally.",
            stdout_buf.getvalue(),
        )
        agent_toml = self.aikito_dir / "projects" / "bundled-notice-proj" / "agent.toml"
        self.assertIn('"durable-memory"', agent_toml.read_text(encoding="utf-8"))

    def test_add_skill_rebuilds_dangling_global_registration(self) -> None:
        skills_toml = self.aikito_dir / "skills.toml"
        skills_toml.write_text(
            'skills = [\n    "dangling-skill"\n]\n', encoding="utf-8"
        )
        canonical_dir = self.aikito_dir / "skills" / "dangling-skill"
        self.assertFalse(canonical_dir.exists())

        ext_dir = self.home / "dangling-source"
        ext_dir.mkdir()
        (ext_dir / "SKILL.md").write_text(
            "---\nname: dangling-skill\ndescription: Rebuilt.\n---\n\n# Rebuilt\n",
            encoding="utf-8",
        )
        success = add_skill(
            self.aikito_dir,
            self.home,
            name="dangling-skill",
            from_source=ext_dir,
            force=True,
        )
        self.assertTrue(success)
        self.assertTrue(canonical_dir.is_dir())
        self.assertIn(
            "# Rebuilt", (canonical_dir / "SKILL.md").read_text(encoding="utf-8")
        )

    def test_add_skill_rebuilds_dangling_project_registration(self) -> None:
        proj_dir = self.home / "dangling-proj"
        proj_dir.mkdir()
        init_project(self.aikito_dir, proj_dir, "dangling-proj")

        agent_toml = self.aikito_dir / "projects" / "dangling-proj" / "agent.toml"
        agent_toml.write_text(
            'name = "dangling-proj"\nskills = ["dangling-proj-skill"]\n',
            encoding="utf-8",
        )
        canonical_dir = self.aikito_dir / "skills" / "dangling-proj-skill"
        self.assertFalse(canonical_dir.exists())

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="dangling-proj-skill",
            project_name="dangling-proj",
        )
        self.assertTrue(success)
        self.assertTrue(canonical_dir.is_dir())
        self.assertTrue((canonical_dir / "SKILL.md").is_file())

    def test_add_skill_force_restores_previous_snapshot_on_failure(self) -> None:
        ext_dir = self.home / "rollback-ext"
        ext_dir.mkdir()
        source_file = ext_dir / "SKILL.md"
        source_file.write_text(
            "---\nname: rollback-skill\ndescription: Initial.\n---\n\n# Initial\n",
            encoding="utf-8",
        )
        self.assertTrue(add_skill(self.aikito_dir, self.home, from_source=ext_dir))
        canonical_file = self.aikito_dir / "skills" / "rollback-skill" / "SKILL.md"
        original_content = canonical_file.read_text(encoding="utf-8")

        source_file.write_text(
            "---\nname: rollback-skill\ndescription: Updated.\n---\n\n# Updated\n",
            encoding="utf-8",
        )
        real_atomic_write = aikito.add._atomic_write_text

        def fail_registry_write(path_obj: Path, content: str, *args, **kwargs):
            if path_obj.name == "skills.toml":
                raise OSError("Simulated registry write failure")
            return real_atomic_write(path_obj, content, *args, **kwargs)

        stderr_buf = io.StringIO()
        with (
            patch("aikito.add._atomic_write_text", side_effect=fail_registry_write),
            redirect_stderr(stderr_buf),
        ):
            success = add_skill(
                self.aikito_dir,
                self.home,
                from_source=ext_dir,
                force=True,
            )

        self.assertFalse(success)
        self.assertIn("Simulated registry write failure", stderr_buf.getvalue())
        self.assertEqual(canonical_file.read_text(encoding="utf-8"), original_content)

    def test_add_skill_frontmatter_preserves_other_fields(self) -> None:
        ext_dir = self.home / "complex-ext"
        ext_dir.mkdir()
        complex_skill_content = """---
name: original-name
description: original description
license: MIT
compatibility: [claude, codex]
metadata:
  version: 2.1
  author: aikito-team
allowed-tools:
  - bash
  - git
---

# Complex Skill

Body content here.
"""
        (ext_dir / "SKILL.md").write_text(complex_skill_content, encoding="utf-8")

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="renamed-skill",
            description="Updated description: with colon",
            from_source=ext_dir,
        )
        self.assertTrue(success)

        result_file = self.aikito_dir / "skills" / "renamed-skill" / "SKILL.md"
        self.assertTrue(result_file.is_file())
        content = result_file.read_text(encoding="utf-8")

        # Check updated fields
        self.assertIn("name: renamed-skill", content)
        self.assertIn('description: "Updated description: with colon"', content)
        # Check preserved fields
        self.assertIn("license: MIT", content)
        self.assertIn("compatibility: [claude, codex]", content)
        self.assertIn("version: 2.1", content)
        self.assertIn("author: aikito-team", content)
        self.assertIn("allowed-tools:", content)
        self.assertIn("- bash", content)
        self.assertIn("- git", content)
        # Check body preserved
        self.assertIn("# Complex Skill", content)
        self.assertIn("Body content here.", content)

    def test_add_skill_reuse_requires_valid_skill_md(self) -> None:
        proj = self.home / "my-proj"
        proj.mkdir()
        init_project(self.aikito_dir, proj, "my-proj")

        # Case 1: skill dir exists but is empty (no SKILL.md)
        invalid_skill_dir = self.aikito_dir / "skills" / "broken-skill"
        invalid_skill_dir.mkdir(parents=True)

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_skill(
                self.aikito_dir,
                self.home,
                name="broken-skill",
                project_name="my-proj",
            )
        self.assertFalse(success)
        self.assertIn(
            "not a valid skill directory (missing SKILL.md)", stderr_buf.getvalue()
        )

        agent_toml = self.aikito_dir / "projects" / "my-proj" / "agent.toml"
        with agent_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertNotIn("broken-skill", data.get("skills", []))

        # Case 2: skill path is a regular file, not a directory
        regular_file = self.aikito_dir / "skills" / "file-skill"
        regular_file.write_text("just a file", encoding="utf-8")

        stderr_buf2 = io.StringIO()
        with redirect_stderr(stderr_buf2):
            success2 = add_skill(
                self.aikito_dir,
                self.home,
                name="file-skill",
                project_name="my-proj",
            )
        self.assertFalse(success2)
        self.assertIn("not a valid skill directory", stderr_buf2.getvalue())

    def test_add_skill_multi_project_failure_rolls_back_cleanly(self) -> None:
        proj_a = self.home / "project-a"
        proj_b = self.home / "project-b"
        proj_a.mkdir()
        proj_b.mkdir()
        init_project(self.aikito_dir, proj_a, "project-a")
        init_project(self.aikito_dir, proj_b, "project-b")

        toml_a = self.aikito_dir / "projects" / "project-a" / "agent.toml"
        toml_b = self.aikito_dir / "projects" / "project-b" / "agent.toml"

        orig_toml_a = toml_a.read_text(encoding="utf-8")
        orig_toml_b = toml_b.read_text(encoding="utf-8")

        # Simulate failure when writing to project-b's agent.toml,
        # verifying that even if project-b was partially corrupted during failure,
        # all projects in the plan are fully rolled back to original text and canonical skill is cleaned up.
        real_atomic_write = aikito.add._atomic_write_text

        def mock_atomic_write(path_obj: Path, content: str, *args, **kwargs):
            if path_obj.resolve() == toml_b.resolve() and "atomic-skill" in content:
                path_obj.write_text("skills = [\n", encoding="utf-8")
                raise OSError("Simulated disk error writing project-b agent.toml")
            return real_atomic_write(path_obj, content, *args, **kwargs)

        stderr_buf = io.StringIO()
        with patch("aikito.add._atomic_write_text", side_effect=mock_atomic_write):
            with redirect_stderr(stderr_buf):
                success = add_skill(
                    self.aikito_dir,
                    self.home,
                    name="atomic-skill",
                    projects=["project-a", "project-b"],
                )

        self.assertFalse(success)
        self.assertIn("Simulated disk error", stderr_buf.getvalue())

        # Project A must be rolled back to its exact original text
        self.assertEqual(toml_a.read_text(encoding="utf-8"), orig_toml_a)
        # Project B must be cleanly restored to its exact original text (not left corrupted as 'skills = [\n')
        self.assertEqual(toml_b.read_text(encoding="utf-8"), orig_toml_b)

        # Canonical skill directory must not exist
        skill_dir = self.aikito_dir / "skills" / "atomic-skill"
        self.assertFalse(skill_dir.exists())

    def test_add_skill_frontmatter_delimiter_with_dashes_in_values(self) -> None:
        ext_dir = self.home / "dashes-ext"
        ext_dir.mkdir()
        skill_content = """---
name: old-skill
description: Use --- as a separator
license: MIT
compatibility: [claude, codex]
---

# Old Skill Title

Some body content here with --- as horizontal rule.
"""
        (ext_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="new-skill",
            description="Use --- as separator in new desc",
            from_source=ext_dir,
        )
        self.assertTrue(success)

        result_file = self.aikito_dir / "skills" / "new-skill" / "SKILL.md"
        self.assertTrue(result_file.is_file())
        content = result_file.read_text(encoding="utf-8")

        self.assertIn("name: new-skill", content)
        self.assertIn("description: Use --- as separator in new desc", content)
        self.assertIn("license: MIT", content)
        self.assertIn("compatibility: [claude, codex]", content)
        self.assertIn("# Old Skill Title", content)
        self.assertIn("Some body content here with --- as horizontal rule.", content)

        # Ensure license and compatibility did not leak into body
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "new-skill")
        self.assertEqual(meta.get("description"), "Use --- as separator in new desc")
        self.assertEqual(meta.get("license"), "MIT")
        self.assertNotIn("license:", body)
        self.assertNotIn("compatibility:", body)

    def test_add_skill_frontmatter_preserves_description_with_dashes_when_renaming(
        self,
    ) -> None:
        ext_dir = self.home / "keep-desc-ext"
        ext_dir.mkdir()
        skill_content = """---
name: old-skill
description: Use --- as a separator
license: MIT
---

# Old Skill Title

Body content.
"""
        (ext_dir / "SKILL.md").write_text(skill_content, encoding="utf-8")

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="renamed-skill",
            from_source=ext_dir,
        )
        self.assertTrue(success)

        result_file = self.aikito_dir / "skills" / "renamed-skill" / "SKILL.md"
        content = result_file.read_text(encoding="utf-8")
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "renamed-skill")
        self.assertEqual(meta.get("description"), "Use --- as a separator")
        self.assertEqual(meta.get("license"), "MIT")
        self.assertNotIn("license:", body)

    def test_atomic_write_text_preserves_target_on_write_failure(self) -> None:
        target = self.home / "atomic_target.txt"
        target.write_text("initial content", encoding="utf-8")

        real_named_temp = tempfile.NamedTemporaryFile

        def broken_temp(*args, **kwargs):
            tf = real_named_temp(*args, **kwargs)
            orig_write = tf.write

            def failing_write(data):
                orig_write(data[:5])
                raise IOError("Interrupted disk write")

            tf.write = failing_write
            return tf

        with patch("tempfile.NamedTemporaryFile", side_effect=broken_temp):
            with self.assertRaises(IOError):
                _atomic_write_text(target, "new shiny content")

        # Target file must retain initial content completely untouched
        self.assertEqual(target.read_text(encoding="utf-8"), "initial content")

    def test_add_skill_failure_during_skill_write_removes_created_dir_and_allows_retry(
        self,
    ) -> None:
        proj = self.home / "retry-proj"
        proj.mkdir()
        init_project(self.aikito_dir, proj, "retry-proj")

        skill_dir = self.aikito_dir / "skills" / "leaked-skill"
        skill_file = skill_dir / "SKILL.md"

        real_atomic_write = aikito.add._atomic_write_text

        def mock_atomic_write(path_obj: Path, content: str, *args, **kwargs):
            if path_obj.resolve() == skill_file.resolve():
                raise OSError("Simulated disk error writing SKILL.md")
            return real_atomic_write(path_obj, content, *args, **kwargs)

        # 1. First attempt fails during skill file write
        stderr_buf = io.StringIO()
        with patch("aikito.add._atomic_write_text", side_effect=mock_atomic_write):
            with redirect_stderr(stderr_buf):
                success = add_skill(
                    self.aikito_dir,
                    self.home,
                    name="leaked-skill",
                    project_name="retry-proj",
                )

        self.assertFalse(success)
        self.assertIn("Simulated disk error", stderr_buf.getvalue())
        # The empty skill_dir must NOT be left on disk
        self.assertFalse(skill_dir.exists())

        # 2. Subsequent attempt succeeds and creates valid skill
        success2 = add_skill(
            self.aikito_dir,
            self.home,
            name="leaked-skill",
            project_name="retry-proj",
        )
        self.assertTrue(success2)
        self.assertTrue(skill_dir.is_dir())
        self.assertTrue(skill_file.is_file())

    def test_atomic_write_text_preserves_file_permissions(self) -> None:
        target = self.home / "perm_target.txt"
        target.write_text("initial content", encoding="utf-8")

        if not is_windows():
            target.chmod(0o644)
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)

        _atomic_write_text(target, "updated content")
        self.assertEqual(target.read_text(encoding="utf-8"), "updated content")

        if not is_windows():
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o644)
        else:
            self.assertTrue(os.access(target, os.R_OK | os.W_OK))

        # Test with agent.toml update in add_skill
        proj = self.home / "perm-proj"
        proj.mkdir()
        init_project(self.aikito_dir, proj, "perm-proj")
        agent_toml = self.aikito_dir / "projects" / "perm-proj" / "agent.toml"

        if not is_windows():
            agent_toml.chmod(0o644)

        success = add_skill(
            self.aikito_dir,
            self.home,
            name="perm-skill",
            project_name="perm-proj",
        )
        self.assertTrue(success)
        self.assertIn("perm-skill", agent_toml.read_text(encoding="utf-8"))

        if not is_windows():
            self.assertEqual(stat.S_IMODE(agent_toml.stat().st_mode), 0o644)
        else:
            self.assertTrue(os.access(agent_toml, os.R_OK | os.W_OK))

    def test_atomic_write_text_respects_umask_on_new_files(self) -> None:
        target = self.home / "new_atomic_file.txt"
        self.assertFalse(target.exists())

        if not is_windows():
            current_umask = os.umask(0)
            os.umask(current_umask)
            expected_mode = 0o666 & ~current_umask

            _atomic_write_text(target, "new file content")
            self.assertTrue(target.exists())
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), expected_mode)
            self.assertEqual(target.read_text(encoding="utf-8"), "new file content")
        else:
            _atomic_write_text(target, "new file content")
            self.assertTrue(target.exists())
            self.assertEqual(target.read_text(encoding="utf-8"), "new file content")
            self.assertTrue(os.access(target, os.R_OK | os.W_OK))


class TestParseMarkdownFrontmatter(unittest.TestCase):
    def test_parse_literal_block_scalar(self) -> None:
        content = """---
name: my-agent
description: |
  Line one of description.
  Line two of description.
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "my-agent")
        self.assertEqual(
            meta.get("description"),
            "Line one of description.\nLine two of description.",
        )
        self.assertIsInstance(meta.get("description"), str)
        self.assertIn("# Instructions", body)

    def test_parse_folded_block_scalar(self) -> None:
        content = """---
name: my-agent
description: >
  Line one of description.
  Line two of description.
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "my-agent")
        self.assertEqual(
            meta.get("description"),
            "Line one of description. Line two of description.",
        )
        self.assertIsInstance(meta.get("description"), str)

    def test_parse_unmarked_multiline_text(self) -> None:
        content = """---
name: my-agent
description:
  This is a multiline description without
  an explicit block scalar indicator.
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "my-agent")
        self.assertEqual(
            meta.get("description"),
            "This is a multiline description without an explicit block scalar indicator.",
        )
        self.assertIsInstance(meta.get("description"), str)
        self.assertNotIsInstance(meta.get("description"), dict)

    def test_parse_empty_scalar_returns_empty_string(self) -> None:
        content = """---
name: my-agent
description:
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "my-agent")
        self.assertEqual(meta.get("description"), "")
        self.assertIsInstance(meta.get("description"), str)

    def test_parse_no_implicit_number_conversion(self) -> None:
        content = """---
name: 2024
model: 4.5
version: 1
user-invocable: false
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "2024")
        self.assertIsInstance(meta.get("name"), str)
        self.assertEqual(meta.get("model"), "4.5")
        self.assertIsInstance(meta.get("model"), str)
        self.assertEqual(meta.get("version"), "1")
        self.assertIsInstance(meta.get("version"), str)
        self.assertIs(meta.get("user-invocable"), False)

    def test_parse_nested_platform_table(self) -> None:
        content = """---
name: multi-helper
codex:
  model: gpt-4o
claude-code:
  model: claude-3-5-sonnet
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("name"), "multi-helper")
        self.assertEqual(meta.get("codex"), {"model": "gpt-4o"})
        self.assertEqual(meta.get("claude-code"), {"model": "claude-3-5-sonnet"})

    def test_parse_indented_list(self) -> None:
        content = """---
name: multi-helper
agents:
  - codex
  - claude-code
---
# Instructions
"""
        meta, body = _parse_markdown_frontmatter(content)
        self.assertEqual(meta.get("agents"), ["codex", "claude-code"])


class TestAikitoAddSubagent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.aikito_dir = self.home / "aikito"
        init_workspace(self.aikito_dir, self.home)
        (self.aikito_dir / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_add_subagent_default_agents(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="code-reviewer",
                description="Performs automated code reviews.",
            )
        self.assertTrue(success)

        subagent_file = self.aikito_dir / "subagents" / "code-reviewer.md"
        self.assertTrue(subagent_file.is_file())
        content = subagent_file.read_text(encoding="utf-8")
        self.assertIn("# Code Reviewer", content)

        subagents_toml = self.aikito_dir / "subagents.toml"
        with subagents_toml.open("rb") as f:
            data = tomllib.load(f)
        subs = data.get("subagents", {})
        self.assertIn("code-reviewer", subs)
        self.assertEqual(
            subs["code-reviewer"]["description"],
            "Performs automated code reviews.",
        )
        self.assertEqual(
            subs["code-reviewer"]["agents"],
            ["codex", "claude-code", "agy", "github-copilot"],
        )

    def test_add_subagent_custom_agents(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="tester",
                description="Runs integration tests.",
                agents=["claude-code", "codex"],
            )
        self.assertTrue(success)

        subagents_toml = self.aikito_dir / "subagents.toml"
        with subagents_toml.open("rb") as f:
            data = tomllib.load(f)
        subs = data.get("subagents", {})
        self.assertIn("tester", subs)
        self.assertEqual(subs["tester"]["agents"], ["claude-code", "codex"])

    def test_add_subagent_duplicate_rejected(self) -> None:
        add_subagent(self.aikito_dir, self.home, name="verifier-agent")

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(self.aikito_dir, self.home, name="verifier-agent")
        self.assertFalse(success)
        self.assertIn("already registered", stderr_buf.getvalue())

    def test_add_subagent_from_file_with_frontmatter(self) -> None:
        src = self.home / "reviewer.md"
        src.write_text(
            "---\n"
            "name: code-reviewer\n"
            "description: Automated code review specialist\n"
            'agents: ["codex", "claude-code"]\n'
            "---\n"
            "# Code Reviewer\n\n"
            "Please review diffs carefully.\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
            )
        self.assertTrue(success)

        subagent_file = self.aikito_dir / "subagents" / "code-reviewer.md"
        self.assertTrue(subagent_file.is_file())
        content = subagent_file.read_text(encoding="utf-8")
        self.assertNotIn("name: code-reviewer", content)
        self.assertIn("# Code Reviewer", content)
        self.assertIn("Please review diffs carefully.", content)

        subagents_toml = self.aikito_dir / "subagents.toml"
        data = tomllib.loads(subagents_toml.read_text(encoding="utf-8"))
        self.assertIn("code-reviewer", data["subagents"])
        self.assertEqual(
            data["subagents"]["code-reviewer"]["description"],
            "Automated code review specialist",
        )
        self.assertEqual(
            data["subagents"]["code-reviewer"]["agents"],
            ["codex", "claude-code"],
        )

    def test_add_subagent_from_file_without_frontmatter_infers_name(self) -> None:
        src = self.home / "security-guard.md"
        src.write_text(
            "# Security Guard\n\nScan code for vulnerabilities.\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
            )
        self.assertTrue(success)

        subagent_file = self.aikito_dir / "subagents" / "security-guard.md"
        self.assertTrue(subagent_file.is_file())
        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        self.assertIn("security-guard", data["subagents"])
        self.assertEqual(
            data["subagents"]["security-guard"]["description"],
            "Subagent security-guard.",
        )

    def test_add_subagent_from_copilot_agent_file(self) -> None:
        src = self.home / "auditor.agent.md"
        src.write_text(
            "---\n"
            "name: auditor\n"
            "description: Copilot auditor agent\n"
            'tools: ["read", "search"]\n'
            "user-invocable: false\n"
            "---\n"
            "Audit all files for compliance.\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
                agents=["github-copilot"],
            )
        self.assertTrue(success)

        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        self.assertIn("auditor", data["subagents"])
        auditor_sec = data["subagents"]["auditor"]
        self.assertEqual(auditor_sec["agents"], ["github-copilot"])
        self.assertIn("github-copilot", auditor_sec)
        self.assertEqual(auditor_sec["github-copilot"]["tools"], ["read", "search"])
        self.assertFalse(auditor_sec["github-copilot"]["user-invocable"])

    def test_add_subagent_from_directory(self) -> None:
        src_dir = self.home / "my-helper"
        src_dir.mkdir()
        (src_dir / "instructions.md").write_text(
            "# Helper\n\nHelp user solve tasks.\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src_dir,
            )
        self.assertTrue(success)
        self.assertTrue((self.aikito_dir / "subagents" / "my-helper.md").is_file())

    def test_add_subagent_validations(self) -> None:
        # Missing name without --from
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(self.aikito_dir, self.home, name=None)
        self.assertFalse(success)
        self.assertIn("Subagent name is required", stderr_buf.getvalue())

        # --force without --from
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(
                self.aikito_dir, self.home, name="test-subagent", force=True
            )
        self.assertFalse(success)
        self.assertIn("--force requires --from", stderr_buf.getvalue())

        # Empty instructions file
        empty_src = self.home / "empty.md"
        empty_src.write_text("", encoding="utf-8")
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(
                self.aikito_dir, self.home, name="empty-subagent", from_source=empty_src
            )
        self.assertFalse(success)
        self.assertIn("does not contain any instructions", stderr_buf.getvalue())

    def test_add_subagent_force_overwrites_existing(self) -> None:
        # Create initial subagent
        add_subagent(
            self.aikito_dir,
            self.home,
            name="refactorer",
            description="Initial description",
        )

        new_src = self.home / "refactorer-v2.md"
        new_src.write_text(
            "---\n"
            "description: Upgraded refactorer persona\n"
            "---\n"
            "# Refactorer V2\n\nPerform aggressive semantic refactoring.\n",
            encoding="utf-8",
        )

        # Without force -> rejected
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            rejected = add_subagent(
                self.aikito_dir,
                self.home,
                name="refactorer",
                from_source=new_src,
                force=False,
            )
        self.assertFalse(rejected)
        self.assertIn("already registered", stderr_buf.getvalue())

        # With force -> overwritten
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="refactorer",
                from_source=new_src,
                force=True,
            )
        self.assertTrue(success)

        content = (self.aikito_dir / "subagents" / "refactorer.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Refactorer V2", content)
        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        self.assertEqual(
            data["subagents"]["refactorer"]["description"],
            "Upgraded refactorer persona",
        )

    def test_add_subagent_with_sync(self) -> None:
        claude_agents_dir = self.home / ".claude" / "agents"
        claude_agents_dir.mkdir(parents=True)

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="live-agent",
                description="Live synced subagent",
                agents=["claude-code"],
                sync=True,
            )
        self.assertTrue(success)

        # Check that runtime file was rendered
        claude_target = claude_agents_dir / "live-agent.md"
        self.assertTrue(claude_target.is_file())
        claude_text = claude_target.read_text(encoding="utf-8")
        self.assertIn("generated by aikito from subagents/live-agent.md", claude_text)
        self.assertIn("Live synced subagent", claude_text)

    def test_add_subagent_force_preserves_existing_agents_and_description(self) -> None:
        # Create initial subagent with custom agents and description
        add_subagent(
            self.aikito_dir,
            self.home,
            name="specialist",
            description="Specialized reviewer",
            agents=["claude-code"],
        )

        # New source has NO frontmatter, only new body instructions
        new_src = self.home / "new-specialist.md"
        new_src.write_text(
            "# Specialist Instructions\n\nOnly review Python code.\n",
            encoding="utf-8",
        )

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="specialist",
                from_source=new_src,
                force=True,
            )
        self.assertTrue(success)

        # Check subagents.toml: agents and description must NOT be reset to defaults
        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        spec = data["subagents"]["specialist"]
        self.assertEqual(spec["agents"], ["claude-code"])
        self.assertEqual(spec["description"], "Specialized reviewer")

        # Instructions file must be updated
        instructions = (self.aikito_dir / "subagents" / "specialist.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Only review Python code.", instructions)

    def test_add_subagent_sync_blocks_on_unmanaged_agent_conflict(self) -> None:
        claude_agents_dir = self.home / ".claude" / "agents"
        claude_agents_dir.mkdir(parents=True)
        unmanaged_file = claude_agents_dir / "conflict-sub.md"
        unmanaged_file.write_text(
            "# Handcrafted subagent without marker\n", encoding="utf-8"
        )

        prompt_file = self.home / "conflict-sub.md"
        prompt_file.write_text("# Aikito prompt\n", encoding="utf-8")

        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                name="conflict-sub",
                agents=["claude-code"],
                from_source=prompt_file,
                force=True,
                sync=True,
            )
        self.assertFalse(success)
        self.assertIn("[CONFLICT]", stdout_buf.getvalue())
        # Unmanaged file was NOT overwritten
        self.assertEqual(
            unmanaged_file.read_text(encoding="utf-8"),
            "# Handcrafted subagent without marker\n",
        )

    def test_add_subagent_top_level_platform_options_ambiguous_error(self) -> None:
        src = self.home / "ambiguous.md"
        src.write_text(
            "---\n"
            "name: ambiguous-agent\n"
            'tools: ["read_file"]\n'
            "model: gpt-4o\n"
            "---\n"
            "# Ambiguous\n",
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
            )
        self.assertFalse(success)
        err_msg = stderr_buf.getvalue()
        self.assertIn("[ERROR]", err_msg)
        self.assertIn("multiple target agents are specified", err_msg)
        self.assertIn("--agents", err_msg)

    def test_add_subagent_top_level_model_passthrough_single_agent(self) -> None:
        src = self.home / "single-agent.md"
        src.write_text(
            "---\nname: claude-reviewer\nmodel: claude-3-7-sonnet\n---\n# Reviewer\n",
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
                agents=["claude-code"],
            )
        self.assertTrue(success)
        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        self.assertIn("claude-reviewer", data["subagents"])
        sec = data["subagents"]["claude-reviewer"]
        self.assertEqual(sec["agents"], ["claude-code"])
        self.assertIn("claude-code", sec)
        self.assertEqual(sec["claude-code"]["model"], "claude-3-7-sonnet")

    def test_add_subagent_top_level_tools_unsupported_on_agent_fails(self) -> None:
        src = self.home / "unsupported-tools.md"
        src.write_text(
            '---\nname: claude-tools\ntools: ["read_file"]\n---\n# Claude Tools\n',
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
                agents=["claude-code"],
            )
        self.assertFalse(success)
        err_msg = stderr_buf.getvalue()
        self.assertIn("[ERROR]", err_msg)
        self.assertIn(
            "contains unknown field 'tools' for platform 'claude-code'", err_msg
        )

    def test_add_subagent_explicit_platform_tables_multiple_agents(self) -> None:
        src = self.home / "multi-platform.md"
        src.write_text(
            "---\n"
            "name: multi-helper\n"
            'agents: ["codex", "claude-code"]\n'
            "codex:\n"
            "  model: gpt-4o\n"
            "claude-code:\n"
            "  model: claude-3-5-sonnet\n"
            "---\n"
            "# Multi Helper\n",
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
            )
        self.assertTrue(success)
        data = tomllib.loads(
            (self.aikito_dir / "subagents.toml").read_text(encoding="utf-8")
        )
        sec = data["subagents"]["multi-helper"]
        self.assertEqual(sec["agents"], ["codex", "claude-code"])
        self.assertEqual(sec["codex"]["model"], "gpt-4o")
        self.assertEqual(sec["claude-code"]["model"], "claude-3-5-sonnet")

    def test_add_subagent_explicit_platform_table_invalid_option_fails(self) -> None:
        src = self.home / "invalid-platform-opt.md"
        src.write_text(
            "---\n"
            "name: bad-opt-agent\n"
            "codex:\n"
            "  unsupported_field: 123\n"
            "---\n"
            "# Bad Opt\n",
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_subagent(
                self.aikito_dir,
                self.home,
                from_source=src,
            )
        self.assertFalse(success)
        err_msg = stderr_buf.getvalue()
        self.assertIn("[ERROR]", err_msg)
        self.assertIn(
            "contains unknown field 'unsupported_field' for platform 'codex'", err_msg
        )


class TestAikitoAddMCP(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.aikito_dir = self.home / "aikito"
        init_workspace(self.aikito_dir, self.home)
        (self.aikito_dir / "agents.toml").write_text(
            load_agents_template(), encoding="utf-8"
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_add_mcp_default_stdio(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="sqlite-mcp",
            )
        self.assertTrue(success)

        mcp_file = self.aikito_dir / "mcps" / "sqlite-mcp.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["command"], "npx")
        self.assertEqual(data["args"], [])
        self.assertIn("codex", data["agents"])

    def test_add_mcp_custom_stdio(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="custom-cli",
                command="uvx mcp-server-custom",
                agents=["codex", "claude-code"],
            )
        self.assertTrue(success)

        mcp_file = self.aikito_dir / "mcps" / "custom-cli.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["command"], "uvx mcp-server-custom")
        self.assertEqual(data["agents"], ["codex", "claude-code"])

    def test_add_mcp_remote(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="github-remote",
                url="https://api.githubcopilot.com/mcp",
                agents=["agy", "codex"],
            )
        self.assertTrue(success)

        mcp_file = self.aikito_dir / "mcps" / "github-remote.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://api.githubcopilot.com/mcp")
        self.assertEqual(data["agents"], ["agy", "codex"])

    def test_add_mcp_duplicate_rejected(self) -> None:
        add_mcp(self.aikito_dir, self.home, name="test-server")

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(self.aikito_dir, self.home, name="test-server")
        self.assertFalse(success)
        self.assertIn("already exists", stderr_buf.getvalue())

    def test_add_mcp_conflicts(self) -> None:
        # 1. Both --command and --url
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="bad-server-1",
                command="npx foo",
                url="https://example.com/mcp",
            )
        self.assertFalse(success)
        self.assertIn("Cannot specify both --command and --url", stderr_buf.getvalue())

        # 2. stdio with --url
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="bad-server-2",
                transport="stdio",
                url="https://example.com/mcp",
            )
        self.assertFalse(success)
        self.assertIn(
            "Cannot specify --url when --transport is 'stdio'", stderr_buf.getvalue()
        )

        # 3. remote with --command
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="bad-server-3",
                transport="remote",
                command="npx foo",
            )
        self.assertFalse(success)
        self.assertIn(
            "Cannot specify --command when --transport is 'remote'",
            stderr_buf.getvalue(),
        )

        # 4. remote without --url
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="bad-server-4",
                transport="remote",
            )
        self.assertFalse(success)
        self.assertIn(
            "--url is required when --transport is 'remote'", stderr_buf.getvalue()
        )

    def test_add_mcp_from_remote_url(self) -> None:
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source="https://example.com/v1/my-api",
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "my-api.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://example.com/v1/my-api")
        self.assertEqual(
            data["agents"],
            ["codex", "claude-code", "opencode", "agy", "github-copilot"],
        )

    def test_add_mcp_from_json_file_sanitizes_secret_header(self) -> None:
        cfg = self.home / "weather.json"
        cfg.write_text(
            json.dumps(
                {
                    "url": "https://weather.example.com/mcp",
                    "headers": {"Authorization": "Bearer key123"},
                }
            ),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "weather.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://weather.example.com/mcp")
        # Assert secret was sanitized to an environment variable reference
        self.assertEqual(
            data["headers"]["Authorization"], "${AIKITO_WEATHER_AUTHORIZATION}"
        )
        self.assertIn("[SECURITY]", stderr_buf.getvalue())

    def test_add_mcp_preserves_env_reference_header(self) -> None:
        cfg = self.home / "custom.json"
        cfg.write_text(
            json.dumps(
                {
                    "url": "https://custom.example.com/mcp",
                    "headers": {"X-Custom": "safe-value", "Token": "${CUSTOM_TOKEN}"},
                }
            ),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "custom.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["headers"]["X-Custom"], "safe-value")
        self.assertEqual(data["headers"]["Token"], "${CUSTOM_TOKEN}")
        self.assertNotIn("[SECURITY]", stderr_buf.getvalue())

    def test_add_mcp_from_multiserver_json_with_name(self) -> None:
        cfg = self.home / "claude_desktop.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "github_tool": {"url": "https://api.github.com/mcp"},
                        "local_stdio": {"command": "npx"},
                    }
                }
            ),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="github-tool",
                from_source=cfg,
                agents=["claude-code", "codex"],
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "github-tool.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://api.github.com/mcp")
        self.assertEqual(data["agents"], ["claude-code", "codex"])

    def test_add_mcp_from_multiserver_ambiguous_error(self) -> None:
        cfg = self.home / "multi.json"
        cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "server_a": {"url": "https://a.com"},
                        "server_b": {"url": "https://b.com"},
                    }
                }
            ),
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=cfg,
            )
        self.assertFalse(success)
        self.assertIn("multiple MCP servers", stderr_buf.getvalue())

    def test_add_mcp_rejects_stdio_import(self) -> None:
        cfg = self.home / "stdio_only.json"
        cfg.write_text(
            json.dumps({"command": "npx", "args": ["-y", "pkg"]}),
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="stdio-only",
                from_source=cfg,
            )
        self.assertFalse(success)
        self.assertIn(
            "supports synchronizing remote MCP servers", stderr_buf.getvalue()
        )

    def test_add_mcp_force_preserves_customizations(self) -> None:
        # Create an existing server with custom agents, authentication, and overrides
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        mcp_file = self.aikito_dir / "mcps" / "preserve-test.toml"
        mcp_file.write_text(
            """
transport = "remote"
url = "https://old.example.com"
agents = ["codex", "claude-code"]

[authentication]
account_email = "user@test.org"
token_env = "TEST_PAT"
authorization_env = "TEST_AUTH"

[overrides.opencode]
timeout = 45000
enabled = false
reason = "Testing preserve"
""".lstrip(),
            encoding="utf-8",
        )

        # Force update with only a new URL and no agents specified
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="preserve-test",
                url="https://new.example.com",
                force=True,
            )
        self.assertTrue(success)

        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        # URL updated
        self.assertEqual(data["url"], "https://new.example.com")
        # Custom agents preserved
        self.assertEqual(data["agents"], ["codex", "claude-code"])
        # Authentication block preserved
        self.assertIn("authentication", data)
        self.assertEqual(data["authentication"]["account_email"], "user@test.org")
        self.assertEqual(data["authentication"]["token_env"], "TEST_PAT")
        # Overrides block preserved
        self.assertIn("overrides", data)
        self.assertEqual(data["overrides"]["opencode"]["timeout"], 45000)
        self.assertFalse(data["overrides"]["opencode"]["enabled"])
        self.assertEqual(data["overrides"]["opencode"]["reason"], "Testing preserve")

    def test_add_mcp_sync_failure_reverts_file(self) -> None:
        # 1. New file creation: sync failure unlinks newly created file
        with patch("aikito.mcp.sync_mcp_configs", return_value=False):
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                success = add_mcp(
                    self.aikito_dir,
                    self.home,
                    name="fail-sync-new",
                    url="https://fail.example.com",
                    sync=True,
                )
            self.assertFalse(success)
            self.assertFalse((self.aikito_dir / "mcps" / "fail-sync-new.toml").exists())
            self.assertIn("Reverted changes", stderr_buf.getvalue())

        # 2. Existing file update: sync failure restores original content
        mcp_file = self.aikito_dir / "mcps" / "fail-sync-existing.toml"
        mcp_file.write_text(
            'transport = "remote"\nurl = "https://original.com"\nagents = ["codex"]\n',
            encoding="utf-8",
        )
        with patch("aikito.mcp.sync_mcp_configs", return_value=False):
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                success = add_mcp(
                    self.aikito_dir,
                    self.home,
                    name="fail-sync-existing",
                    url="https://attempted-new.com",
                    force=True,
                    sync=True,
                )
            self.assertFalse(success)
            # Assert original content was restored
            restored_content = mcp_file.read_text(encoding="utf-8")
            self.assertIn("https://original.com", restored_content)
            self.assertNotIn("https://attempted-new.com", restored_content)
            self.assertIn("Reverted changes", stderr_buf.getvalue())

    def test_add_mcp_force_does_not_pass_force_to_sync(self) -> None:
        with patch("aikito.mcp.sync_mcp_configs", return_value=True) as mock_sync:
            stdout_buf = io.StringIO()
            with redirect_stdout(stdout_buf):
                success = add_mcp(
                    self.aikito_dir,
                    self.home,
                    name="decoupled-force",
                    url="https://sync.example.com",
                    force=True,
                    sync=True,
                )
            self.assertTrue(success)
            # Crucial check: force=False passed to sync_mcp_configs to preserve agent conflict protections
            self.assertTrue(mock_sync.called)
            self.assertFalse(mock_sync.call_args.kwargs["force"])
            self.assertEqual(
                mock_sync.call_args.kwargs["aikito_dir"],
                self.aikito_dir.resolve(),
            )

    def test_add_mcp_from_toml_file(self) -> None:
        # 1. Single server in TOML under [servers.<name>]
        toml_cfg = self.home / "codex_config.toml"
        toml_cfg.write_text(
            """
[servers.linear_app]
url = "https://linear.app/mcp"
""".lstrip(),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=toml_cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "linear-app.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://linear.app/mcp")

        # 2. Multi-server TOML with explicit name selection
        multi_toml = self.home / "multi_config.toml"
        multi_toml.write_text(
            """
[mcp_servers.figma]
url = "https://figma.com/mcp"

[mcp_servers.slack]
url = "https://slack.com/mcp"
""".lstrip(),
            encoding="utf-8",
        )
        with redirect_stdout(stdout_buf):
            success2 = add_mcp(
                self.aikito_dir,
                self.home,
                name="figma",
                from_source=multi_toml,
            )
        self.assertTrue(success2)
        figma_file = self.aikito_dir / "mcps" / "figma.toml"
        self.assertTrue(figma_file.is_file())
        with figma_file.open("rb") as f:
            data2 = tomllib.load(f)
        self.assertEqual(data2["url"], "https://figma.com/mcp")

    def test_add_mcp_from_jsonc_with_comments(self) -> None:
        jsonc_cfg = self.home / "settings.jsonc"
        jsonc_cfg.write_text(
            """// Claude / VS Code settings with JSON comments
{
    /* MCP servers registry */
    "mcpServers": {
        "context7": {
            // Remote endpoint
            "url": "https://context7.ai/mcp"
        }
    }
}
""",
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=jsonc_cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "context7.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["transport"], "remote")
        self.assertEqual(data["url"], "https://context7.ai/mcp")

    def test_add_mcp_url_generic_path_requires_explicit_name(self) -> None:
        for generic_url in [
            "https://api.githubcopilot.com/mcp",
            "https://example.com/sse",
            "https://service.org/api/v1",
        ]:
            stderr_buf = io.StringIO()
            with redirect_stderr(stderr_buf):
                success = add_mcp(
                    self.aikito_dir,
                    self.home,
                    from_source=generic_url,
                )
            self.assertFalse(success)
            self.assertIn("MCP server name is required", stderr_buf.getvalue())

        # With explicit name, it should succeed
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="copilot",
                from_source="https://api.githubcopilot.com/mcp",
            )
        self.assertTrue(success)
        self.assertTrue((self.aikito_dir / "mcps" / "copilot.toml").exists())

    def test_add_mcp_force_without_args_rejected(self) -> None:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="lonely-server",
                force=True,
            )
        self.assertFalse(success)
        self.assertIn(
            "--force requires --from or server configuration arguments",
            stderr_buf.getvalue(),
        )

    def test_add_mcp_force_with_stdio_transport_accepted(self) -> None:
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        mcp_file = self.aikito_dir / "mcps" / "local-tool.toml"
        mcp_file.write_text(
            'command = "old-cmd"\nargs = []\nagents = ["codex"]\n',
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="local-tool",
                transport="stdio",
                command="new-cmd",
                force=True,
            )
        self.assertTrue(success)
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["command"], "new-cmd")

    def test_add_mcp_when_mcps_directory_does_not_exist(self) -> None:
        mcps_dir = self.aikito_dir / "mcps"
        if mcps_dir.exists():
            shutil.rmtree(mcps_dir)
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="fresh-server",
                url="https://fresh.example.com",
            )
        self.assertTrue(success)
        self.assertTrue((mcps_dir / "fresh-server.toml").is_file())

    def test_add_mcp_preserves_multiple_env_reference_formats(self) -> None:
        cfg = self.home / "env_refs.json"
        cfg.write_text(
            json.dumps(
                {
                    "url": "https://refs.example.com/mcp",
                    "headers": {
                        "H1": "${VAR_ONE}",
                        "Authorization": "{env:VAR_TWO}",
                        "H3": "!!js process.env.VAR_THREE",
                    },
                }
            ),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        stderr_buf = io.StringIO()
        with redirect_stdout(stdout_buf), redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="ref-server",
                from_source=cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "ref-server.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["headers"]["H1"], "${VAR_ONE}")
        self.assertEqual(data["headers"]["Authorization"], "{env:VAR_TWO}")
        self.assertEqual(data["headers"]["H3"], "!!js process.env.VAR_THREE")
        self.assertNotIn("[SECURITY]", stderr_buf.getvalue())

    def test_add_mcp_sanitizes_password_and_cookie_headers(self) -> None:
        cfg = self.home / "creds.json"
        cfg.write_text(
            json.dumps(
                {
                    "url": "https://creds.example.com/mcp",
                    "headers": {
                        "X-Password": "hunter2",
                        "Cookie": "session=abc",
                    },
                }
            ),
            encoding="utf-8",
        )
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="secret-server",
                from_source=cfg,
            )
        self.assertTrue(success)
        mcp_file = self.aikito_dir / "mcps" / "secret-server.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(
            data["headers"]["X-Password"], "${AIKITO_SECRET_SERVER_X_PASSWORD}"
        )
        self.assertEqual(data["headers"]["Cookie"], "${AIKITO_SECRET_SERVER_COOKIE}")
        stderr_out = stderr_buf.getvalue()
        # Secret values must NOT appear in stderr — only the variable name.
        self.assertIn("AIKITO_SECRET_SERVER_X_PASSWORD", stderr_out)
        self.assertNotIn("hunter2", stderr_out)
        self.assertIn("AIKITO_SECRET_SERVER_COOKIE", stderr_out)
        self.assertNotIn("session=abc", stderr_out)
        # Hint should show placeholder format, not the actual value.
        self.assertIn(
            "export AIKITO_SECRET_SERVER_X_PASSWORD=<your-secret-value>", stderr_out
        )

    def test_add_mcp_force_stdio_preserves_overrides_and_args(self) -> None:
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        mcp_file = self.aikito_dir / "mcps" / "stdio-keep.toml"
        mcp_file.write_text(
            """
command = "old-cmd"
args = ["-y", "pkg"]
agents = ["codex", "claude-code"]

[overrides.codex]
enabled = false
reason = "Disabled for codex"
""".lstrip(),
            encoding="utf-8",
        )
        stdout_buf = io.StringIO()
        with redirect_stdout(stdout_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="stdio-keep",
                transport="stdio",
                command="uvx",
                force=True,
            )
        self.assertTrue(success)
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)
        self.assertEqual(data["command"], "uvx")
        self.assertEqual(data["args"], ["-y", "pkg"])
        self.assertEqual(data["agents"], ["codex", "claude-code"])
        self.assertIn("overrides", data)
        self.assertFalse(data["overrides"]["codex"]["enabled"])
        self.assertEqual(data["overrides"]["codex"]["reason"], "Disabled for codex")

    def test_add_mcp_sync_conflict_preflight_prevents_partial_agent_writes(
        self,
    ) -> None:
        # Pre-seed an unmanaged config for claude-code that will trigger [CONFLICT]
        (self.home / ".claude").mkdir(parents=True, exist_ok=True)
        claude_cfg = self.home / ".claude.json"
        claude_cfg.write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "collide-server": {
                            "type": "http",
                            "url": "https://other.com",
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        codex_cfg = self.home / ".codex" / "config.toml"
        codex_cfg.parent.mkdir(parents=True, exist_ok=True)
        codex_cfg.write_text("# clean initial codex config\n", encoding="utf-8")

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="collide-server",
                url="https://new-url.com",
                agents=["codex", "claude-code"],
                sync=True,
            )
        self.assertFalse(success)
        # Canonical file must be rolled back (unlinked)
        self.assertFalse((self.aikito_dir / "mcps" / "collide-server.toml").exists())
        # Codex config must NOT have been modified by partial sync
        self.assertEqual(
            codex_cfg.read_text(encoding="utf-8"), "# clean initial codex config\n"
        )
        self.assertIn("preflight failed", stderr_buf.getvalue())

    def test_add_mcp_from_codex_merges_http_headers_and_env_http_headers(self) -> None:
        codex_toml = self.home / "codex_source.toml"
        codex_toml.write_text(
            """
[mcp_servers.test-server]
url = "https://example.com/mcp"

[mcp_servers.test-server.http_headers]
X-Static = "static-value"
Authorization = "Bearer secret123"

[mcp_servers.test-server.env_http_headers]
X-Dynamic-Key = "MY_TOKEN_ENV"
""".lstrip(),
            encoding="utf-8",
        )

        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                from_source=codex_toml,
            )
        self.assertTrue(success)

        mcp_file = self.aikito_dir / "mcps" / "test-server.toml"
        self.assertTrue(mcp_file.is_file())
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)

        headers = data["headers"]
        # Static non-sensitive header preserved
        self.assertEqual(headers["X-Static"], "static-value")
        # Sensitive plaintext header sanitized into AIKITO placeholder
        self.assertEqual(
            headers["Authorization"], "${AIKITO_TEST_SERVER_AUTHORIZATION}"
        )
        # Bare env-var name from env_http_headers converted directly to ${MY_TOKEN_ENV}
        self.assertEqual(headers["X-Dynamic-Key"], "${MY_TOKEN_ENV}")
        # Secret value not echoed in stderr
        self.assertNotIn("secret123", stderr_buf.getvalue())

    def test_add_mcp_sanitizes_complex_url_credentials(self) -> None:
        stderr_buf = io.StringIO()
        with redirect_stderr(stderr_buf):
            success = add_mcp(
                self.aikito_dir,
                self.home,
                name="azure-aws-mcp",
                url="https://storage.example.com/mcp?sig=super_secret_sig&x-amz-signature=aws_sig&jwt=token_jwt&access_key=my_key&version=2026&author=john&authority=gov&authentication_mode=saml&private_mode=strict&spr=https",
            )
        self.assertTrue(success)

        mcp_file = self.aikito_dir / "mcps" / "azure-aws-mcp.toml"
        with mcp_file.open("rb") as f:
            data = tomllib.load(f)

        # Sensitive params stripped, legitimate params (author, authority, authentication_mode, private_mode, spr) preserved
        sanitized_url = data["url"]
        self.assertNotIn("sig=super_secret_sig", sanitized_url)
        self.assertNotIn("x-amz-signature=", sanitized_url)
        self.assertNotIn("jwt=", sanitized_url)
        self.assertNotIn("access_key=", sanitized_url)
        self.assertIn("version=2026", sanitized_url)
        self.assertIn("author=john", sanitized_url)
        self.assertIn("authority=gov", sanitized_url)
        self.assertIn("authentication_mode=saml", sanitized_url)
        self.assertIn("private_mode=strict", sanitized_url)
        self.assertIn("spr=https", sanitized_url)

        stderr_out = stderr_buf.getvalue()
        # Secret values must NEVER appear in stderr
        self.assertNotIn("super_secret_sig", stderr_out)
        self.assertNotIn("aws_sig", stderr_out)
        self.assertNotIn("token_jwt", stderr_out)
        self.assertNotIn("my_key", stderr_out)
        # Warning mentions the sensitive parameter names
        self.assertIn("sig", stderr_out)
        self.assertIn("x-amz-signature", stderr_out)
        # Warning must NOT complain about legitimate parameters
        self.assertNotIn("author", stderr_out)
        self.assertNotIn("authority", stderr_out)
        self.assertNotIn("private_mode", stderr_out)
        self.assertNotIn("spr", stderr_out)


if __name__ == "__main__":
    unittest.main()
