import importlib.machinery
import importlib.util
import io
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

LOADER = importlib.machinery.SourceFileLoader(
    "aikito_cli", str(ROOT / "bin" / "aikito")
)
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
assert SPEC is not None
AIKITO_CLI = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(AIKITO_CLI)
sys.modules["aikito_cli"] = AIKITO_CLI


class GlobalEntrySyncTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.source = self.root / "source"
        self.agent_dir = self.root / ".agent"
        self.target = self.agent_dir / "skills"
        self.source.mkdir()
        self.agent_dir.mkdir()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_creates_and_preserves_expected_link(self) -> None:
        self.assertTrue(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )
        self.assertEqual(self.target.resolve(), self.source.resolve())
        self.assertTrue(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )

    def test_regular_directory_is_reported_as_conflict(self) -> None:
        self.target.mkdir()

        self.assertFalse(
            AIKITO_CLI.sync_global_entry(
                self.source, self.target, "Test Agent", "skills"
            )
        )
        self.assertTrue(self.target.is_dir())


class SyncSubcommandParserTest(unittest.TestCase):
    def test_diff_command(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args = parser.parse_args(["diff"])

        self.assertEqual(args.command, "diff")
        self.assertEqual(args.func, AIKITO_CLI.cmd_diff)

    def test_sync_subcommands(self) -> None:
        parser = AIKITO_CLI.build_parser()

        # Global
        args = parser.parse_args(["sync", "global"])
        self.assertEqual(args.command, "sync")
        self.assertEqual(args.sync_target, "global")
        self.assertEqual(args.func, AIKITO_CLI.cmd_global_sync)

        # Project with path
        args = parser.parse_args(["sync", "project", "doxturbo", "~/com/doxturbo"])
        self.assertEqual(args.sync_target, "project")
        self.assertEqual(args.project_name, "doxturbo")
        self.assertEqual(args.project_path, "~/com/doxturbo")

        # Project without path
        args = parser.parse_args(["sync", "project", "doxturbo"])
        self.assertEqual(args.sync_target, "project")
        self.assertEqual(args.project_name, "doxturbo")
        self.assertIsNone(args.project_path)

        args = parser.parse_args(["sync", "project", "doxturbo", "--dry-run"])
        self.assertTrue(args.dry_run)

        # MCP
        args = parser.parse_args(["sync", "mcp", "--dry-run", "--force"])
        self.assertEqual(args.sync_target, "mcp")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.force)

        # Subagents (and alias subagent)
        args = parser.parse_args(
            [
                "sync",
                "subagents",
                "--dry-run",
                "--prune",
                "--force",
                "claude-code/verifier",
            ]
        )
        self.assertEqual(args.sync_target, "subagents")
        self.assertTrue(args.dry_run)
        self.assertTrue(args.prune)
        self.assertEqual(args.force, ["claude-code/verifier"])

        args_alias = parser.parse_args(["sync", "subagent"])
        self.assertEqual(args_alias.sync_target, "subagent")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_subagent_sync)


class MaintainMemoryParserTest(unittest.TestCase):
    def test_maintain_memory_defaults_to_current_project_and_codex(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["maintain", "memory"])

        self.assertEqual(args.maintain_target, "memory")
        self.assertEqual(args.target, ".")
        self.assertEqual(args.agent, "codex")
        self.assertEqual(args.func, AIKITO_CLI.cmd_maintain_memory)

    def test_maintain_memory_accepts_scope_and_agent(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["maintain", "memory", "global", "--agent", "custom"])

        self.assertEqual(args.target, "global")
        self.assertEqual(args.agent, "custom")


class ShowProjectParserTest(unittest.TestCase):
    def test_show_project_and_projects_alias(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args = parser.parse_args(["show", "project", "demo"])
        self.assertEqual(args.show_target, "project")
        self.assertEqual(args.target, "demo")
        self.assertEqual(args.func, AIKITO_CLI.cmd_show_project)

        alias = parser.parse_args(["show", "projects"])
        self.assertEqual(alias.show_target, "projects")
        self.assertIsNone(alias.target)
        self.assertEqual(alias.func, AIKITO_CLI.cmd_show_project)


class ProjectSyncSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.project = self.root / "project"
        self.project.mkdir()
        AIKITO_CLI.init_workspace(self.workspace, self.root)
        self.assertEqual(
            AIKITO_CLI.init_project(self.workspace, self.project, "example"),
            "example",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run_sync(self, *extra_args: str) -> None:
        args = AIKITO_CLI.build_parser().parse_args(
            ["sync", "project", "example", str(self.project), *extra_args]
        )
        with patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.workspace):
            args.func(args)

    def test_rejects_unmanaged_runtime_entries_without_deleting_them(self) -> None:
        unmanaged = self.project / ".agents" / "skills" / "keep-me"
        unmanaged.mkdir(parents=True)
        (unmanaged / "data.txt").write_text("keep\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            self._run_sync()

        self.assertEqual((unmanaged / "data.txt").read_text(encoding="utf-8"), "keep\n")

    def test_dry_run_does_not_create_runtime_or_persist_path(self) -> None:
        runtime = self.project / ".agents"
        self.assertFalse(runtime.exists())
        config = self.workspace / "projects" / "example" / "agent.toml"
        original_config = config.read_text(encoding="utf-8")

        self._run_sync("--dry-run")

        self.assertFalse(runtime.exists())
        self.assertEqual(config.read_text(encoding="utf-8"), original_config)

    def test_copy_mode_refuses_drift_unless_forced(self) -> None:
        canonical = self.workspace / "skills" / "example-skill"
        canonical.mkdir()
        (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")
        config = self.workspace / "projects" / "example" / "agent.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            .replace('sync_mode = "link"', 'sync_mode = "copy"')
            .replace("skills = []", 'skills = ["example-skill"]'),
            encoding="utf-8",
        )
        self._run_sync()
        runtime_file = (
            self.project / ".agents" / "skills" / "example-skill" / "SKILL.md"
        )
        runtime_file.write_text("collaborator change\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            self._run_sync()
        self.assertEqual(
            runtime_file.read_text(encoding="utf-8"), "collaborator change\n"
        )

        self._run_sync("--force")
        self.assertEqual(runtime_file.read_text(encoding="utf-8"), "canonical\n")

    def test_deselected_managed_link_is_cleaned_and_previewed(self) -> None:
        canonical = self.workspace / "skills" / "example-skill"
        canonical.mkdir()
        (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")
        config = self.workspace / "projects" / "example" / "agent.toml"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                "skills = []", 'skills = ["example-skill"]'
            ),
            encoding="utf-8",
        )
        self._run_sync()
        runtime = self.project / ".agents" / "skills" / "example-skill"
        self.assertTrue(runtime.is_symlink())
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'skills = ["example-skill"]', "skills = []"
            ),
            encoding="utf-8",
        )

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self._run_sync("--dry-run")
        self.assertTrue(runtime.is_symlink())
        self.assertIn("[DRY RUN CLEANUP]", stdout.getvalue())

        self._run_sync()
        self.assertFalse(runtime.exists())

    def test_deselected_unmodified_copy_is_cleaned(self) -> None:
        canonical = self.workspace / "skills" / "example-skill"
        canonical.mkdir()
        (canonical / "SKILL.md").write_text("canonical\n", encoding="utf-8")
        config = self.workspace / "projects" / "example" / "agent.toml"
        config.write_text(
            config.read_text(encoding="utf-8")
            .replace('sync_mode = "link"', 'sync_mode = "copy"')
            .replace("skills = []", 'skills = ["example-skill"]'),
            encoding="utf-8",
        )
        self._run_sync()
        runtime = self.project / ".agents" / "skills" / "example-skill"
        config.write_text(
            config.read_text(encoding="utf-8").replace(
                'skills = ["example-skill"]', "skills = []"
            ),
            encoding="utf-8",
        )

        self._run_sync()
        self.assertFalse(runtime.exists())


class GlobalSyncSafetyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.runtime = self.root / ".agents" / "skills"
        (self.workspace / "skills" / "stale").mkdir(parents=True)
        (self.workspace / "skills" / "stale" / "SKILL.md").write_text(
            "managed\n", encoding="utf-8"
        )
        (self.workspace / "global").mkdir()
        (self.workspace / "global" / "AGENTS.md").write_text("", encoding="utf-8")
        (self.workspace / "skills.toml").write_text("skills = []\n", encoding="utf-8")
        self.runtime.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def _run_sync(self, *extra_args: str) -> None:
        args = AIKITO_CLI.build_parser().parse_args(["sync", "global", *extra_args])
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.workspace),
            patch.object(
                AIKITO_CLI, "get_agents_dir", return_value=self.root / ".agents"
            ),
            patch.object(AIKITO_CLI, "load_agents", return_value={}),
        ):
            args.func(args)

    def test_cleans_only_managed_stale_global_skills(self) -> None:
        stale = self.runtime / "stale"
        stale.symlink_to(self.workspace / "skills" / "stale")

        with patch("sys.stdout", new_callable=io.StringIO) as stdout:
            self._run_sync("--dry-run")
        self.assertTrue(stale.is_symlink())
        self.assertIn("[DRY RUN CLEANUP]", stdout.getvalue())

        self._run_sync()
        self.assertFalse(stale.exists())

    def test_rejects_unmanaged_stale_global_skill(self) -> None:
        unmanaged = self.runtime / "unmanaged"
        unmanaged.mkdir()
        (unmanaged / "data.txt").write_text("keep\n", encoding="utf-8")

        with self.assertRaises(SystemExit):
            self._run_sync()
        self.assertEqual((unmanaged / "data.txt").read_text(encoding="utf-8"), "keep\n")

    def test_rejects_selected_global_skill_with_unmanaged_content(self) -> None:
        selected = self.runtime / "stale"
        selected.mkdir()
        (selected / "SKILL.md").write_text("local\n", encoding="utf-8")
        (self.workspace / "skills.toml").write_text(
            'skills = ["stale"]\n', encoding="utf-8"
        )

        with self.assertRaises(SystemExit):
            self._run_sync()
        self.assertEqual((selected / "SKILL.md").read_text(encoding="utf-8"), "local\n")


class InitSubcommandParserTest(unittest.TestCase):
    def test_init_workspace_and_project_subcommands(self) -> None:
        parser = AIKITO_CLI.build_parser()

        workspace_args = parser.parse_args(["init", "workspace", "~/aikito"])
        self.assertEqual(workspace_args.init_target, "workspace")
        self.assertEqual(workspace_args.workspace_path, "~/aikito")
        self.assertEqual(workspace_args.func, AIKITO_CLI.cmd_init)

        project_args = parser.parse_args(["init", "project"])
        self.assertEqual(project_args.init_target, "project")
        self.assertIsNone(project_args.project_name)
        self.assertIsNone(project_args.project_path)
        self.assertEqual(project_args.func, AIKITO_CLI.cmd_init_project)

        explicit_args = parser.parse_args(
            ["init", "project", "example", "~/code/example"]
        )
        self.assertEqual(explicit_args.project_name, "example")
        self.assertEqual(explicit_args.project_path, "~/code/example")

    def test_legacy_init_syntax_is_rejected(self) -> None:
        parser = AIKITO_CLI.build_parser()

        with self.assertRaises(SystemExit):
            parser.parse_args(["init", "~/aikito"])

    def test_path_workspace_subcommand(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["path", "workspace"])

        self.assertEqual(args.path_target, "workspace")
        self.assertEqual(args.func, AIKITO_CLI.cmd_path_workspace)

    def test_init_project_command_creates_runtime_and_is_idempotent(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            workspace = root / "workspace"
            project = root / "example"
            project.mkdir()
            AIKITO_CLI.init_workspace(workspace, root)

            args = AIKITO_CLI.build_parser().parse_args(["init", "project"])
            with (
                patch.object(AIKITO_CLI, "get_aikito_dir", return_value=workspace),
                patch.object(AIKITO_CLI.Path, "cwd", return_value=project),
            ):
                args.func(args)
                args.func(args)

            canonical = workspace / "projects" / "example"
            runtime = project / ".agents"
            self.assertEqual(
                (runtime / "AGENTS.md").resolve(),
                (canonical / "AGENTS.md").resolve(),
            )
            self.assertEqual(
                (runtime / "memory" / "index.md").resolve(),
                (canonical / "memory" / "index.md").resolve(),
            )


class AuthSubcommandParserTest(unittest.TestCase):
    def test_auth_mcp_subcommand(self) -> None:
        parser = AIKITO_CLI.build_parser()

        args = parser.parse_args(["auth", "mcp", "opencode", "atlassian-rovo"])
        self.assertEqual(args.command, "auth")
        self.assertEqual(args.auth_target, "mcp")
        self.assertEqual(args.agent, "opencode")
        self.assertEqual(args.server, "atlassian-rovo")
        self.assertEqual(args.func, AIKITO_CLI.cmd_mcp_auth)


class ShowMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "projects" / "doxturbo" / "memory" / "notes").mkdir(
            parents=True
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_memory_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["show", "memory", "simplified-clean-architecture"])
        self.assertEqual(args.command, "show")
        self.assertEqual(args.show_target, "memory")
        self.assertEqual(args.target, "simplified-clean-architecture")
        self.assertEqual(args.func, AIKITO_CLI.cmd_show_memory)

    def test_show_memory_unique_match(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "unique-note.md"
        global_note.write_text("# Unique Note Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "unique-note"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Unique Note Content")

    def test_show_memory_unique_prefix_match(self) -> None:
        note = (
            self.aikito_dir
            / "projects"
            / "doxturbo"
            / "memory"
            / "notes"
            / "long-running-tasks-retry-strategy.md"
        )
        note.write_text("# Retry Strategy")

        for target in ("long-running-tasks", "long-running-tasks-retr…"):
            with (
                self.subTest(target=target),
                patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
                patch.object(
                    AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir
                ),
            ):
                args = AIKITO_CLI.build_parser().parse_args(["show", "memory", target])
                args.func(args)
                self.assertEqual(mock_stdout.getvalue(), "# Retry Strategy")

    def test_show_memory_exact_match_wins_over_prefix_matches(self) -> None:
        exact_note = self.aikito_dir / "memory" / "notes" / "release.md"
        exact_note.write_text("# Release")
        longer_note = self.aikito_dir / "memory" / "notes" / "release-checklist.md"
        longer_note.write_text("# Release Checklist")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release"])
            args.func(args)

        self.assertEqual(mock_stdout.getvalue(), "# Release")

    def test_show_memory_ambiguous_prefix_requires_disambiguation(self) -> None:
        (self.aikito_dir / "memory" / "notes" / "release-checklist.md").write_text(
            "# Checklist"
        )
        (self.aikito_dir / "memory" / "notes" / "release-process.md").write_text(
            "# Process"
        )

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release-"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/release-checklist", error_message)
        self.assertIn("global/notes/release-process", error_message)

    def test_show_memory_conflict_and_disambiguation(self) -> None:
        g_note = self.aikito_dir / "memory" / "notes" / "conflict.md"
        g_note.write_text("# Global Conflict Content")
        p_note = (
            self.aikito_dir
            / "projects"
            / "doxturbo"
            / "memory"
            / "notes"
            / "conflict.md"
        )
        p_note.write_text("# Project Conflict Content")

        # 1. Stem conflict -> exits with code 1 and prints candidates & disambiguation command to stderr
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "conflict"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/conflict", error_message)
        self.assertIn("doxturbo/notes/conflict", error_message)
        self.assertIn("aikito show memory global/notes/conflict", error_message)
        self.assertIn("aikito show memory doxturbo/notes/conflict", error_message)

        # 2. Disambiguated by scope/type/stem -> prints exact content
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "global/notes/conflict"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Global Conflict Content")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "doxturbo/notes/conflict"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Project Conflict Content")

    def test_show_memory_same_scope_conflict(self) -> None:
        (self.aikito_dir / "memory" / "notes" / "release-checklist.md").write_text(
            "# Checklist"
        )
        (self.aikito_dir / "memory" / "notes" / "release-process.md").write_text(
            "# Process"
        )

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "release"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", error_message)
        self.assertIn("global/notes/release-checklist", error_message)
        self.assertIn("global/notes/release-process", error_message)

    def test_show_memory_not_found(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "memory", "nonexistent"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        self.assertIn(
            "[ERROR] Memory note 'nonexistent' not found.", mock_stderr.getvalue()
        )
        self.assertIn(
            "Run 'aikito show memory' to view available memory files.",
            mock_stderr.getvalue(),
        )

    def test_show_memory_omitted_target_renders_notes_table(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "global-note.md"
        global_note.write_text("# Global Note")

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("global-note", output)
            self.assertIn("Scope", output)
            self.assertIn("Note File", output)


class ShowSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "skills").mkdir(parents=True)

        (self.aikito_dir / "skills.toml").write_text(
            'skills = ["durable-memory", "agent-browser"]\n'
        )

        g_skill = self.aikito_dir / "skills" / "durable-memory"
        g_skill.mkdir()
        (g_skill / "SKILL.md").write_text("# Durable Memory Content")

        b_skill = self.aikito_dir / "skills" / "agent-browser"
        b_skill.mkdir()
        (b_skill / "SKILL.md").write_text("# Agent Browser Content")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_skill_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["show", "skill", "durable-memory"])
        self.assertEqual(args.command, "show")
        self.assertEqual(args.show_target, "skill")
        self.assertEqual(args.target, "durable-memory")
        self.assertEqual(args.func, AIKITO_CLI.cmd_show_skill)

        args_alias = parser.parse_args(["show", "skills", "dur"])
        self.assertEqual(args_alias.show_target, "skills")
        self.assertEqual(args_alias.target, "dur")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_show_skill)

    def test_show_skill_unique_match(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "durable-memory"]
            )
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Durable Memory Content")

    def test_show_skill_unique_prefix_match(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "skill", "dur"])
            args.func(args)
            self.assertEqual(mock_stdout.getvalue(), "# Durable Memory Content")

    def test_show_skill_ambiguous_prefix_conflict(self) -> None:
        s1 = self.aikito_dir / "skills" / "gino-code-review"
        s1.mkdir()
        (s1 / "SKILL.md").write_text("s1")
        s2 = self.aikito_dir / "skills" / "gino-jira"
        s2.mkdir()
        (s2 / "SKILL.md").write_text("s2")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "skill", "gino"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", err)
        self.assertIn("gino-code-review", err)
        self.assertIn("gino-jira", err)

    def test_show_skill_not_found(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "nonexistent"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Skill 'nonexistent' not found.", mock_stderr.getvalue())


class EditMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_memory_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["edit", "memory", "test-note"])
        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "memory")
        self.assertEqual(args.target, "test-note")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_memory)

    def test_edit_memory_invokes_editor_with_shlex(self) -> None:
        global_note = self.aikito_dir / "memory" / "notes" / "edit-note.md"
        global_note.write_text("# Edit Note")

        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"EDITOR": "code --wait"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "memory", "edit-note"])
            args.func(args)
            mock_run.assert_called_once_with(["code", "--wait", str(global_note)])

    def test_edit_memory_conflict_suggests_edit_command(self) -> None:
        project_notes = self.aikito_dir / "projects" / "doxturbo" / "memory" / "notes"
        project_notes.mkdir(parents=True)
        (self.aikito_dir / "memory" / "notes" / "conflict.md").write_text(
            "# Global Conflict"
        )
        (project_notes / "conflict.md").write_text("# Project Conflict")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "memory", "conflict"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        error_message = mock_stderr.getvalue()
        self.assertIn("aikito edit memory global/notes/conflict", error_message)
        self.assertIn("aikito edit memory doxturbo/notes/conflict", error_message)
        self.assertNotIn("aikito show memory", error_message)

    def test_path_escape_prevention(self) -> None:
        outside_dir = Path(self.temporary_directory.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "secret.md"
        outside_file.write_text("secret")

        # Create a symlink in memory pointing outside
        bad_symlink = self.aikito_dir / "memory" / "notes" / "escaped.md"
        bad_symlink.symlink_to(outside_file)

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "memory", "escaped"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("[ERROR] Path escape detected", mock_stderr.getvalue())


class EditInstructionsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        self.home = self.aikito_dir / "home"
        self.project_path = self.aikito_dir / "project-code"
        project_dir = self.aikito_dir / "projects" / "doxturbo"
        project_dir.mkdir(parents=True)
        self.instructions_file = project_dir / "AGENTS.md"
        self.instructions_file.write_text("", encoding="utf-8")
        (project_dir / "agent.toml").write_text(
            f'name = "doxturbo"\npath = "{self.project_path}"\n',
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_instructions_parser(self) -> None:
        args = AIKITO_CLI.build_parser().parse_args(
            ["edit", "instructions", "doxturbo"]
        )

        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "instructions")
        self.assertEqual(args.target, "doxturbo")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_instructions)

    def test_edit_instructions_invokes_editor(self) -> None:
        mock_proc = MagicMock(returncode=0)
        with (
            patch("os.environ", {"EDITOR": "code --wait"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["edit", "instructions", "doxturbo"]
            )
            args.func(args)

        mock_run.assert_called_once_with(
            ["code", "--wait", str(self.instructions_file)]
        )


class EditSkillTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        (self.aikito_dir / "skills").mkdir(parents=True)
        (self.aikito_dir / "skills.toml").write_text('skills = ["durable-memory"]\n')

        skill_dir = self.aikito_dir / "skills" / "durable-memory"
        skill_dir.mkdir()
        self.skill_file = skill_dir / "SKILL.md"
        self.skill_file.write_text("# Durable Memory")

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_edit_skill_parser(self) -> None:
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(["edit", "skill", "durable-memory"])
        self.assertEqual(args.command, "edit")
        self.assertEqual(args.edit_target, "skill")
        self.assertEqual(args.target, "durable-memory")
        self.assertEqual(args.func, AIKITO_CLI.cmd_edit_skill)

        args_alias = parser.parse_args(["edit", "skills", "dur"])
        self.assertEqual(args_alias.edit_target, "skills")
        self.assertEqual(args_alias.target, "dur")
        self.assertEqual(args_alias.func, AIKITO_CLI.cmd_edit_skill)

    def test_edit_skill_invokes_editor(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"EDITOR": "nano"}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "dur"])
            args.func(args)
            mock_run.assert_called_once_with(["nano", str(self.skill_file)])

    def test_edit_skill_whitespace_editor_fallback_to_vi(self) -> None:
        mock_proc = MagicMock()
        mock_proc.returncode = 0

        with (
            patch("os.environ", {"VISUAL": "   ", "EDITOR": "   "}),
            patch("subprocess.run", return_value=mock_proc) as mock_run,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "dur"])
            args.func(args)
            mock_run.assert_called_once_with(["vi", str(self.skill_file)])

    def test_edit_skill_conflict_suggests_edit_command(self) -> None:
        s1 = self.aikito_dir / "skills" / "gino-code-review"
        s1.mkdir()
        (s1 / "SKILL.md").write_text("s1")
        s2 = self.aikito_dir / "skills" / "gino-jira"
        s2.mkdir()
        (s2 / "SKILL.md").write_text("s2")

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "skill", "gino"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)

        self.assertEqual(cm.exception.code, 1)
        err = mock_stderr.getvalue()
        self.assertIn("[CONFLICT]", err)
        self.assertIn("aikito edit skill gino-code-review", err)
        self.assertIn("aikito edit skill gino-jira", err)

    def test_skill_path_escape_prevention(self) -> None:
        outside_dir = Path(self.temporary_directory.name) / "outside"
        outside_dir.mkdir()
        outside_file = outside_dir / "SKILL.md"
        outside_file.write_text("secret skill")

        # Create a symlink in skills pointing outside
        bad_symlink_dir = self.aikito_dir / "skills" / "escaped-skill"
        bad_symlink_dir.mkdir()
        bad_symlink_file = bad_symlink_dir / "SKILL.md"
        bad_symlink_file.symlink_to(outside_file)

        (self.aikito_dir / "skills.toml").write_text('skills = ["escaped-skill"]\n')

        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "skill", "escaped-skill"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn(
                "[ERROR] Path escape detected for skill", mock_stderr.getvalue()
            )


class ShowSubcommandsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.temporary_directory.name)
        self.home = self.aikito_dir / "home"
        (self.home / ".codex").mkdir(parents=True)
        (self.aikito_dir / "agents.toml").write_text(
            """
[agents.codex]
display_name = "Codex"
instruction_path = ".codex/AGENTS.md"

[agents.codex.subagents]
config_path = ".codex/agents"
config_format = "codex_toml"

[agents.codex.mcp]
config_path = ".codex/config.toml"
config_format = "toml"
name_style = "verbatim"
""".lstrip()
        )
        (self.aikito_dir / "mcps").mkdir(parents=True, exist_ok=True)
        (self.aikito_dir / "mcps/managed.toml").write_text(
            'transport = "remote"\nurl = "http://ex.com"\nagents = ["codex"]\n'
        )
        (self.aikito_dir / "subagents.toml").write_text(
            '[subagents.formatter]\ndescription = "Format"\nagents = ["codex"]\n'
        )
        (self.aikito_dir / "subagents").mkdir()
        (self.aikito_dir / "subagents" / "formatter.md").write_text(
            "# Formatter Instructions"
        )
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "memory" / "index.md").write_text("# Global Memory Index")
        (self.home / ".codex/config.toml").write_text(
            """
[mcp_servers.managed]
url = "http://ex.com"

[mcp_servers.custom]
url = "http://custom.example.com"
""".lstrip()
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_show_mcp(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "mcp"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("MCP Server", output)
            self.assertIn("managed", output)

    def test_show_mcp_agent_view_lists_managed_and_unmanaged(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "--agent", "codex", "--color", "never"]
            )
            args.func(args)

        output = mock_stdout.getvalue()
        self.assertIn("Agent: Codex", output)
        self.assertIn(str(self.home / ".codex/config.toml"), output)
        self.assertIn("1 managed", output)
        self.assertIn("1 unmanaged", output)
        self.assertIn("custom", output)

    def test_show_mcp_intersection_displays_managed_entry(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "man", "--agent", "codex"]
            )
            args.func(args)

        output = mock_stdout.getvalue()
        self.assertIn("MCP Server: managed", output)
        self.assertEqual(output.count("MCP Server: managed"), 1)
        self.assertIn("| Codex", output)
        self.assertNotIn("Agent: Codex", output)
        self.assertIn("Managed entry:", output)
        self.assertIn('"url": "http://ex.com"', output)

    def test_show_subagents(self) -> None:
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "subagents"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Subagent", output)
            self.assertIn("formatter", output)

        # Show specific target
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagent", "formatter"]
            )
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertEqual(output, "# Formatter Instructions")

        # Show target with prefix
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "subagent", "form"])
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertEqual(output, "# Formatter Instructions")

        # Show nonexistent target
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "subagent", "unknown"])
            args.func(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("Subagent 'unknown' not found", mock_stderr.getvalue())

    def test_show_subagents_with_agent_flag(self) -> None:
        # Update subagents.toml with platform config override
        (self.aikito_dir / "subagents.toml").write_text(
            '[subagents.formatter]\ndescription = "Format code"\nagents = ["codex"]\n\n'
            '[subagents.formatter.codex]\nmodel = "gpt-5"\n'
        )

        # 1. show subagents --agent codex (Agent table view)
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "--agent", "codex", "--no-color"]
            )
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Agent: Codex", output)
            self.assertIn("Agent key: codex", output)
            self.assertIn("formatter", output)
            self.assertIn("model: gpt-5", output)
            self.assertIn("Format code", output)

        # 2. show subagents formatter --agent (Detail view across agents)
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "formatter", "--agent", "--no-color"]
            )
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Subagent: formatter", output)
            self.assertIn("Description: Format code", output)
            self.assertIn("Codex", output)
            self.assertIn("Agent key: codex", output)
            self.assertIn("Platform options:", output)
            self.assertIn("model: gpt-5", output)

        # 3. show subagents formatter --agent codex (Detail view single agent)
        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "formatter", "--agent", "codex", "--no-color"]
            )
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Subagent: formatter", output)
            self.assertIn("Agent key: codex", output)
            self.assertIn("model: gpt-5", output)

        # 4. Unknown agent error handling (no traceback)
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "--agent", "nosuch"]
            )
            args.func(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Unknown agent 'nosuch'", mock_stderr.getvalue())

        # 5. Unknown subagent with --agent error handling (no traceback)
        with (
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            self.assertRaises(SystemExit) as cm,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "nosuchsub", "--agent"]
            )
            args.func(args)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("[ERROR] Unknown subagent 'nosuchsub'", mock_stderr.getvalue())

        # 6. Non-targeted agent for subagent
        # Add another agent to agents.toml
        agents_toml = self.aikito_dir / "agents.toml"
        content = (
            agents_toml.read_text()
            + "\n[agents.other]\ndisplay_name = 'Other Agent'\n[agents.other.subagents]\nconfig_path = '.other/agents'\nconfig_format = 'claude_markdown'\n"
        )
        agents_toml.write_text(content)

        with (
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "subagents", "formatter", "--agent", "other", "--no-color"]
            )
            args.func(args)
            output = mock_stdout.getvalue()
            self.assertIn("Subagent: formatter", output)
            self.assertIn("Status: not targeted by this subagent", output)
            self.assertIn("n/a (not targeted by this subagent)", output)

    def test_edit_subagent(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(AIKITO_CLI, "open_in_editor") as mock_edit,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["edit", "subagent", "formatter"]
            )
            args.func(args)
            mock_edit.assert_called_once_with(
                self.aikito_dir / "subagents" / "formatter.md"
            )

    def test_show_instructions_lists_and_reads_targets(self) -> None:
        project_path = self.aikito_dir / "project-code"
        project_dir = self.aikito_dir / "projects" / "example"
        project_dir.mkdir(parents=True)
        (self.aikito_dir / "global").mkdir()
        (self.aikito_dir / "global" / "AGENTS.md").write_text("global rules\n")
        (self.home / ".codex" / "AGENTS.md").symlink_to(
            self.aikito_dir / "global" / "AGENTS.md"
        )
        (project_dir / "AGENTS.md").write_text("project rules\n")
        (project_dir / "agent.toml").write_text(
            f'name = "example"\npath = "{project_path}"\n'
        )
        (project_path / ".agents").mkdir(parents=True)
        (project_path / ".agents" / "AGENTS.md").symlink_to(project_dir / "AGENTS.md")
        empty_project_dir = self.aikito_dir / "projects" / "empty"
        empty_project_dir.mkdir()
        (empty_project_dir / "AGENTS.md").write_text("\n")
        (empty_project_dir / "agent.toml").write_text(
            f'name = "empty"\npath = "{self.aikito_dir / "empty-code"}"\n'
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "--color", "always", "--no-color"]
            )
            args.func(args)
        listing = mock_stdout.getvalue()
        self.assertIn("╭", listing)
        self.assertIn("│ Codex", listing)
        self.assertIn("Status: linked", listing)
        self.assertIn("Target: ~/.codex/AGENTS.md", listing)
        self.assertIn("│ Projects", listing)
        self.assertIn("example: linked", listing)
        self.assertIn("empty: -", listing)

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "global"]
            )
            args.func(args)
        self.assertEqual(mock_stdout.getvalue(), "global rules\n")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "instructions", "example"]
            )
            args.func(args)
        self.assertEqual(mock_stdout.getvalue(), "project rules\n")

    def test_show_instructions_dot_resolves_current_project(self) -> None:
        project_path = self.aikito_dir / "project-code"
        nested_path = project_path / "src"
        project_dir = self.aikito_dir / "projects" / "example"
        nested_path.mkdir(parents=True)
        project_dir.mkdir(parents=True)
        (project_dir / "AGENTS.md").write_text("project rules\n")
        (project_dir / "agent.toml").write_text(
            f'name = "example"\npath = "{project_path}"\n'
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch.object(Path, "cwd", return_value=nested_path),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "instructions", "."])
            args.func(args)

        self.assertEqual(mock_stdout.getvalue(), "project rules\n")
        self.assertIn("Also active: global instructions", mock_stderr.getvalue())

    def test_status_has_no_subcommands(self) -> None:
        with (
            patch("sys.stderr", new_callable=io.StringIO),
        ):
            with self.assertRaises(SystemExit) as cm:
                AIKITO_CLI.build_parser().parse_args(["status", "mcp"])
            self.assertEqual(cm.exception.code, 2)

    def test_resolve_color_flags(self) -> None:
        parser = AIKITO_CLI.build_parser()

        with patch.dict(AIKITO_CLI.os.environ, {"NO_COLOR": ""}):
            args = parser.parse_args(["show", "mcp", "--color", "always"])
            use_unicode, use_color = AIKITO_CLI.resolve_color_flags(args)
            self.assertTrue(use_unicode)
            self.assertTrue(use_color)

            args_no_color = parser.parse_args(
                ["show", "mcp", "--color", "always", "--no-color"]
            )
            use_unicode, use_color = AIKITO_CLI.resolve_color_flags(args_no_color)
            self.assertTrue(use_unicode)
            self.assertFalse(use_color)

    def test_show_mcp_raw_content(self) -> None:
        mcps_dir = self.aikito_dir / "mcps"
        mcps_dir.mkdir(parents=True, exist_ok=True)
        (mcps_dir / "atlassian-rovo.toml").write_text(
            'transport = "remote"\nurl = "https://example.com"\n',
            encoding="utf-8",
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "atlassian-rovo"]
            )
            args.func(args)

        self.assertEqual(
            mock_stdout.getvalue(),
            'transport = "remote"\nurl = "https://example.com"\n',
        )

    def test_show_mcp_prefix_raw_content(self) -> None:
        mcps_dir = self.aikito_dir / "mcps"
        mcps_dir.mkdir(parents=True, exist_ok=True)
        (mcps_dir / "atlassian-rovo.toml").write_text(
            'transport = "remote"\nurl = "https://example.com"\n',
            encoding="utf-8",
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["show", "mcp", "atl"])
            args.func(args)

        self.assertEqual(
            mock_stdout.getvalue(),
            'transport = "remote"\nurl = "https://example.com"\n',
        )

    def test_show_mcp_detail_view(self) -> None:
        mcps_dir = self.aikito_dir / "mcps"
        mcps_dir.mkdir(parents=True, exist_ok=True)
        (mcps_dir / "atlassian-rovo.toml").write_text(
            'transport = "remote"\nurl = "https://example.com"\nagents = ["codex"]\n',
            encoding="utf-8",
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["show", "mcp", "atlassian-rovo", "--agent"]
            )
            args.func(args)

        self.assertIn("MCP Server: atlassian-rovo", mock_stdout.getvalue())
        self.assertIn("Canonical source:", mock_stdout.getvalue())

    def test_edit_mcp_command_opens_target_in_editor(self) -> None:
        mcps_dir = self.aikito_dir / "mcps"
        mcps_dir.mkdir(parents=True, exist_ok=True)
        mcp_file = mcps_dir / "atlassian-rovo.toml"
        mcp_file.write_text('url = "https://example.com"\n', encoding="utf-8")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(AIKITO_CLI, "open_in_editor") as mock_open,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["edit", "mcp", "atl"])
            args.func(args)

        mock_open.assert_called_once_with(mcp_file)

    def test_version_matches_latest_changelog_release(self) -> None:
        changelog_path = ROOT / "CHANGELOG.md"
        if not changelog_path.is_file():
            return
        content = changelog_path.read_text(encoding="utf-8")
        match = re.search(r"(?m)^## \[(\d+\.\d+\.\d+)\]", content)
        self.assertIsNotNone(
            match, "Could not find a release version header in CHANGELOG.md"
        )
        assert match is not None
        latest_changelog_version = match.group(1)
        self.assertEqual(
            AIKITO_CLI.__version__,
            latest_changelog_version,
            f"bin/aikito __version__ ({AIKITO_CLI.__version__}) does not match latest CHANGELOG.md release ({latest_changelog_version})",
        )

    def test_add_requires_subcommand(self) -> None:
        with patch("sys.stderr", new_callable=io.StringIO):
            with self.assertRaises(SystemExit) as cm:
                AIKITO_CLI.build_parser().parse_args(["add"])
            self.assertEqual(cm.exception.code, 2)

    def test_add_skill_cli(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["add", "skill", "cli-skill", "--description", "CLI Skill description"]
            )
            args.func(args)

        self.assertTrue(
            (self.aikito_dir / "skills" / "cli-skill" / "SKILL.md").is_file()
        )
        self.assertIn(
            "[SUCCESS] Added global skill 'cli-skill'.", mock_stdout.getvalue()
        )

    def test_add_subagent_cli(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                [
                    "add",
                    "subagent",
                    "cli-subagent",
                    "--description",
                    "CLI Subagent desc",
                    "--agents",
                    "codex,claude-code",
                ]
            )
            args.func(args)

        self.assertTrue((self.aikito_dir / "subagents" / "cli-subagent.md").is_file())
        self.assertIn(
            "[SUCCESS] Added subagent 'cli-subagent'.", mock_stdout.getvalue()
        )

    def test_add_mcp_cli(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch.object(Path, "home", return_value=self.home),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                [
                    "add",
                    "mcp",
                    "cli-mcp",
                    "--url",
                    "https://example.com/mcp",
                    "--agents",
                    "agy,codex",
                ]
            )
            args.func(args)

        self.assertTrue((self.aikito_dir / "mcps" / "cli-mcp.toml").is_file())
        self.assertIn("[SUCCESS] Added MCP server 'cli-mcp'.", mock_stdout.getvalue())


class TestMemoryRenameAndRemove(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.tmp.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "memory" / "index.md").write_text(
            "- [[note-a|Note A Title]]\n"
        )
        (self.aikito_dir / "memory" / "notes" / "note-a.md").write_text(
            "# Note A Title\nContent"
        )
        (self.aikito_dir / "memory" / "notes" / "note-b.md").write_text(
            "# Note B\nLinks to [[note-a]] and [[note-a|Custom Text]]."
        )
        (self.aikito_dir / "memory" / "index.md").write_text(
            "- [[note-a|Note A Title]]\n- [[note-b|Note B]]\n"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_rename_memory_success(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "note-a", "note-alpha"]
            )
            args.func(args)

        self.assertFalse((self.aikito_dir / "memory" / "notes" / "note-a.md").exists())
        self.assertTrue(
            (self.aikito_dir / "memory" / "notes" / "note-alpha.md").exists()
        )

        # Check index.md updated
        index_text = (self.aikito_dir / "memory" / "index.md").read_text()
        self.assertIn("- [[note-alpha|Note A Title]]", index_text)
        self.assertNotIn("[[note-a|", index_text)
        self.assertNotIn("[[note-a]]", index_text)

        # Check note-b inbound references updated
        note_b_text = (self.aikito_dir / "memory" / "notes" / "note-b.md").read_text()
        self.assertIn("[[note-alpha]]", note_b_text)
        self.assertIn("[[note-alpha|Custom Text]]", note_b_text)
        self.assertNotIn("[[note-a]]", note_b_text)
        self.assertNotIn("[[note-a|", note_b_text)

        out = mock_stdout.getvalue()
        self.assertIn("[OK] Renamed memory note 'note-a' → 'note-alpha'", out)
        self.assertIn("Updated inbound wikilinks in 1 file(s)", out)

    def test_rename_memory_invalid_name(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "note-a", "Invalid_Name"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Must be kebab-case", mock_stderr.getvalue())

    def test_rename_memory_already_exists(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "note-a", "note-b"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("already exists", mock_stderr.getvalue())

    def test_rm_memory_success_and_warn_inbound(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "memory", "note-a"])
            args.func(args)

        self.assertFalse((self.aikito_dir / "memory" / "notes" / "note-a.md").exists())
        index_text = (self.aikito_dir / "memory" / "index.md").read_text()
        self.assertNotIn("[[note-a", index_text)
        self.assertIn("- [[note-b|Note B]]", index_text)

        out = mock_stdout.getvalue()
        self.assertIn("[OK] Removed memory note 'note-a'", out)
        self.assertIn("[WARN] 1 inbound reference(s) still exist", out)

    def test_remove_memory_alias(self) -> None:
        (self.aikito_dir / "memory" / "notes" / "solo-note.md").write_text("# Solo")
        (self.aikito_dir / "memory" / "index.md").write_text("- [[solo-note|Solo]]\n")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["remove", "memory", "solo-note"]
            )
            args.func(args)

        self.assertFalse(
            (self.aikito_dir / "memory" / "notes" / "solo-note.md").exists()
        )
        out = mock_stdout.getvalue()
        self.assertIn("[OK] Removed memory note 'solo-note'", out)
        self.assertIn("No inbound references found", out)

    def test_rename_index_file_rejected(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "index", "new-index"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Cannot rename 'index.md'", mock_stderr.getvalue())

    def test_rm_index_file_rejected(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "memory", "index"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("Cannot remove 'index.md'", mock_stderr.getvalue())
        self.assertTrue((self.aikito_dir / "memory" / "index.md").exists())

    def test_rename_memory_conflict_handling(self) -> None:
        proj_notes = self.aikito_dir / "projects" / "p1" / "memory" / "notes"
        proj_notes.mkdir(parents=True)
        (proj_notes / "note-a.md").write_text("# Proj Note A")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "note-a", "new-name"]
            )
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            err = mock_stderr.getvalue()
            self.assertIn("[CONFLICT] Multiple memory notes match 'note-a'", err)
            self.assertIn("global/notes/note-a", err)
            self.assertIn("p1/notes/note-a", err)
            self.assertIn("aikito rename memory global/notes/note-a", err)

    def test_rm_memory_conflict_handling(self) -> None:
        proj_notes = self.aikito_dir / "projects" / "p1" / "memory" / "notes"
        proj_notes.mkdir(parents=True)
        (proj_notes / "note-a.md").write_text("# Proj Note A")

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stderr", new_callable=io.StringIO) as mock_stderr,
        ):
            args = AIKITO_CLI.build_parser().parse_args(["rm", "memory", "note-a"])
            with self.assertRaises(SystemExit) as cm:
                args.func(args)
            self.assertEqual(cm.exception.code, 1)
            err = mock_stderr.getvalue()
            self.assertIn("[CONFLICT] Multiple memory notes match 'note-a'", err)
            self.assertIn("global/notes/note-a", err)
            self.assertIn("p1/notes/note-a", err)
            self.assertIn("aikito rm memory global/notes/note-a", err)

    def test_rename_memory_isolated_to_same_scope(self) -> None:
        # Create demo project with its own note-a and note-other that references its own [[note-a]]
        proj_notes = self.aikito_dir / "projects" / "demo" / "memory" / "notes"
        proj_notes.mkdir(parents=True)
        (proj_notes / "note-a.md").write_text("# Demo Note A")
        (proj_notes / "proj-other.md").write_text("See [[note-a]] in demo.")
        (self.aikito_dir / "projects" / "demo" / "memory" / "index.md").write_text(
            "- [[note-a|Demo Note A]]\n- [[proj-other|Proj Other]]\n"
        )

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stdout", new_callable=io.StringIO),
        ):
            # Rename global note-a to global-note-a using full identifier
            args = AIKITO_CLI.build_parser().parse_args(
                ["rename", "memory", "global/notes/note-a", "global-note-a"]
            )
            args.func(args)

        # Global note and its inbound refs are renamed
        self.assertTrue(
            (self.aikito_dir / "memory" / "notes" / "global-note-a.md").exists()
        )
        global_note_b = (self.aikito_dir / "memory" / "notes" / "note-b.md").read_text()
        self.assertIn("[[global-note-a]]", global_note_b)

        # Demo project's note and its wikilinks MUST REMAIN UNTOUCHED
        self.assertTrue((proj_notes / "note-a.md").exists())
        demo_other = (proj_notes / "proj-other.md").read_text()
        self.assertEqual(demo_other, "See [[note-a]] in demo.")


class TestDoctorFixCli(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.tmp.name)
        (self.aikito_dir / "memory" / "notes").mkdir(parents=True)
        (self.aikito_dir / "memory" / "notes" / "bare.md").write_text(
            "# Bare Note Title\nBody"
        )
        (self.aikito_dir / "memory" / "index.md").write_text(
            "- [[ghost-note|Ghost]]\n- [[bare]] — Some Description\n"
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_doctor_fix_flag(self) -> None:
        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=self.aikito_dir),
            patch("sys.stdout", new_callable=io.StringIO) as mock_stdout,
        ):
            args = AIKITO_CLI.build_parser().parse_args(
                ["doctor", "--fix", "--no-color"]
            )
            try:
                args.func(args)
            except SystemExit:
                pass

        out = mock_stdout.getvalue()
        self.assertIn("[FIX] Removed dangling index entry [[ghost-note]]", out)
        self.assertIn("[FIX] Normalized index entry for [[bare]]", out)

        index_text = (self.aikito_dir / "memory" / "index.md").read_text()
        self.assertNotIn("ghost-note", index_text)
        self.assertIn("- [[bare|Bare Note Title]]", index_text)


if __name__ == "__main__":
    unittest.main()
