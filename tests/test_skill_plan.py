"""Unit tests for pure skill planning covering FIX-TR-01 through FIX-TR-20 and authorizations."""

from __future__ import annotations

from pathlib import Path
from unittest import TestCase

from aikito.skill_plan import (
    DesiredSkill,
    ObservedSkill,
    SkillTarget,
    plan_single_skill,
)
from aikito.skill_state import SkillStateRecord


class SkillPlanTransitionTests(TestCase):
    def setUp(self) -> None:
        self.ws = Path("/ws")
        self.co = Path("/co")
        self.target = SkillTarget(
            workspace_root=self.ws,
            workspace_id="ws",
            project_name="demo",
            physical_checkout=self.co,
            skill_name="foo",
            target_path=self.co / ".agents" / "skills" / "foo",
        )
        self.canon = self.ws / "skills" / "foo"

    def test_fix_tr_01_create_link_and_copy(self) -> None:
        # Link mode missing target
        desired_link = DesiredSkill("foo", "link", self.canon)
        obs_missing = ObservedSkill(
            target=self.target, entry_type="missing", canonical_valid=True
        )
        op_link = plan_single_skill(self.target, desired_link, obs_missing)
        self.assertEqual(op_link.rule_id, "INV-TR-01")
        self.assertEqual(op_link.action, "CREATE")
        self.assertEqual(op_link.desired_representation, "link")

        # Copy mode missing target
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        obs_missing_c = ObservedSkill(
            target=self.target,
            entry_type="missing",
            canonical_valid=True,
            canonical_fingerprint="v1:c1",
        )
        op_copy = plan_single_skill(self.target, desired_copy, obs_missing_c)
        self.assertEqual(op_copy.rule_id, "INV-TR-01")
        self.assertEqual(op_copy.action, "CREATE")
        self.assertEqual(op_copy.desired_representation, "copy")
        self.assertEqual(op_copy.next_state_lifecycle, "active")
        self.assertEqual(op_copy.next_baseline_origin, "write")

    def test_fix_tr_02_canonical_missing_blocks(self) -> None:
        desired_link = DesiredSkill("foo", "link", self.canon)
        obs_bad_canon = ObservedSkill(
            target=self.target,
            entry_type="missing",
            canonical_valid=False,
            canonical_error="Missing canonical directory",
        )
        op = plan_single_skill(self.target, desired_link, obs_bad_canon)
        self.assertEqual(op.rule_id, "INV-TR-02")
        self.assertEqual(op.action, "CONFLICT")
        self.assertFalse(op.is_authorized)

    def test_fix_tr_03_link_owned_unchanged_noop(self) -> None:
        desired_link = DesiredSkill("foo", "link", self.canon)
        obs = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            link_points_to_canonical=True,
            canonical_valid=True,
        )
        op = plan_single_skill(self.target, desired_link, obs)
        self.assertEqual(op.rule_id, "INV-TR-03")
        self.assertEqual(op.action, "NOOP")

    def test_fix_tr_04_broken_link_to_canonical_blocks(self) -> None:
        desired_link = DesiredSkill("foo", "link", self.canon)
        obs = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            link_points_to_canonical=True,
            canonical_valid=False,
        )
        op = plan_single_skill(self.target, desired_link, obs)
        self.assertEqual(op.rule_id, "INV-TR-04")
        self.assertEqual(op.action, "CONFLICT")

    def test_fix_tr_05_external_symlink_conflicts(self) -> None:
        desired_link = DesiredSkill("foo", "link", self.canon)
        obs = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            raw_link_target=Path("/etc/passwd"),
            link_points_to_canonical=False,
            canonical_valid=True,
        )
        op = plan_single_skill(self.target, desired_link, obs, force=True)
        self.assertEqual(op.rule_id, "INV-TR-05")
        self.assertEqual(op.action, "CONFLICT")
        # Force cannot authorize reconnecting external symlink
        self.assertFalse(op.is_authorized)

    def test_fix_tr_07_copy_owned_unchanged_noop(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:same"
        )
        state = SkillStateRecord("foo", "copy", "active", "v1:same", "write", True)
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:same",
            canonical_fingerprint="v1:same",
            state_record=state,
        )
        op = plan_single_skill(self.target, desired_copy, obs)
        self.assertEqual(op.rule_id, "INV-TR-07")
        self.assertEqual(op.action, "NOOP")

    def test_fix_tr_08_copy_upstream_update(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c2"
        )
        state = SkillStateRecord("foo", "copy", "active", "v1:b1", "write", True)
        # R = B (v1:b1), C changed (v1:c2)
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:b1",
            canonical_fingerprint="v1:c2",
            state_record=state,
        )
        op = plan_single_skill(self.target, desired_copy, obs)
        self.assertEqual(op.rule_id, "INV-TR-08")
        self.assertEqual(op.action, "UPDATE")
        self.assertTrue(op.is_authorized)
        self.assertFalse(op.requires_force)

    def test_fix_tr_09_copy_drifted_conflict_and_force(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c2"
        )
        state = SkillStateRecord("foo", "copy", "active", "v1:b1", "write", True)
        # R != B, R != C
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:drifted",
            canonical_fingerprint="v1:c2",
            state_record=state,
        )
        op_no_force = plan_single_skill(self.target, desired_copy, obs, force=False)
        self.assertEqual(op_no_force.rule_id, "INV-TR-09")
        self.assertEqual(op_no_force.action, "CONFLICT")
        self.assertTrue(op_no_force.requires_force)

        op_force = plan_single_skill(self.target, desired_copy, obs, force=True)
        self.assertEqual(op_force.rule_id, "INV-TR-09")
        self.assertEqual(op_force.action, "UPDATE")
        self.assertTrue(op_force.is_authorized)
        self.assertEqual(op_force.force_type, "overwrite_drift")

    def test_fix_tr_10_copy_reconcile_state(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c2"
        )
        state = SkillStateRecord("foo", "copy", "active", "v1:b1", "write", True)
        # R != B, but R == C
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:c2",
            canonical_fingerprint="v1:c2",
            state_record=state,
        )
        op = plan_single_skill(self.target, desired_copy, obs)
        self.assertEqual(op.rule_id, "INV-TR-10")
        self.assertEqual(op.action, "RECONCILE_STATE")
        self.assertEqual(op.next_baseline_origin, "reconcile")
        self.assertTrue(op.is_authorized)

    def test_fix_tr_11_unmanaged_copy_claim_state(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        # No state, R == C
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:c1",
            canonical_fingerprint="v1:c1",
            state_record=None,
        )
        op_no_force = plan_single_skill(self.target, desired_copy, obs, force=False)
        self.assertEqual(op_no_force.rule_id, "INV-TR-11")
        self.assertEqual(op_no_force.action, "NOOP")

        op_force = plan_single_skill(self.target, desired_copy, obs, force=True)
        self.assertEqual(op_force.rule_id, "INV-TR-11")
        self.assertEqual(op_force.action, "CLAIM_STATE")
        self.assertEqual(op_force.force_type, "claim_state")
        self.assertEqual(op_force.next_baseline_origin, "claim")

    def test_fix_tr_12_unmanaged_dir_conflict_and_force(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        # No state, R != C
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:other",
            canonical_fingerprint="v1:c1",
            state_record=None,
        )
        op_no_force = plan_single_skill(self.target, desired_copy, obs, force=False)
        self.assertEqual(op_no_force.rule_id, "INV-TR-12")
        self.assertEqual(op_no_force.action, "CONFLICT")

        op_force = plan_single_skill(self.target, desired_copy, obs, force=True)
        self.assertEqual(op_force.rule_id, "INV-TR-12")
        self.assertEqual(op_force.action, "UPDATE")
        self.assertEqual(op_force.force_type, "overwrite_unmanaged")

    def test_fix_tr_13_corrupt_state_cannot_bypass_force(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        obs = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:c1",
            canonical_fingerprint="v1:c1",
            state_error="JSON decode error",
        )
        op = plan_single_skill(self.target, desired_copy, obs, force=True)
        self.assertEqual(op.rule_id, "INV-TR-13")
        self.assertEqual(op.action, "CONFLICT")
        self.assertFalse(op.is_authorized)

    def test_fix_tr_14_and_15_deselected_links(self) -> None:
        desired_absent = DesiredSkill("foo", "absent", self.canon)
        # Owned link
        obs_owned = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            link_points_to_canonical=True,
        )
        op_unlink = plan_single_skill(self.target, desired_absent, obs_owned)
        self.assertEqual(op_unlink.rule_id, "INV-TR-14")
        self.assertEqual(op_unlink.action, "UNLINK")

        # Foreign link
        obs_foreign = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            link_points_to_canonical=False,
        )
        op_preserve = plan_single_skill(self.target, desired_absent, obs_foreign)
        self.assertEqual(op_preserve.rule_id, "INV-TR-15")
        self.assertEqual(op_preserve.action, "NOOP")

    def test_fix_tr_16_and_17_deselected_copies(self) -> None:
        desired_absent = DesiredSkill("foo", "absent", self.canon)
        # Active copy deactivates state, preserves target
        state = SkillStateRecord("foo", "copy", "active", "v1:b1", "write", True)
        obs_active = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:b1",
            state_record=state,
        )
        op_deact = plan_single_skill(self.target, desired_absent, obs_active)
        self.assertEqual(op_deact.rule_id, "INV-TR-16")
        self.assertEqual(op_deact.action, "DEACTIVATE_STATE")
        self.assertEqual(op_deact.next_state_lifecycle, "inactive")

        # Inactive copy remains untouched
        state_inact = SkillStateRecord(
            "foo", "copy", "inactive", "v1:b1", "write", False
        )
        obs_inact = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:b1",
            state_record=state_inact,
        )
        op_noop = plan_single_skill(self.target, desired_absent, obs_inact)
        self.assertEqual(op_noop.rule_id, "INV-TR-17")
        self.assertEqual(op_noop.action, "NOOP")

    def test_fix_tr_18_and_19_reselected_inactive_copies(self) -> None:
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        state_inact = SkillStateRecord(
            "foo", "copy", "inactive", "v1:old", "write", False
        )

        # R == C
        obs_match = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:c1",
            canonical_fingerprint="v1:c1",
            state_record=state_inact,
        )
        op18_force = plan_single_skill(self.target, desired_copy, obs_match, force=True)
        self.assertEqual(op18_force.rule_id, "INV-TR-18")
        self.assertEqual(op18_force.action, "REACTIVATE_STATE")
        self.assertEqual(op18_force.next_baseline_origin, "reactivate")

        # R != C
        obs_diff = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:diff",
            canonical_fingerprint="v1:c1",
            state_record=state_inact,
        )
        op19_force = plan_single_skill(self.target, desired_copy, obs_diff, force=True)
        self.assertEqual(op19_force.rule_id, "INV-TR-19")
        self.assertEqual(op19_force.action, "UPDATE")

    def test_fix_tr_20_mode_switches(self) -> None:
        # Link -> Copy
        desired_copy = DesiredSkill(
            "foo", "copy", self.canon, canonical_fingerprint="v1:c1"
        )
        obs_link = ObservedSkill(
            target=self.target,
            entry_type="symlink",
            link_points_to_canonical=True,
            canonical_valid=True,
            canonical_fingerprint="v1:c1",
        )
        op_l_to_c = plan_single_skill(self.target, desired_copy, obs_link)
        self.assertEqual(op_l_to_c.rule_id, "INV-TR-20")
        self.assertEqual(op_l_to_c.action, "UPDATE")
        self.assertEqual(op_l_to_c.desired_representation, "copy")

        # Copy -> Link (unchanged active)
        desired_link = DesiredSkill("foo", "link", self.canon)
        state_active = SkillStateRecord(
            "foo", "copy", "active", "v1:same", "write", True
        )
        obs_copy = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:same",
            canonical_valid=True,
            state_record=state_active,
        )
        op_c_to_l = plan_single_skill(self.target, desired_link, obs_copy)
        self.assertEqual(op_c_to_l.rule_id, "INV-TR-20")
        self.assertEqual(op_c_to_l.action, "UPDATE")
        self.assertEqual(op_c_to_l.desired_representation, "link")

        # Copy -> Link (drifted copy blocks)
        obs_drifted = ObservedSkill(
            target=self.target,
            entry_type="dir",
            runtime_fingerprint="v1:drifted",
            canonical_valid=True,
            state_record=state_active,
        )
        op_drift_c_to_l = plan_single_skill(
            self.target, desired_link, obs_drifted, force=True
        )
        self.assertEqual(op_drift_c_to_l.rule_id, "INV-TR-20")
        self.assertEqual(op_drift_c_to_l.action, "CONFLICT")
        self.assertFalse(op_drift_c_to_l.is_authorized)
