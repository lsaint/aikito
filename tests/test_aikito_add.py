"""Unit tests for aikito add commands and bin/aikito_add.py."""

import io
import sys
import tempfile
import tomllib
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_add import add_mcp, add_skill, add_subagent, validate_resource_name  # noqa: E402
from aikito_init import init_project, init_workspace  # noqa: E402


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
