import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito_maintain import (
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
            f'name = "example"\npath = "{self.project_path}"\n',
            encoding="utf-8",
        )
        (self.aikito_dir / "agents.toml").write_text(
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
            encoding="utf-8",
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

    def test_rejects_unconfigured_agent(self) -> None:
        with self.assertRaisesRegex(MemoryMaintenanceError, "not found"):
            load_agent_runner(self.aikito_dir, "unknown")

    def test_distinguishes_missing_runner_from_invalid_command(self) -> None:
        config_path = self.aikito_dir / "agents.toml"
        config_path.write_text(
            """[agents.no-runner]
display_name = "No Runner"

[agents.bad-command.runner]
command = "not-an-array"
""",
            encoding="utf-8",
        )

        with self.assertRaisesRegex(MemoryMaintenanceError, "no runner configuration"):
            load_agent_runner(self.aikito_dir, "no-runner")
        with self.assertRaisesRegex(MemoryMaintenanceError, "invalid runner.command"):
            load_agent_runner(self.aikito_dir, "bad-command")

    @patch("aikito_maintain.subprocess.run")
    def test_reports_invalid_placeholder_syntax(self, run_mock) -> None:
        config_path = self.aikito_dir / "agents.toml"

        for placeholder in ("{workdir", "{0}"):
            with self.subTest(placeholder=placeholder):
                config_path.write_text(
                    f'[agents.codex.runner]\ncommand = ["codex", "{placeholder}"]\n',
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    MemoryMaintenanceError, "Invalid runner placeholder"
                ):
                    run_memory_maintenance(
                        self.aikito_dir, "example", "codex", self.root
                    )

        run_mock.assert_not_called()

    @patch("aikito_maintain.subprocess.run")
    def test_launches_agent_with_confirmation_gated_prompt(self, run_mock) -> None:
        run_mock.return_value.returncode = 0

        result = run_memory_maintenance(self.aikito_dir, "example", "codex", self.root)

        self.assertEqual(result, 0)
        command = run_mock.call_args.args[0]
        prompt = command[-1]
        self.assertEqual(command[:2], ["fake-agent", str(self.project_path.resolve())])
        self.assertIn(str(self.project_memory), prompt)
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

        self.assertIn("repair affected indices and wikilinks", prompt)
        self.assertIn("Do not push", prompt)
        self.assertIn("relevant canonical skills and instructions", prompt)
        self.assertIn("ask the user to decide", prompt)
        self.assertIn("Do not modify skills or instructions", prompt)
