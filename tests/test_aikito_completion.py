import importlib.machinery
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

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

from aikito_completion import (
    extract_cli_schema,
    generate_bash,
    generate_fish,
    generate_zsh,
    get_candidates,
    list_memories,
    list_projects,
    list_skills,
)
from aikito_memory import (
    MemoryFileItem,
    MemoryTargetConflictError,
    find_memory_files,
    resolve_memory_target,
)


class AikitoCompletionReflectionTest(unittest.TestCase):
    def test_extract_cli_schema_includes_aliases_and_flags(self) -> None:
        parser = AIKITO_CLI.build_parser()
        schema = extract_cli_schema(parser)

        # Top-level commands & flags
        self.assertIn("show", schema["commands"])
        self.assertIn("sync", schema["commands"])
        self.assertIn("edit", schema["commands"])
        self.assertIn("--version", schema["flags"])

        # Subcommand aliases
        show_subs = schema["commands"]["show"]["subcommands"]
        self.assertIn("skill", show_subs)
        self.assertIn("skills", show_subs)  # Alias of skill
        self.assertIn("subagents", show_subs)
        self.assertIn("subagent", show_subs)  # Alias of subagents

        sync_subs = schema["commands"]["sync"]["subcommands"]
        self.assertIn("subagents", sync_subs)
        self.assertIn("subagent", sync_subs)  # Alias of subagents

        edit_subs = schema["commands"]["edit"]["subcommands"]
        self.assertIn("skill", edit_subs)
        self.assertIn("skills", edit_subs)  # Alias of skill

        # Flags per command & subcommand
        adopt_flags = schema["commands"]["adopt"]["flags"]
        self.assertIn("--apply", adopt_flags)
        self.assertIn("--dry-run", adopt_flags)

        doctor_flags = schema["commands"]["doctor"]["flags"]
        self.assertIn("--json", doctor_flags)
        self.assertIn("--stale-days", doctor_flags)

        sync_mcp_flags = schema["commands"]["sync"]["subcommands"]["mcp"]["flags"]
        self.assertIn("--dry-run", sync_mcp_flags)
        self.assertIn("--force", sync_mcp_flags)

        sync_sub_flags = schema["commands"]["sync"]["subcommands"]["subagents"]["flags"]
        self.assertIn("--dry-run", sync_sub_flags)
        self.assertIn("--force", sync_sub_flags)
        self.assertIn("--prune", sync_sub_flags)

    def test_generators_produce_script_with_aliases_and_flags(self) -> None:
        parser = AIKITO_CLI.build_parser()

        zsh = generate_zsh(parser)
        self.assertTrue(zsh.startswith("#compdef aikito"))
        self.assertIn('if [[ "$funcstack[1]" == *"_aikito"* ]]; then', zsh)
        self.assertIn("skills", zsh)
        self.assertIn("subagent", zsh)
        self.assertIn("--dry-run", zsh)
        self.assertIn("--apply", zsh)
        self.assertIn("--prune", zsh)
        self.assertIn("_files -/", zsh)

        bash = generate_bash(parser)
        self.assertIn("skills", bash)
        self.assertIn("subagent", bash)
        self.assertIn("--dry-run", bash)
        self.assertIn("--apply", bash)
        self.assertIn("--prune", bash)
        self.assertIn("compgen -d", bash)

        fish = generate_fish(parser)
        self.assertIn("skills", fish)
        self.assertIn("subagent", fish)
        self.assertIn("-l dry-run", fish)
        self.assertIn("-l apply", fish)
        self.assertIn("-l prune", fish)
        self.assertIn("-F", fish)


class AikitoCompletionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_list_memories_uses_canonical_rglob_and_full_identifiers(self) -> None:
        global_mem = self.aikito_dir / "memory"
        (global_mem / "notes" / "sub").mkdir(parents=True, exist_ok=True)
        (global_mem / "index.md").write_text("# Global Index", encoding="utf-8")
        (global_mem / "notes" / "bare.md").write_text("# Bare Note", encoding="utf-8")
        (global_mem / "notes" / "sub" / "nested.md").write_text(
            "# Nested Note", encoding="utf-8"
        )

        proj_mem = self.aikito_dir / "projects" / "myproj" / "memory" / "notes"
        proj_mem.mkdir(parents=True, exist_ok=True)
        (proj_mem / "proj-note.md").write_text("# Proj Note", encoding="utf-8")

        candidates = list_memories(self.aikito_dir)

        expected = sorted([
            "global/index",
            "bare",
            "global/bare",
            "global/notes/bare",
            "nested",
            "global/nested",
            "global/notes/sub/nested",
            "proj-note",
            "myproj/proj-note",
            "myproj/notes/proj-note",
        ])
        self.assertEqual(candidates, expected)

    def test_list_skills_includes_global_disk_and_project_registered_skills(
        self,
    ) -> None:
        (self.aikito_dir / "skills.toml").write_text(
            'skills = ["global-skill"]\n', encoding="utf-8"
        )

        (self.aikito_dir / "skills" / "disk-skill").mkdir(parents=True, exist_ok=True)

        proj_dir = self.aikito_dir / "projects" / "p1"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "agent.toml").write_text(
            'skills = ["proj-registered-skill"]\n', encoding="utf-8"
        )

        candidates = list_skills(self.aikito_dir)

        expected = ["disk-skill", "global-skill", "proj-registered-skill"]
        self.assertEqual(candidates, expected)

    def test_list_projects(self) -> None:
        projects_dir = self.aikito_dir / "projects"
        (projects_dir / "alpha").mkdir(parents=True, exist_ok=True)
        (projects_dir / "beta").mkdir(parents=True, exist_ok=True)
        (projects_dir / ".hidden").mkdir(parents=True, exist_ok=True)

        candidates = list_projects(self.aikito_dir)
        self.assertEqual(candidates, ["alpha", "beta"])

    def test_get_candidates_dispatch(self) -> None:
        (self.aikito_dir / "projects" / "p1").mkdir(parents=True, exist_ok=True)
        self.assertEqual(get_candidates("projects", self.aikito_dir), ["p1"])

        with self.assertRaises(ValueError):
            get_candidates("unknown_cat", self.aikito_dir)


class AikitoMemoryTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.aikito_dir = Path(self.tmp_dir.name)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_find_and_resolve_memory_target(self) -> None:
        global_notes = self.aikito_dir / "memory" / "notes"
        global_notes.mkdir(parents=True, exist_ok=True)
        note1 = global_notes / "architecture.md"
        note1.write_text("# Architecture", encoding="utf-8")

        items = find_memory_files(self.aikito_dir)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].full_identifier, "global/notes/architecture")
        self.assertEqual(items[0].short_identifier, "global/architecture")

        resolved = resolve_memory_target(self.aikito_dir, "global/notes/architecture")
        self.assertEqual(resolved.resolve(), note1.resolve())

        resolved_short = resolve_memory_target(self.aikito_dir, "architecture")
        self.assertEqual(resolved_short.resolve(), note1.resolve())


if __name__ == "__main__":
    unittest.main()
