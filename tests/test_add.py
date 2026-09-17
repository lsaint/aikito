import io
import os
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
        skills_toml = self.aikito_dir / "skills.toml"
        with skills_toml.open("rb") as f:
            data = tomllib.load(f)
        self.assertIn("code-formatter", data.get("skills", []))

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


class TestAikitoAddSubagent(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.aikito_dir = self.home / "aikito"
        init_workspace(self.aikito_dir, self.home)

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


class TestAikitoAddMCP(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp_dir.name)
        self.aikito_dir = self.home / "aikito"
        init_workspace(self.aikito_dir, self.home)

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


if __name__ == "__main__":
    unittest.main()
