"""Unit tests for shared link inspect and planning primitive."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase

from aikito.link import (
    LinkOperation,
    ObservedLink,
    inspect_link_target,
    plan_link_target,
)


class InspectLinkTargetTest(TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.mkdtemp()
        self.root = Path(self.tmp_dir)
        self.canonical = self.root / "canonical-skill"
        self.canonical.mkdir()
        (self.canonical / "SKILL.md").write_text("# Skill", encoding="utf-8")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp_dir, ignore_errors=True)

    def test_inspect_missing_target(self) -> None:
        target = self.root / "target"
        obs = inspect_link_target(target, self.canonical)
        self.assertEqual(obs.entry_type, "missing")
        self.assertFalse(obs.link_points_to_canonical)
        self.assertIsNone(obs.resolved_link_target)

    def test_inspect_correct_symlink(self) -> None:
        target = self.root / "target"
        target.symlink_to(self.canonical)
        obs = inspect_link_target(target, self.canonical)
        self.assertEqual(obs.entry_type, "symlink")
        self.assertTrue(obs.link_points_to_canonical)
        self.assertEqual(obs.resolved_link_target, self.canonical.resolve())

    def test_inspect_wrong_symlink(self) -> None:
        other = self.root / "other"
        other.mkdir()
        target = self.root / "target"
        target.symlink_to(other)
        obs = inspect_link_target(target, self.canonical)
        self.assertEqual(obs.entry_type, "symlink")
        self.assertFalse(obs.link_points_to_canonical)
        self.assertEqual(obs.resolved_link_target, other.resolve())

    def test_inspect_broken_symlink_pointing_to_canonical(self) -> None:
        missing_canon = self.root / "deleted-skill"
        target = self.root / "target"
        target.symlink_to(missing_canon)
        obs = inspect_link_target(
            target, missing_canon, canonical_valid=False, canonical_error="Missing"
        )
        self.assertEqual(obs.entry_type, "symlink")
        self.assertTrue(obs.link_points_to_canonical)
        self.assertFalse(obs.canonical_valid)

    def test_inspect_directory(self) -> None:
        target = self.root / "target"
        target.mkdir()
        obs = inspect_link_target(target, self.canonical)
        self.assertEqual(obs.entry_type, "dir")
        self.assertFalse(obs.link_points_to_canonical)

    def test_inspect_regular_file(self) -> None:
        target = self.root / "target"
        target.write_text("hello", encoding="utf-8")
        obs = inspect_link_target(target, self.canonical)
        self.assertEqual(obs.entry_type, "file")
        self.assertFalse(obs.link_points_to_canonical)

    def test_inspect_same_object(self) -> None:
        target = self.canonical
        obs = inspect_link_target(target, self.canonical, is_same_object=True)
        self.assertTrue(obs.is_same_object)


class PlanLinkTargetTest(TestCase):
    def setUp(self) -> None:
        self.target_path = Path("/home/user/.agents/skills/foo")
        self.canonical = Path("/home/user/aikito/skills/foo")

    def test_selected_missing_creates_link(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="missing",
            expected_canonical=self.canonical,
            canonical_valid=True,
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CREATE")
        self.assertEqual(op.rule_id, "INV-TR-01")
        self.assertTrue(op.is_authorized)
        self.assertFalse(op.requires_parent_creation)

    def test_selected_correct_symlink_noop(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="symlink",
            expected_canonical=self.canonical,
            link_points_to_canonical=True,
            canonical_valid=True,
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "NOOP")
        self.assertEqual(op.rule_id, "INV-TR-03")
        self.assertTrue(op.is_authorized)

    def test_selected_broken_link_to_canonical_conflicts(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="symlink",
            expected_canonical=self.canonical,
            link_points_to_canonical=True,
            canonical_valid=False,
            canonical_error="Canonical missing",
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-TR-04")
        self.assertFalse(op.is_authorized)

    def test_selected_missing_canonical_conflicts(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="missing",
            expected_canonical=self.canonical,
            canonical_valid=False,
            canonical_error="Canonical missing",
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-TR-02")
        self.assertFalse(op.is_authorized)

    def test_selected_wrong_symlink_conflicts(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="symlink",
            expected_canonical=self.canonical,
            link_points_to_canonical=False,
            resolved_link_target=Path("/somewhere/else"),
            canonical_valid=True,
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-TR-05")
        self.assertFalse(op.is_authorized)

    def test_selected_normal_directory_conflicts(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="dir",
            expected_canonical=self.canonical,
            canonical_valid=True,
            target_kind="managed_entry",
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-GLB-02")
        self.assertFalse(op.is_authorized)

    def test_selected_unsupported_file_conflicts(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="file",
            expected_canonical=self.canonical,
            canonical_valid=True,
        )
        op = plan_link_target(obs, desired_mode="link", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-TR-13")
        self.assertFalse(op.is_authorized)

    def test_deselected_exact_link_unlinks(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="symlink",
            expected_canonical=self.canonical,
            link_points_to_canonical=True,
            canonical_valid=True,
        )
        op = plan_link_target(obs, desired_mode="absent", resource_name="foo")
        self.assertEqual(op.action, "UNLINK")
        self.assertEqual(op.rule_id, "INV-TR-14")
        self.assertTrue(op.is_authorized)

    def test_deselected_wrong_symlink_preserved_noop(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="symlink",
            expected_canonical=self.canonical,
            link_points_to_canonical=False,
            resolved_link_target=Path("/other/workspace/foo"),
            canonical_valid=True,
            target_kind="project_entry",
        )
        op = plan_link_target(obs, desired_mode="absent", resource_name="foo")
        self.assertEqual(op.action, "NOOP")
        self.assertEqual(op.rule_id, "INV-TR-15")
        self.assertTrue(op.is_authorized)

    def test_deselected_matching_dir_conflicts_for_global_entry(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="dir",
            expected_canonical=self.canonical,
            canonical_valid=True,
            target_kind="managed_entry",
            scope="global",
        )
        op = plan_link_target(obs, desired_mode="absent", resource_name="foo")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-GLB-03")
        self.assertFalse(op.is_authorized)

    def test_deselected_dir_preserved_noop_for_project_entry(self) -> None:
        obs = ObservedLink(
            target_path=self.target_path,
            entry_type="dir",
            expected_canonical=self.canonical,
            canonical_valid=True,
            target_kind="project_entry",
        )
        op = plan_link_target(obs, desired_mode="absent", resource_name="foo")
        self.assertEqual(op.action, "NOOP")
        self.assertEqual(op.rule_id, "INV-TR-17")
        self.assertTrue(op.is_authorized)

    def test_consumer_link_same_object_disposition(self) -> None:
        container = Path("/home/user/.agents/skills")
        obs = ObservedLink(
            target_path=container,
            entry_type="dir",
            expected_canonical=container,
            is_same_object=True,
            target_kind="consumer_link",
        )
        op = plan_link_target(obs, desired_mode="link")
        self.assertEqual(op.action, "SHARED_PATH")
        self.assertEqual(op.rule_id, "INV-GLB-06")
        self.assertTrue(op.is_same_object)
        self.assertTrue(op.is_authorized)

    def test_consumer_link_parent_missing_not_installed_skips(self) -> None:
        target = Path("/home/user/.claude/skills")
        container = Path("/home/user/.agents/skills")
        obs = ObservedLink(
            target_path=target,
            entry_type="missing",
            expected_canonical=container,
            canonical_valid=True,
            target_kind="consumer_link",
        )
        op = plan_link_target(
            obs,
            desired_mode="link",
            availability_status="not_installed",
            parent_exists=False,
        )
        self.assertEqual(op.action, "SKIP")
        self.assertEqual(op.rule_id, "INV-GLB-05")
        self.assertTrue(op.is_authorized)

    def test_consumer_link_parent_missing_unknown_skips_with_finding(self) -> None:
        target = Path("/home/user/.custom/skills")
        container = Path("/home/user/.agents/skills")
        obs = ObservedLink(
            target_path=target,
            entry_type="missing",
            expected_canonical=container,
            canonical_valid=True,
            target_kind="consumer_link",
        )
        op = plan_link_target(
            obs,
            desired_mode="link",
            availability_status="unknown",
            parent_exists=False,
        )
        self.assertEqual(op.action, "SKIP")
        self.assertEqual(op.rule_id, "INV-GLB-05")
        self.assertIsNotNone(op.finding)

    def test_consumer_link_parent_missing_installed_creates_parent(self) -> None:
        target = Path("/home/user/.claude/skills")
        container = Path("/home/user/.agents/skills")
        obs = ObservedLink(
            target_path=target,
            entry_type="missing",
            expected_canonical=container,
            canonical_valid=True,
            target_kind="consumer_link",
        )
        op = plan_link_target(
            obs,
            desired_mode="link",
            availability_status="installed",
            parent_exists=False,
        )
        self.assertEqual(op.action, "CREATE")
        self.assertEqual(op.rule_id, "INV-TR-01")
        self.assertTrue(op.requires_parent_creation)

    def test_consumer_link_wrong_symlink_conflicts(self) -> None:
        target = Path("/home/user/.claude/skills")
        container = Path("/home/user/.agents/skills")
        obs = ObservedLink(
            target_path=target,
            entry_type="symlink",
            expected_canonical=container,
            link_points_to_canonical=False,
            resolved_link_target=Path("/somewhere/custom"),
            canonical_valid=True,
            target_kind="consumer_link",
        )
        op = plan_link_target(obs, desired_mode="link")
        self.assertEqual(op.action, "CONFLICT")
        self.assertEqual(op.rule_id, "INV-GLB-05")
        self.assertFalse(op.is_authorized)

    def test_legacy_container_migration(self) -> None:
        container = Path("/home/user/.agents/skills")
        ws_skills = Path("/home/user/workspace/skills")

        # Exact root link migrates
        obs_exact = ObservedLink(
            target_path=container,
            entry_type="symlink",
            expected_canonical=ws_skills,
            link_points_to_canonical=True,
            canonical_valid=True,
        )
        op = plan_link_target(obs_exact, is_legacy_container=True)
        self.assertEqual(op.action, "MIGRATE_CONTAINER")
        self.assertEqual(op.rule_id, "INV-GLB-04")
        self.assertTrue(op.is_authorized)

        # Wrong target link conflicts
        obs_wrong = ObservedLink(
            target_path=container,
            entry_type="symlink",
            expected_canonical=ws_skills,
            link_points_to_canonical=False,
            resolved_link_target=Path("/other/path"),
            canonical_valid=True,
        )
        op_wrong = plan_link_target(obs_wrong, is_legacy_container=True)
        self.assertEqual(op_wrong.action, "CONFLICT")
        self.assertEqual(op_wrong.rule_id, "INV-GLB-04")
        self.assertFalse(op_wrong.is_authorized)
