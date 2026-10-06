"""Unit tests for aikito.context_footprint module."""

import tempfile
import unittest
from pathlib import Path

from aikito.context_footprint import (
    estimate_project_context,
    estimate_tokens,
    extract_skill_description,
    format_token_estimate,
    get_skill_context_bytes,
)


class TestContextFootprintEstimates(unittest.TestCase):
    def test_estimate_tokens_empty(self) -> None:
        self.assertEqual(estimate_tokens(""), 0)

    def test_estimate_tokens_ceil_math(self) -> None:
        # 1 byte -> 1 token
        self.assertEqual(estimate_tokens("a"), 1)
        # 4 bytes -> 1 token
        self.assertEqual(estimate_tokens("abcd"), 1)
        # 5 bytes -> 2 tokens
        self.assertEqual(estimate_tokens("abcde"), 2)
        # UTF-8: Chinese character is 3 bytes -> 1 token
        self.assertEqual(estimate_tokens("中"), 1)
        # 2 Chinese chars: 6 bytes -> 2 tokens
        self.assertEqual(estimate_tokens("中国"), 2)

    def test_format_token_estimate_boundaries(self) -> None:
        self.assertEqual(format_token_estimate(0), "~0")
        self.assertEqual(format_token_estimate(-5), "~0")
        self.assertEqual(format_token_estimate(42), "~42")
        self.assertEqual(format_token_estimate(999), "~999")
        self.assertEqual(format_token_estimate(1000), "~1.0k")
        self.assertEqual(format_token_estimate(6200), "~6.2k")
        self.assertEqual(format_token_estimate(18400), "~18.4k")

    def test_extract_colon_description(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "SKILL.md").write_text(
                "---\ndescription:\n  Use when: reviewing code\nmetadata:\n  author: me\n---\nReview.\n"
            )
            self.assertEqual(
                extract_skill_description(root), "Use when: reviewing code"
            )

    def test_extract_skill_description(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "my-skill"
            skill_dir.mkdir()

            # Missing SKILL.md
            self.assertIsNone(extract_skill_description(skill_dir))

            # SKILL.md with description
            (skill_dir / "SKILL.md").write_text(
                '---\nname: my-skill\ndescription: "A helpful skill"\n---\n# Body\n',
                encoding="utf-8",
            )
            self.assertEqual(extract_skill_description(skill_dir), "A helpful skill")

            # SKILL.md with empty description
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: \n---\n# Body\n",
                encoding="utf-8",
            )
            self.assertIsNone(extract_skill_description(skill_dir))

            # SKILL.md without frontmatter
            (skill_dir / "SKILL.md").write_text("# Just Markdown\n", encoding="utf-8")
            self.assertIsNone(extract_skill_description(skill_dir))

            # SKILL.md with folded block scalar (description: >-)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: >-\n  First line of folded description.\n  Second line of folded description.\n---\n# Body\n",
                encoding="utf-8",
            )
            self.assertEqual(
                extract_skill_description(skill_dir),
                "First line of folded description. Second line of folded description.",
            )

            # SKILL.md with literal block scalar (description: |)
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: |\n  Line one of literal.\n  Line two of literal.\n---\n# Body\n",
                encoding="utf-8",
            )
            self.assertEqual(
                extract_skill_description(skill_dir),
                "Line one of literal.\nLine two of literal.",
            )

            # SKILL.md with empty folded block scalar
            (skill_dir / "SKILL.md").write_text(
                "---\nname: my-skill\ndescription: >-\n---\n# Body\n",
                encoding="utf-8",
            )
            self.assertIsNone(extract_skill_description(skill_dir))

    def test_get_skill_context_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            skills_dir = Path(td)
            s1 = skills_dir / "s1"
            s1.mkdir()
            (s1 / "SKILL.md").write_text(
                '---\ndescription: "Desc"\n---\n# Body\n', encoding="utf-8"
            )
            # s1 with desc -> "s1: Desc"
            expected = len("s1: Desc".encode("utf-8"))
            self.assertEqual(get_skill_context_bytes("s1", skills_dir), expected)

            # s2 without desc -> "s2"
            s2 = skills_dir / "s2"
            s2.mkdir()
            (s2 / "SKILL.md").write_text(
                "---\nname: s2\n---\n# Body\n", encoding="utf-8"
            )
            self.assertEqual(
                get_skill_context_bytes("s2", skills_dir), len("s2".encode("utf-8"))
            )

            # s3 missing SKILL.md -> 0
            s3 = skills_dir / "s3"
            s3.mkdir()
            self.assertEqual(get_skill_context_bytes("s3", skills_dir), 0)

            # always loaded skill (durable-memory) -> full SKILL.md
            dm = skills_dir / "durable-memory"
            dm.mkdir()
            dm_content = "---\nname: durable-memory\n---\n# Full body content here\n"
            (dm / "SKILL.md").write_text(dm_content, encoding="utf-8")
            self.assertEqual(
                get_skill_context_bytes("durable-memory", skills_dir),
                len(dm_content.encode("utf-8")),
            )


class TestProjectContextEstimation(unittest.TestCase):
    def test_estimate_project_context_full_flow(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            global_dir = root / "global"
            global_dir.mkdir(parents=True)
            global_agents = global_dir / "AGENTS.md"
            global_agents.write_text(
                "# Global Rules\n- Less is more.\n", encoding="utf-8"
            )

            # Skills
            skills_dir = root / "skills"
            skills_dir.mkdir()
            (skills_dir / "common").mkdir()
            (skills_dir / "common" / "SKILL.md").write_text(
                '---\ndescription: "Common utility"\n---\n', encoding="utf-8"
            )
            (skills_dir / "proj-only").mkdir()
            (skills_dir / "proj-only" / "SKILL.md").write_text(
                '---\ndescription: "Project specific"\n---\n', encoding="utf-8"
            )
            (skills_dir / "durable-memory").mkdir()
            dm_text = "---\nname: durable-memory\n---\n# Durable memory full text\n"
            (skills_dir / "durable-memory" / "SKILL.md").write_text(
                dm_text, encoding="utf-8"
            )

            # skills.toml has common and durable-memory
            (root / "skills.toml").write_text(
                'skills = ["common", "durable-memory"]\n', encoding="utf-8"
            )

            # Project
            proj_dir = root / "projects" / "myproj"
            proj_dir.mkdir(parents=True)
            (proj_dir / "AGENTS.md").write_text(
                "# Project instructions\n", encoding="utf-8"
            )

            # Estimate context for project selecting ("proj-only", "common")
            # Effective skills: {"common", "durable-memory", "proj-only"}
            tokens = estimate_project_context(root, proj_dir, ("proj-only", "common"))

            # Calculate expected total bytes:
            global_b = len(global_agents.read_text(encoding="utf-8").encode("utf-8"))
            proj_b = len(
                (proj_dir / "AGENTS.md").read_text(encoding="utf-8").encode("utf-8")
            )
            common_b = len("common: Common utility".encode("utf-8"))
            proj_only_b = len("proj-only: Project specific".encode("utf-8"))
            dm_b = len(dm_text.encode("utf-8"))
            total_b = global_b + proj_b + common_b + proj_only_b + dm_b
            expected_tokens = (total_b + 3) // 4

            self.assertEqual(tokens, expected_tokens)

    def test_estimate_project_context_no_durable_memory(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "skills"
            skills_dir.mkdir()
            (skills_dir / "durable-memory").mkdir()
            (skills_dir / "durable-memory" / "SKILL.md").write_text(
                "---\nname: durable-memory\n---\n# Body\n", encoding="utf-8"
            )
            # Not in skills.toml, not in project skills
            (root / "skills.toml").write_text("skills = []\n", encoding="utf-8")

            proj_dir = root / "projects" / "p1"
            proj_dir.mkdir(parents=True)

            tokens = estimate_project_context(root, proj_dir, ())
            self.assertEqual(tokens, 0)

    def test_estimate_project_context_multiline_description(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skills_dir = root / "skills"
            skills_dir.mkdir()
            (skills_dir / "folded-skill").mkdir()
            (skills_dir / "folded-skill" / "SKILL.md").write_text(
                "---\nname: folded-skill\ndescription: >-\n  This is line one.\n  This is line two.\n---\n# Body\n",
                encoding="utf-8",
            )
            (root / "skills.toml").write_text(
                'skills = ["folded-skill"]\n', encoding="utf-8"
            )

            proj_dir = root / "projects" / "p1"
            proj_dir.mkdir(parents=True)

            tokens = estimate_project_context(root, proj_dir, ())
            expected_text = "folded-skill: This is line one. This is line two."
            expected_tokens = (len(expected_text.encode("utf-8")) + 3) // 4
            self.assertEqual(tokens, expected_tokens)
            # Ensure it is significantly larger than what ">-" would have given
            flawed_tokens = (len("folded-skill: >-".encode("utf-8")) + 3) // 4
            self.assertGreater(tokens, flawed_tokens)


if __name__ == "__main__":
    unittest.main()
