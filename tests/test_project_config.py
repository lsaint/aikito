"""Unit and architectural tests for project_config.py and pure transforms (P1-B & P1-C)."""

from __future__ import annotations

import tomllib
import unittest
from pathlib import Path
import tempfile
from unittest.mock import patch

from aikito.project_config import (
    add_candidate_path,
    get_project_candidate_paths,
    resolve_project_path,
)
from aikito.project_sync import build_project_sync_batch


class ProjectConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.home = Path(self.temp_dir.name).resolve()

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_resolve_project_path(self) -> None:
        self.assertIsNone(resolve_project_path(None, self.home))
        self.assertIsNone(resolve_project_path("", self.home))
        self.assertEqual(resolve_project_path("~", self.home), self.home)
        self.assertEqual(
            resolve_project_path("~/code/proj", self.home),
            (self.home / "code" / "proj").resolve(),
        )

    def test_get_project_candidate_paths(self) -> None:
        # 1. paths dict
        config1 = {"paths": {"mac": "~/m", "win": "D:/w"}}
        self.assertEqual(
            get_project_candidate_paths(config1),
            (("mac", "~/m"), ("win", "D:/w")),
        )

        # 2. paths list
        config2 = {"paths": ["~/p1", "~/p2"]}
        self.assertEqual(
            get_project_candidate_paths(config2),
            (("1", "~/p1"), ("2", "~/p2")),
        )

        # 3. path scalar
        config3 = {"path": "~/single"}
        self.assertEqual(
            get_project_candidate_paths(config3),
            (("default", "~/single"),),
        )

    def test_pure_add_candidate_path_byte_preservation(self) -> None:
        """P1-C: Pure transform maintains determinism, idempotency, and byte/comment preservation."""
        sample_toml = (
            "# Top-level comment preserving\n"
            'name = "my_project"\n'
            'sync_mode = "link"\n'
            "\n"
            "[paths]\n"
            "# Existing path comment\n"
            'mac = "~/code/mac"\n'
            "\n"
            "[other_section]\n"
            'key = "value"\n'
        )
        pre_bytes = sample_toml.encode("utf-8")

        # 1. Transform produces deterministic post_bytes
        post_bytes_1 = add_candidate_path(pre_bytes, "~/code/linux", self.home)
        post_bytes_2 = add_candidate_path(pre_bytes, "~/code/linux", self.home)
        self.assertIsNotNone(post_bytes_1)
        self.assertEqual(post_bytes_1, post_bytes_2)

        # 2. Top-level and unrelated comments are preserved
        post_text = post_bytes_1.decode("utf-8")  # type: ignore[union-attr]
        self.assertIn("# Top-level comment preserving", post_text)
        self.assertIn("[other_section]", post_text)
        self.assertIn('key = "value"', post_text)

        # 3. Parsed TOML contains the new candidate path
        parsed = tomllib.loads(post_text)
        self.assertEqual(parsed["name"], "my_project")
        self.assertEqual(parsed["paths"]["mac"], "~/code/mac")
        self.assertIn("~/code/linux", parsed["paths"].values())

        # 4. Idempotency: adding the same path again returns None
        again = add_candidate_path(post_bytes_1, "~/code/linux", self.home)  # type: ignore[arg-type]
        self.assertIsNone(again)

    def test_planner_does_not_create_tempfile(self) -> None:
        """P1-C: build_project_sync_batch must purely compute CandidatePathCAS without temp files."""
        ws_root = self.home / "workspace"
        ws_root.mkdir(parents=True, exist_ok=True)
        proj_dir = ws_root / "projects" / "p1"
        proj_dir.mkdir(parents=True, exist_ok=True)
        agent_toml = proj_dir / "agent.toml"
        agent_toml.write_text('name = "p1"\npath = "~/existing"\n', encoding="utf-8")

        explicit = self.home / "new_path"
        explicit.mkdir(parents=True, exist_ok=True)

        data = tomllib.loads(agent_toml.read_text(encoding="utf-8"))

        pre_bytes = agent_toml.read_bytes()
        with (
            patch(
                "tempfile.NamedTemporaryFile", side_effect=AssertionError("tempfile")
            ),
            patch("tempfile.mkstemp", side_effect=AssertionError("tempfile")),
            patch("tempfile.mkdtemp", side_effect=AssertionError("tempfile")),
        ):
            batch = build_project_sync_batch(
                workspace_root=ws_root,
                home=self.home,
                project_name="p1",
                data=data,
                explicit_path=explicit,
                register_explicit_path=True,
            )
        self.assertEqual(agent_toml.read_bytes(), pre_bytes)

        self.assertIsNotNone(batch.config_cas)
        self.assertFalse(batch.config_cas.is_noop)  # type: ignore[union-attr]
        # Verify the candidate path was added to post_image_bytes
        post_data = tomllib.loads(batch.config_cas.post_image_bytes.decode("utf-8"))  # type: ignore[union-attr]
        candidates = [raw for _, raw in get_project_candidate_paths(post_data)]
        self.assertIn("~/new_path", candidates)

    def test_project_sync_module_has_no_tempfile_dependency(self) -> None:
        """P1-C: candidate path planning must not rely on temp files."""
        src = Path(__file__).resolve().parents[1] / "src" / "aikito" / "project_sync.py"
        self.assertNotIn("tempfile", src.read_text(encoding="utf-8"))
