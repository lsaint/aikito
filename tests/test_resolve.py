import tempfile
import unittest
from pathlib import Path

from aikito.resolve import (
    ProjectContextConflictError,
    detect_current_project,
    resolve_instruction_target,
    resolve_project_filter,
)


class ResolveProjectContextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.aikito_dir = self.root / "aikito"
        self.home = self.root / "home"
        self.aikito_dir.mkdir(parents=True)
        self.home.mkdir(parents=True)
        self.projects_dir = self.aikito_dir / "projects"
        self.projects_dir.mkdir(parents=True)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _register_project(self, name: str, code_path: Path) -> Path:
        proj_dir = self.projects_dir / name
        proj_dir.mkdir(parents=True, exist_ok=True)
        code_path.mkdir(parents=True, exist_ok=True)
        (proj_dir / "agent.toml").write_text(
            f'name = "{name}"\npath = "{code_path.as_posix()}"\n',
            encoding="utf-8",
        )
        return proj_dir

    def test_detect_inside_workspace_project_folder(self) -> None:
        code_path = self.root / "code" / "app"
        proj_dir = self._register_project("app", code_path)
        subdir = proj_dir / "memory" / "notes"
        subdir.mkdir(parents=True)

        self.assertEqual(
            detect_current_project(self.aikito_dir, proj_dir, self.home),
            "app",
        )
        self.assertEqual(
            detect_current_project(self.aikito_dir, subdir, self.home),
            "app",
        )

    def test_unregistered_workspace_folder_without_agent_toml_is_not_detected(
        self,
    ) -> None:
        unreg_dir = self.projects_dir / "unregistered"
        unreg_dir.mkdir(parents=True)
        (unreg_dir / "memory" / "notes").mkdir(parents=True)

        self.assertIsNone(detect_current_project(self.aikito_dir, unreg_dir, self.home))
        self.assertIsNone(
            detect_current_project(self.aikito_dir, unreg_dir / "memory", self.home)
        )

    def test_detect_active_path_and_nested_subdirectories(self) -> None:
        code_path = self.root / "code" / "app"
        self._register_project("app", code_path)
        deep_subdir = code_path / "src" / "components" / "ui"
        deep_subdir.mkdir(parents=True)

        self.assertEqual(
            detect_current_project(self.aikito_dir, code_path, self.home),
            "app",
        )
        self.assertEqual(
            detect_current_project(self.aikito_dir, deep_subdir, self.home),
            "app",
        )

    def test_detect_monorepo_longest_prefix_match(self) -> None:
        repo_root = self.root / "repo"
        sub_package = repo_root / "packages" / "sub"
        self._register_project("repo-root", repo_root)
        self._register_project("repo-sub", sub_package)

        inside_sub = sub_package / "src"
        inside_sub.mkdir(parents=True)
        outside_sub = repo_root / "docs"
        outside_sub.mkdir(parents=True)

        self.assertEqual(
            detect_current_project(self.aikito_dir, inside_sub, self.home),
            "repo-sub",
        )
        self.assertEqual(
            detect_current_project(self.aikito_dir, outside_sub, self.home),
            "repo-root",
        )

    def test_detect_conflict_when_same_longest_path(self) -> None:
        shared_path = self.root / "code" / "shared"
        self._register_project("proj-a", shared_path)
        self._register_project("proj-b", shared_path)

        with self.assertRaises(ProjectContextConflictError) as ctx:
            detect_current_project(self.aikito_dir, shared_path, self.home)

        self.assertEqual(ctx.exception.projects, ["proj-a", "proj-b"])

    def test_detect_unregistered_directory_returns_none(self) -> None:
        unrelated = self.root / "unrelated"
        unrelated.mkdir(parents=True)
        self.assertIsNone(detect_current_project(self.aikito_dir, unrelated, self.home))

    def test_resolve_project_filter_with_dot(self) -> None:
        code_path = self.root / "code" / "app"
        self._register_project("app", code_path)

        # Inside project
        self.assertEqual(
            resolve_project_filter(self.aikito_dir, ".", code_path, self.home),
            "app",
        )

        # Outside project -> exits 1
        unrelated = self.root / "unrelated"
        unrelated.mkdir(parents=True)
        with self.assertRaises(SystemExit):
            resolve_project_filter(self.aikito_dir, ".", unrelated, self.home)

    def test_resolve_instruction_target_with_dot(self) -> None:
        code_path = self.root / "code" / "app"
        proj_dir = self._register_project("app", code_path)
        agents_md = proj_dir / "AGENTS.md"
        agents_md.write_text("# App Instructions\n", encoding="utf-8")

        name, path = resolve_instruction_target(
            self.aikito_dir, self.home, ".", code_path
        )
        self.assertEqual(name, "app")
        self.assertEqual(path, agents_md)


if __name__ == "__main__":
    unittest.main()
