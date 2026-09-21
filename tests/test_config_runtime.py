"""Unit tests for config_runtime.py.

Verifies:
- INV-CFG-01: Separation of Logical Config Node and Physical File
- INV-CFG-02: Single Pre-Image Multi-Operation File Aggregation and Write-Once Guarantee
- INV-CFG-03: Duplicate and Colliding Logical Key Detection Prior to Write
- INV-CFG-04: Pre-Image Mutation Stale Plan Invalidation
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from aikito.compat import safe_symlink
from aikito.config_runtime import (
    ConfigCollisionError,
    ConfigOperation,
    ConfigTarget,
    FileMutationPlan,
    FileSnapshot,
    StaleConfigPlanError,
    aggregate_file_plans,
    capture_file_snapshot,
    resolve_physical_identity,
)


class ConfigRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name).resolve()

    def tearDown(self) -> None:
        self.td.cleanup()

    def test_resolve_physical_identity_handles_symlink(self) -> None:
        target_file = self.root / "real_config.json"
        target_file.write_text("{}", encoding="utf-8")

        symlink_file = self.root / "link_config.json"
        safe_symlink(target_file, symlink_file)

        id_real = resolve_physical_identity(target_file)
        id_link = resolve_physical_identity(symlink_file)

        self.assertEqual(id_real, id_link)

    def test_file_snapshot_capture_and_validation(self) -> None:
        cfg = self.root / "agent.json"
        cfg.write_text('{"foo": "bar"}', encoding="utf-8")

        snapshot = capture_file_snapshot(cfg, format="json", sensitive=False)
        self.assertTrue(snapshot.exists)
        self.assertIsNotNone(snapshot.content_hash)
        self.assertGreater(snapshot.size_bytes, 0)

        # 1. Unmodified file validates
        valid, msg = snapshot.validate_precondition()
        self.assertTrue(valid)
        self.assertEqual(msg, "")

        # 2. Tampered content fails validation
        cfg.write_text('{"foo": "tampered"}', encoding="utf-8")
        valid_tampered, msg_tampered = snapshot.validate_precondition()
        self.assertFalse(valid_tampered)
        self.assertIn("modified externally", msg_tampered)

        # 3. Deleted file fails validation
        cfg.unlink()
        valid_deleted, msg_deleted = snapshot.validate_precondition()
        self.assertFalse(valid_deleted)
        self.assertIn("missing", msg_deleted)

    def test_missing_file_snapshot_validation(self) -> None:
        nonexistent = self.root / "new_file.json"
        snapshot = capture_file_snapshot(nonexistent, format="json")
        self.assertFalse(snapshot.exists)
        self.assertIsNone(snapshot.content_hash)

        # Valid while still missing
        valid, _ = snapshot.validate_precondition()
        self.assertTrue(valid)

        # Invalid if created externally before apply
        nonexistent.write_text("{}", encoding="utf-8")
        valid_created, msg = snapshot.validate_precondition()
        self.assertFalse(valid_created)
        self.assertIn("now exists", msg)

    def test_file_mutation_plan_aggregation_groups_by_physical_identity(self) -> None:
        real_file = self.root / "multi.json"
        real_file.write_text("{}", encoding="utf-8")

        link_file = self.root / "multi_link.json"
        safe_symlink(real_file, link_file)

        target1 = ConfigTarget(
            path=real_file,
            logical_identity="server1",
            key_path=("mcpServers", "server1"),
            format="claude_json",
            agent="claude-code",
        )
        target2 = ConfigTarget(
            path=link_file,
            logical_identity="server2",
            key_path=("mcpServers", "server2"),
            format="claude_json",
            agent="claude-code",
        )

        op1 = ConfigOperation(target=target1, action="CREATE", reason="New server 1")
        op2 = ConfigOperation(target=target2, action="UPDATE", reason="Update server 2")

        file_plans = aggregate_file_plans([op1, op2])
        self.assertEqual(len(file_plans), 1)
        fp = file_plans[0]
        self.assertEqual(len(fp.operations), 2)
        self.assertTrue(fp.has_mutations)
        self.assertFalse(fp.has_conflicts)

    def test_duplicate_logical_key_collision_raises_error(self) -> None:
        cfg_file = self.root / "config.json"
        target1 = ConfigTarget(
            path=cfg_file,
            logical_identity="serverA",
            key_path=("mcpServers", "duplicate_key"),
            format="json",
        )
        target2 = ConfigTarget(
            path=cfg_file,
            logical_identity="serverB",
            key_path=("mcpServers", "duplicate_key"),
            format="json",
        )

        op1 = ConfigOperation(target=target1, action="CREATE")
        op2 = ConfigOperation(target=target2, action="UPDATE")

        with self.assertRaises(ConfigCollisionError) as ctx:
            aggregate_file_plans([op1, op2])
        self.assertIn("Duplicate logical key", str(ctx.exception))

    def test_incompatible_formats_for_same_file_raises_error(self) -> None:
        cfg_file = self.root / "config.txt"
        target1 = ConfigTarget(
            path=cfg_file,
            logical_identity="node1",
            key_path=("subagent", "n1"),
            format="json",
        )
        target2 = ConfigTarget(
            path=cfg_file,
            logical_identity="node2",
            key_path=("subagent", "n2"),
            format="yaml",
        )

        op1 = ConfigOperation(target=target1, action="CREATE")
        op2 = ConfigOperation(target=target2, action="CREATE")

        with self.assertRaises(ConfigCollisionError) as ctx:
            aggregate_file_plans([op1, op2])
        self.assertIn("Conflicting formats", str(ctx.exception))

    def test_sensitive_flag_aggregation(self) -> None:
        cfg_file = self.root / "mixed.json"
        target_public = ConfigTarget(
            path=cfg_file,
            logical_identity="public_server",
            key_path=("mcpServers", "public"),
            format="json",
            sensitive=False,
        )
        target_secret = ConfigTarget(
            path=cfg_file,
            logical_identity="secret_server",
            key_path=("mcpServers", "secret"),
            format="json",
            sensitive=True,
        )

        op1 = ConfigOperation(target=target_public, action="CREATE")
        op2 = ConfigOperation(target=target_secret, action="CREATE")

        file_plans = aggregate_file_plans([op1, op2])
        self.assertEqual(len(file_plans), 1)
        self.assertTrue(file_plans[0].sensitive)

    def test_stale_precondition_raises_stale_plan_error(self) -> None:
        cfg_file = self.root / "stale.json"
        cfg_file.write_text('{"v": 1}', encoding="utf-8")

        target = ConfigTarget(
            path=cfg_file,
            logical_identity="item",
            key_path=("item",),
            format="json",
        )
        op = ConfigOperation(target=target, action="UPDATE")

        file_plans = aggregate_file_plans([op])
        self.assertEqual(len(file_plans), 1)
        fp = file_plans[0]

        # External change occurs
        cfg_file.write_text('{"v": 2}', encoding="utf-8")

        with self.assertRaises(StaleConfigPlanError):
            fp.validate_precondition()
