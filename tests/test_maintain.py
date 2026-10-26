import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from layout_helpers import write_agents

from aikito.maintain import (
    MemoryMaintenanceError,
    build_memory_maintenance_prompt,
    load_agent_runner,
    resolve_memory_maintenance_scope,
    run_memory_maintenance,
)

ROOT = Path(__file__).resolve().parents[1]


class MemoryMaintenanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.aikito_dir = self.root / "aikito"
        self.project_path = self.root / "code" / "example"
        self.project_memory = self.aikito_dir / "projects" / "example" / "memory"
        (self.aikito_dir / "memory").mkdir(parents=True)
        self.project_memory.mkdir(parents=True)
        self.project_path.mkdir(parents=True)
        (self.project_memory.parent / "agent.toml").write_text(
            f'name = "example"\npath = "{self.project_path.as_posix()}"\n',
            encoding="utf-8",
        )

        write_agents(
            self.aikito_dir,
            """[agents.codex.runner]
command = ["fake-agent", "{workdir}", "{prompt}"]

[agents.codex.runner.env]
HTTPS_PROXY = "http://proxy.example:1234"
RUN_SCOPE = "{scope}"

[agents.claude-code.runner]
command = ["claude", "{prompt}"]

[agents.agy.runner]
command = ["agy", "--prompt-interactive", "{prompt}"]

[agents.opencode.runner]
command = ["opencode", "{workdir}", "--prompt", "{prompt}"]

[agents.github-copilot.runner]
command = ["copilot", "-C", "{workdir}", "-i", "{prompt}"]
""",
        )

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_resolves_global_named_and_current_project_scopes(self) -> None:
        global_scope = resolve_memory_maintenance_scope(
            self.aikito_dir, "global", self.project_path
        )
        named_scope = resolve_memory_maintenance_scope(
            self.aikito_dir, "example", self.root
        )
        current_scope = resolve_memory_maintenance_scope(
            self.aikito_dir, ".", self.project_path / "nested"
        )

        self.assertEqual(
            global_scope.memory_dir, (self.aikito_dir / "memory").resolve()
        )
        self.assertEqual(named_scope, current_scope)
        self.assertEqual(named_scope.memory_dir, self.project_memory.resolve())
        self.assertEqual(named_scope.workdir, self.project_path.resolve())

    def test_rejects_unregistered_current_directory(self) -> None:
        with self.assertRaisesRegex(
            MemoryMaintenanceError, "not inside a registered project"
        ):
            resolve_memory_maintenance_scope(self.aikito_dir, ".", self.root)

    def test_resolves_from_workspace_project_directory(self) -> None:
        scope = resolve_memory_maintenance_scope(
            self.aikito_dir, ".", self.project_memory
        )
        self.assertEqual(scope.name, "example")
        self.assertEqual(scope.memory_dir, self.project_memory.resolve())
        self.assertEqual(scope.workdir, self.project_path.resolve())

    def test_rejects_conflicting_project_paths(self) -> None:
        # Create second project with same path
        p2_mem = self.aikito_dir / "projects" / "example2" / "memory"
        p2_mem.mkdir(parents=True)
        (p2_mem.parent / "agent.toml").write_text(
            f'name = "example2"\npath = "{self.project_path.as_posix()}"\n',
            encoding="utf-8",
        )

        with self.assertRaisesRegex(
            MemoryMaintenanceError, "belongs to multiple projects"
        ):
            resolve_memory_maintenance_scope(self.aikito_dir, ".", self.project_path)

    def _write_example_paths(self, *paths: Path) -> None:
        quoted = ", ".join(f'"{path.as_posix()}"' for path in paths)
        (self.project_memory.parent / "agent.toml").write_text(
            f'name = "example"\npaths = [{quoted}]\n',
            encoding="utf-8",
        )

    def test_resolves_workdir_from_any_active_path(self) -> None:
        worktree = self.root / "code" / "example-wt"
        worktree.mkdir()
        self._write_example_paths(self.project_path, worktree)

        from_worktree = resolve_memory_maintenance_scope(
            self.aikito_dir, ".", worktree / "nested"
        )
        named_from_worktree = resolve_memory_maintenance_scope(
            self.aikito_dir, "example", worktree
        )
        named_from_main = resolve_memory_maintenance_scope(
            self.aikito_dir, "example", self.project_path
        )

        self.assertEqual(from_worktree.workdir, worktree.resolve())
        self.assertEqual(named_from_worktree.workdir, worktree.resolve())
        self.assertEqual(named_from_main.workdir, self.project_path.resolve())

    def test_named_project_requires_cwd_when_multiple_active_paths(self) -> None:
        worktree = self.root / "code" / "example-wt"
        worktree.mkdir()
        self._write_example_paths(self.project_path, worktree)

        with self.assertRaisesRegex(MemoryMaintenanceError, "multiple local paths"):
            resolve_memory_maintenance_scope(self.aikito_dir, "example", self.root)

    def test_named_offline_project_is_rejected(self) -> None:
        self._write_example_paths(self.root / "gone")

        with self.assertRaisesRegex(MemoryMaintenanceError, "offline on this host"):
            resolve_memory_maintenance_scope(self.aikito_dir, "example", self.root)

    def test_named_project_without_candidates_is_rejected(self) -> None:
        (self.project_memory.parent / "agent.toml").write_text(
            'name = "example"\n',
            encoding="utf-8",
        )
        with self.assertRaisesRegex(MemoryMaintenanceError, "Project path is missing"):
            resolve_memory_maintenance_scope(self.aikito_dir, "example", self.root)

    def test_reports_registered_project_without_memory_scope(self) -> None:
        self.project_memory.rmdir()

        for target, cwd in (("example", self.root), (".", self.project_path)):
            with (
                self.subTest(target=target),
                self.assertRaisesRegex(
                    MemoryMaintenanceError,
                    "registered but has no memory scope",
                ),
            ):
                resolve_memory_maintenance_scope(self.aikito_dir, target, cwd)

    def test_loads_all_configured_runners(self) -> None:
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "codex").command,
            ("fake-agent", "{workdir}", "{prompt}"),
        )
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "claude-code").command,
            ("claude", "{prompt}"),
        )
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "agy").command,
            ("agy", "--prompt-interactive", "{prompt}"),
        )
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "opencode").command,
            ("opencode", "{workdir}", "--prompt", "{prompt}"),
        )
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "github-copilot").command,
            ("copilot", "-C", "{workdir}", "-i", "{prompt}"),
        )
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "codex").env,
            {
                "HTTPS_PROXY": "http://proxy.example:1234",
                "RUN_SCOPE": "{scope}",
            },
        )

    @patch("aikito.maintain.subprocess.run")
    def test_existing_builtin_runners_auto_approve_by_default(self, run_mock) -> None:
        run_mock.return_value.returncode = 0
        cases = {
            "codex": (
                "codex",
                ["--approve-for-me", "--add-dir", str(self.aikito_dir.resolve())],
            ),
            "claude-code": (
                "claude",
                [
                    "--add-dir",
                    str(self.aikito_dir.resolve()),
                    "--permission-mode",
                    "auto",
                ],
            ),
            "agy": (
                "agy",
                ["--mode", "auto", "--add-dir", str(self.aikito_dir.resolve())],
            ),
            "opencode": ("opencode", ["--auto"]),
            "github-copilot": ("copilot", ["--allow-all"]),
            "grok": ("grok", ["--permission-mode", "auto"]),
            "dsh": (
                "dsh",
                [
                    "--permission-mode",
                    "danger-full-access",
                    "--approval-policy",
                    "never",
                ],
            ),
            "pi": ("pi", []),
        }
        for name, (binary, flags) in cases.items():
            with self.subTest(agent=name):
                write_agents(
                    self.aikito_dir,
                    f'[agents.{name}.runner]\ncommand = ["{binary}", "{{prompt}}"]\n',
                )
                with patch("aikito.maintain.resolve_executable", side_effect=list):
                    run_memory_maintenance(self.aikito_dir, "example", name, self.root)
                command = run_mock.call_args.args[0]
                self.assertEqual(command[:-1], [binary, *flags])
                self.assertIn("until the user confirms the proposal", command[-1])
                self.assertIn("Do not push", command[-1])

    @patch("aikito.maintain.subprocess.run")
    def test_runner_can_override_or_disable_auto_approval(self, run_mock) -> None:
        run_mock.return_value.returncode = 0
        for args, expected in (
            ("[]", []),
            (
                '["--custom-auto", "{workspace}"]',
                ["--custom-auto", str(self.aikito_dir.resolve())],
            ),
        ):
            with self.subTest(args=args):
                write_agents(
                    self.aikito_dir,
                    f"""[agents.codex.runner]
command = ["codex", "{{prompt}}"]
auto_approve_args = {args}
""",
                )
                with patch("aikito.maintain.resolve_executable", side_effect=list):
                    run_memory_maintenance(
                        self.aikito_dir, "global", "codex", self.root
                    )
                self.assertEqual(run_mock.call_args.args[0][:-1], ["codex", *expected])

    def test_custom_launcher_does_not_inherit_builtin_arguments(self) -> None:
        self.assertEqual(
            load_agent_runner(self.aikito_dir, "codex").auto_approve_args, ()
        )

    def test_rejects_invalid_auto_approval_arguments(self) -> None:
        for args in ('"--auto"', "[1]", '[""]'):
            with self.subTest(args=args):
                write_agents(
                    self.aikito_dir,
                    f"""[agents.custom.runner]
command = ["custom"]
auto_approve_args = {args}
""",
                )
                with self.assertRaisesRegex(
                    MemoryMaintenanceError, "invalid runner.auto_approve_args"
                ):
                    load_agent_runner(self.aikito_dir, "custom")

    @patch("aikito.maintain.subprocess.run")
    def test_rejects_unknown_approval_placeholder_before_launch(self, run_mock) -> None:
        write_agents(
            self.aikito_dir,
            """[agents.custom.runner]
command = ["custom"]
auto_approve_args = ["{unknown}"]
""",
        )
        with self.assertRaisesRegex(
            MemoryMaintenanceError, "Invalid runner placeholder"
        ):
            run_memory_maintenance(self.aikito_dir, "global", "custom", self.root)
        run_mock.assert_not_called()

    def test_rejects_unconfigured_agent(self) -> None:
        with self.assertRaisesRegex(MemoryMaintenanceError, "not found"):
            load_agent_runner(self.aikito_dir, "unknown")

    def test_distinguishes_missing_runner_from_invalid_command(self) -> None:
        write_agents(
            self.aikito_dir,
            """[agents.no-runner]
display_name = "No Runner"

[agents.bad-command.runner]
command = "not-an-array"

[agents.bad-env.runner]
command = ["agent"]

[agents.bad-env.runner.env]
INVALID = 1
""",
        )

        with self.assertRaisesRegex(MemoryMaintenanceError, "no runner configuration"):
            load_agent_runner(self.aikito_dir, "no-runner")
        with self.assertRaisesRegex(MemoryMaintenanceError, "invalid runner.command"):
            load_agent_runner(self.aikito_dir, "bad-command")
        with self.assertRaisesRegex(MemoryMaintenanceError, "invalid runner.env"):
            load_agent_runner(self.aikito_dir, "bad-env")

    @patch("aikito.maintain.subprocess.run")
    def test_reports_invalid_placeholder_syntax(self, run_mock) -> None:
        for placeholder in ("{workdir", "{0}"):
            with self.subTest(placeholder=placeholder):
                write_agents(
                    self.aikito_dir,
                    f'[agents.codex.runner]\ncommand = ["codex", "{placeholder}"]\n',
                )
                with self.assertRaisesRegex(
                    MemoryMaintenanceError, "Invalid runner placeholder"
                ):
                    run_memory_maintenance(
                        self.aikito_dir, "example", "codex", self.root
                    )

        run_mock.assert_not_called()

    @patch("aikito.maintain.subprocess.run")
    def test_launches_agent_with_confirmation_gated_prompt(self, run_mock) -> None:
        run_mock.return_value.returncode = 0

        result = run_memory_maintenance(self.aikito_dir, "example", "codex", self.root)

        self.assertEqual(result, 0)
        command = run_mock.call_args.args[0]
        prompt = command[-1]
        self.assertEqual(command[:2], ["fake-agent", str(self.project_path.resolve())])
        self.assertIn(str(self.project_memory.resolve()), prompt)

        self.assertIn("review the complete selected scope", prompt)
        self.assertIn("Do not modify files", prompt)
        self.assertEqual(run_mock.call_args.kwargs["cwd"], self.project_path.resolve())
        self.assertEqual(
            run_mock.call_args.kwargs["env"]["HTTPS_PROXY"],
            "http://proxy.example:1234",
        )
        self.assertEqual(run_mock.call_args.kwargs["env"]["RUN_SCOPE"], "example")

    def test_prompt_requires_integrity_repairs_and_no_push(self) -> None:
        scope = resolve_memory_maintenance_scope(self.aikito_dir, "example", self.root)
        prompt = build_memory_maintenance_prompt(scope)

        self.assertIn("Repair affected wikilinks", prompt)
        self.assertIn("Do not push", prompt)
        self.assertIn("relevant canonical skills and instructions", prompt)
        self.assertIn("Collect unresolved questions", prompt)
        self.assertIn("Do not modify skills or instructions", prompt)
        self.assertIn("without repeated permission requests", prompt)
        self.assertIn("number of notes reviewed", prompt)
        self.assertIn("unread or unverified notes", prompt)
        self.assertIn("obtain explicit approval for those files", prompt)
        self.assertIn(
            "Tool auto-approval does not count as proposal confirmation", prompt
        )
        self.assertIn("interactive follow-up is unavailable", prompt)
        self.assertIn("without asking for each operation", prompt)
        self.assertIn("ask before proceeding", prompt)
        self.assertIn("Do not create or maintain a separate memory index", prompt)
