import io
import shutil
import tempfile
import unittest
from pathlib import Path

from aikito.bundled_skills import (
    outdated_bundled_skills,
    print_bundled_skill_notice,
    refresh_bundled_skills,
)
from aikito.templating import BUNDLED_SKILL_NAMES, bundled_skill_path


class BundledSkillRefreshTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.workspace = self.root / "workspace"
        self.home = self.root / "home"
        self.skills = self.workspace / "skills"
        self.skills.mkdir(parents=True)
        self.home.mkdir()
        for name in BUNDLED_SKILL_NAMES:
            shutil.copytree(bundled_skill_path(name), self.skills / name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_identical_snapshots_need_no_refresh(self) -> None:
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        self.assertEqual(
            refresh_bundled_skills(self.workspace, self.home),
            (),
        )
        self.assertFalse((self.home / ".aikito").exists())

    def test_notice_reports_only_selected_divergent_skill(self) -> None:
        (self.skills / "aikito" / "SKILL.md").write_text(
            "customized\n", encoding="utf-8"
        )
        (self.skills / "durable-memory" / "SKILL.md").write_text(
            "customized\n", encoding="utf-8"
        )
        output = io.StringIO()

        outdated = print_bundled_skill_notice(
            self.workspace,
            names=("aikito",),
            output=output,
        )

        self.assertEqual(outdated, ("aikito",))
        self.assertIn("aikito", output.getvalue())
        self.assertNotIn("durable-memory", output.getvalue())

    def test_dry_run_reports_without_writing_or_backing_up(self) -> None:
        skill_file = self.skills / "aikito" / "SKILL.md"
        skill_file.write_text("customized\n", encoding="utf-8")

        refreshed = refresh_bundled_skills(
            self.workspace,
            self.home,
            dry_run=True,
        )

        self.assertEqual(refreshed, ("aikito",))
        self.assertEqual(skill_file.read_text(encoding="utf-8"), "customized\n")
        self.assertFalse((self.home / ".aikito").exists())

    def test_refresh_backs_up_and_replaces_complete_skill_trees(self) -> None:
        for name in BUNDLED_SKILL_NAMES:
            skill_dir = self.skills / name
            (skill_dir / "SKILL.md").write_text("customized\n", encoding="utf-8")
            (skill_dir / "local.md").write_text("local\n", encoding="utf-8")

        refreshed = refresh_bundled_skills(self.workspace, self.home)

        self.assertEqual(refreshed, BUNDLED_SKILL_NAMES)
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        for name in BUNDLED_SKILL_NAMES:
            self.assertFalse((self.skills / name / "local.md").exists())
            self.assertEqual(
                (self.skills / name / "SKILL.md").read_bytes(),
                (bundled_skill_path(name) / "SKILL.md").read_bytes(),
            )

        backup_roots = list(
            (self.home / ".aikito" / "backups").glob("bundled-skills_*")
        )
        self.assertEqual(len(backup_roots), 1)
        for name in BUNDLED_SKILL_NAMES:
            self.assertEqual(
                (backup_roots[0] / name / "SKILL.md").read_text(encoding="utf-8"),
                "customized\n",
            )
            self.assertTrue((backup_roots[0] / name / "local.md").is_file())

    def test_refresh_restores_missing_bundled_skill(self) -> None:
        shutil.rmtree(self.skills / "durable-memory")

        refreshed = refresh_bundled_skills(self.workspace, self.home)

        self.assertEqual(refreshed, ("durable-memory",))
        self.assertEqual(outdated_bundled_skills(self.workspace), ())
        self.assertFalse((self.home / ".aikito").exists())


if __name__ == "__main__":
    unittest.main()
