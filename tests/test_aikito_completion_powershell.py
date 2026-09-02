import unittest

from aikito_cli_loader import load_cli

from aikito_completion import generate_powershell
from aikito_completion_powershell import generate_powershell as gen_pwsh_direct

AIKITO_CLI = load_cli()


class AikitoCompletionPowerShellTest(unittest.TestCase):
    def test_generate_powershell_structure(self) -> None:
        parser = AIKITO_CLI.build_parser()
        script = generate_powershell(parser)

        # Completer registration
        self.assertIn("Register-ArgumentCompleter -Native -CommandName aikito", script)
        self.assertIn("param($wordToComplete, $commandAst, $cursorPosition)", script)

        # Commands and flags
        self.assertIn('"show"', script)
        self.assertIn('"sync"', script)
        self.assertIn('"doctor"', script)
        self.assertIn('"completion"', script)
        self.assertIn('"--version"', script)

        # Subcommand mappings
        self.assertIn('"show memory"', script)
        self.assertIn('"show skill"', script)
        self.assertIn('"sync project"', script)
        self.assertIn('"init workspace"', script)

        # Dynamic completions
        self.assertIn("memory-completions", script)
        self.assertIn("inbox-completions", script)
        self.assertIn("skills", script)
        self.assertIn("projects", script)
        self.assertIn("subagents", script)
        self.assertIn("mcps", script)

    def test_direct_generator_matches_wrapper(self) -> None:
        parser = AIKITO_CLI.build_parser()
        self.assertEqual(generate_powershell(parser), gen_pwsh_direct(parser))

    def test_powershell_profile_install_instructions_included(self) -> None:
        script = generate_powershell()
        self.assertIn("Invoke-Expression (& aikito completion powershell | Out-String)", script)
        self.assertIn("$PROFILE", script)


if __name__ == "__main__":
    unittest.main()
