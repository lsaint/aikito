"""Unit tests for skill_runtime: staging, secondary checks, execution, and rollback."""

from __future__ import annotations

import hashlib
import shutil
import tempfile
from pathlib import Path
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from aikito.compat import safe_symlink
from aikito.skill_plan import (
    CandidatePathCAS,
    SkillOperation,
    SkillTarget,
    build_skill_plan,
)
from aikito.skill_runtime import (
    execute_selection_transaction,
    execute_skill_plan,
    inspect_skill_target,
)
from aikito.skill_state import (
    ProjectSkillStateDocument,
    SkillStateRecord,
    SkillTransactionJournal,
    calculate_directory_fingerprint,
    get_transactions_dir,
    load_project_skill_state,
    save_project_skill_state,
    write_transaction_journal,
)


class SkillRuntimeExecutionTests(TestCase):
    def test_link_creation_and_noop(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Test\n", encoding="utf-8")

            target_path = co / ".agents" / "skills" / "my-skill"
            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)

            obs, desired = inspect_skill_target(target, "link", home)
            self.assertEqual(obs.entry_type, "missing")

            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create link",
                desired_representation="link",
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Dry-run does not create link
            res_dry = execute_skill_plan(plan, home, dry_run=True)
            self.assertTrue(res_dry.is_success)
            self.assertEqual(res_dry.content_changes, 1)
            self.assertFalse(target_path.exists())

            # Real run creates link
            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertTrue(res.is_success)
            self.assertTrue(target_path.is_symlink())

            # Re-inspecting should now be NOOP
            obs2, desired2 = inspect_skill_target(target, "link", home)
            self.assertTrue(obs2.link_points_to_canonical)

    def test_copy_creation_updates_state_and_secondary_check(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "copied-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Copied\n", encoding="utf-8")
            c_fp, _ = calculate_directory_fingerprint(skill_canon)

            target_path = co / ".agents" / "skills" / "copied-skill"
            target = SkillTarget(ws, "ws", "demo", co, "copied-skill", target_path)

            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create copy",
                desired_representation="copy",
                desired_fingerprint=c_fp,
                next_state_lifecycle="active",
                next_baseline_origin="write",
            )
            plan = build_skill_plan(ws, "demo", [op])

            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertTrue(res.is_success)
            self.assertTrue(target_path.is_dir())
            self.assertFalse(target_path.is_symlink())
            self.assertEqual(
                (target_path / "SKILL.md").read_text(encoding="utf-8"), "# Copied\n"
            )

            # Verify state document was committed
            doc, err = load_project_skill_state(home, ws, "demo", co)
            self.assertIsNone(err)
            self.assertIsNotNone(doc)
            self.assertIn("copied-skill", doc.records)
            rec = doc.records["copied-skill"]
            self.assertEqual(rec.lifecycle, "active")
            self.assertEqual(rec.baseline_fingerprint, c_fp)
            self.assertEqual(rec.baseline_origin, "write")

    def test_staging_detects_canonical_modification_and_aborts(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "modified-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Initial\n", encoding="utf-8")
            initial_fp, _ = calculate_directory_fingerprint(skill_canon)

            target_path = co / ".agents" / "skills" / "modified-skill"
            target = SkillTarget(ws, "ws", "demo", co, "modified-skill", target_path)

            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create copy",
                desired_representation="copy",
                desired_fingerprint=initial_fp,
                next_state_lifecycle="active",
                next_baseline_origin="write",
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Now externally modify the canonical skill before apply
            (skill_canon / "SKILL.md").write_text(
                "# Changed concurrently\n", encoding="utf-8"
            )

            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertFalse(res.is_success)
            self.assertIn(
                "Secondary canonical verification failed", res.error_message or ""
            )
            # Target must not have been created
            self.assertFalse(target_path.exists())

    def test_cas_registration_success_and_concurrent_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            cfg = ws / "projects" / "demo" / "agent.toml"
            cfg.parent.mkdir(parents=True)
            orig_text = 'name = "demo"\nskills = []\n'
            cfg.write_text(orig_text, encoding="utf-8")
            orig_bytes = orig_text.encode("utf-8")

            post_text = 'name = "demo"\nskills = []\npath = "~/checkout"\n'
            post_bytes = post_text.encode("utf-8")

            import hashlib

            cas = CandidatePathCAS(
                config_path=cfg,
                pre_image_bytes=orig_bytes,
                pre_image_hash=hashlib.sha256(orig_bytes).hexdigest(),
                post_image_bytes=post_bytes,
                post_image_hash=hashlib.sha256(post_bytes).hexdigest(),
                is_noop=False,
            )

            plan = build_skill_plan(ws, "demo", [], config_cas=cas)
            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertTrue(res.is_success)
            self.assertEqual(cfg.read_text(encoding="utf-8"), post_text)

            # Concurrent modification abort
            cfg.write_text('name = "demo"\nmodified = true\n', encoding="utf-8")
            res2 = execute_skill_plan(plan, home, dry_run=False)
            self.assertFalse(res2.is_success)
            self.assertIn(
                "Concurrent configuration modification detected",
                res2.error_message or "",
            )

    def test_executor_verifies_expected_fingerprint_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")
            c_fp, _ = calculate_directory_fingerprint(skill_canon)

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.mkdir(parents=True)
            (target_path / "SKILL.md").write_text("# Initial\n", encoding="utf-8")
            r_fp, _ = calculate_directory_fingerprint(target_path)

            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)
            op = SkillOperation(
                action="UPDATE",
                rule_id="INV-TR-08",
                target=target,
                reason="update copy",
                expected_representation="copy",
                desired_representation="copy",
                expected_fingerprint=r_fp,
                desired_fingerprint=c_fp,
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Concurrently modify target directory before execute
            (target_path / "SKILL.md").write_text(
                "# Concurrent local edit\n", encoding="utf-8"
            )

            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertFalse(res.is_success)
            self.assertIn("changed since plan", res.error_message or "")
            self.assertEqual(
                (target_path / "SKILL.md").read_text(encoding="utf-8"),
                "# Concurrent local edit\n",
            )

    def test_copy_to_link_mode_switch_staged_and_restored_on_failure(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.mkdir(parents=True)
            (target_path / "SKILL.md").write_text("# Existing copy\n", encoding="utf-8")

            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)
            op = SkillOperation(
                action="UPDATE",
                rule_id="INV-TR-20",
                target=target,
                reason="switch copy to link",
                expected_representation="copy",
                desired_representation="link",
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Simulate symlink failure during mode switch
            with patch("aikito.skill_runtime.safe_symlink", return_value=False):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertIn("Failed to create symlink", res.error_message or "")
                # Original copy must be preserved / restored!
                self.assertTrue(target_path.is_dir())
                self.assertEqual(
                    (target_path / "SKILL.md").read_text(encoding="utf-8"),
                    "# Existing copy\n",
                )

    def test_journal_write_failure_aborts_before_directory_replacement(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")
            c_fp, _ = calculate_directory_fingerprint(skill_canon)

            target_path = co / ".agents" / "skills" / "my-skill"
            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)
            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create copy",
                desired_representation="copy",
                desired_fingerprint=c_fp,
            )
            plan = build_skill_plan(ws, "demo", [op])

            with patch(
                "aikito.skill_runtime.write_transaction_journal",
                return_value=(None, "Disk read-only"),
            ):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertIn(
                    "Cannot persist transaction journal", res.error_message or ""
                )
                self.assertFalse(target_path.exists())

    def test_executor_verifies_expected_representation_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")

            target_path = co / ".agents" / "skills" / "my-skill"
            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)

            # Planned when missing, but becomes a regular file before execution
            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create link",
                expected_representation="missing",
                desired_representation="link",
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Concurrently create a regular file at target_path
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text("rogue file", encoding="utf-8")

            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertFalse(res.is_success)
            self.assertIn(
                "Target representation changed since plan", res.error_message or ""
            )
            self.assertEqual(target_path.read_text(encoding="utf-8"), "rogue file")

    def test_executor_verifies_expected_generation_precondition(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")
            c_fp, _ = calculate_directory_fingerprint(skill_canon)

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.mkdir(parents=True)
            (target_path / "SKILL.md").write_text("# Initial\n", encoding="utf-8")
            r_fp, _ = calculate_directory_fingerprint(target_path)

            # Seed state doc at generation 1
            init_doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=ws.as_posix(),
                project_name="demo",
                physical_checkout=co.as_posix(),
                records={},
            )
            save_project_skill_state(home, init_doc)

            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)
            op = SkillOperation(
                action="UPDATE",
                rule_id="INV-TR-08",
                target=target,
                reason="update copy",
                expected_representation="copy",
                desired_representation="copy",
                expected_fingerprint=r_fp,
                desired_fingerprint=c_fp,
                expected_generation=1,
            )
            plan = build_skill_plan(ws, "demo", [op])

            # Concurrently advance generation to 2
            init_doc.generation = 2
            save_project_skill_state(home, init_doc)

            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertFalse(res.is_success)
            self.assertIn("State document changed since plan", res.error_message or "")

    def test_copy_to_link_mode_switch_restore_failure_preserves_backup_and_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            skill_canon = ws / "skills" / "my-skill"
            skill_canon.mkdir(parents=True)
            (skill_canon / "SKILL.md").write_text("# Canon\n", encoding="utf-8")

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.mkdir(parents=True)
            (target_path / "SKILL.md").write_text("# Existing copy\n", encoding="utf-8")

            target = SkillTarget(ws, "ws", "demo", co, "my-skill", target_path)
            op = SkillOperation(
                action="UPDATE",
                rule_id="INV-TR-20",
                target=target,
                reason="switch copy to link",
                expected_representation="copy",
                desired_representation="link",
            )
            plan = build_skill_plan(ws, "demo", [op])

            stage_done = False
            orig_replace = Path.replace

            def failing_replace(self: Path, target: Path | str) -> Path:
                nonlocal stage_done
                if not stage_done:
                    stage_done = True
                    return orig_replace(self, target)
                raise OSError("Simulated restore replace failure")

            with patch("aikito.skill_runtime.safe_symlink", return_value=False):
                with patch.object(Path, "replace", failing_replace):
                    res = execute_skill_plan(plan, home, dry_run=False)
                    self.assertFalse(res.is_success)
                    self.assertTrue(res.recovery_required)
                    tx_dir = get_transactions_dir(home)
                    self.assertTrue(tx_dir.exists())
                    journals = list(tx_dir.glob("*/journal.json"))
                    self.assertGreater(len(journals), 0)

    def test_executor_aborts_on_hard_recovery_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            plan = build_skill_plan(ws, "demo", [])
            with patch(
                "aikito.skill_runtime.run_recovery_pass",
                return_value=(False, "Corrupted journal found"),
            ):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertTrue(res.recovery_required)
                self.assertIn(
                    "Recovery failed: Corrupted journal found", res.error_message or ""
                )

    def test_candidate_path_cas_not_written_if_skill_op_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            co = root / "checkout"
            home.mkdir()
            ws.mkdir()
            co.mkdir()

            cfg = ws / "projects" / "demo" / "agent.toml"
            cfg.parent.mkdir(parents=True)
            pre_text = 'name = "demo"\n'
            cfg.write_text(pre_text, encoding="utf-8")
            pre_bytes = pre_text.encode("utf-8")
            post_text = f'name = "demo"\npath = "{co.as_posix()}"\n'
            post_bytes = post_text.encode("utf-8")

            cas = CandidatePathCAS(
                config_path=cfg,
                pre_image_bytes=pre_bytes,
                pre_image_hash=hashlib.sha256(pre_bytes).hexdigest(),
                post_image_bytes=post_bytes,
                post_image_hash=hashlib.sha256(post_bytes).hexdigest(),
                is_noop=False,
            )

            # Skill operation that will fail
            target_path = co / ".agents" / "skills" / "failing-skill"
            target = SkillTarget(ws, "ws", "demo", co, "failing-skill", target_path)
            op = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=target,
                reason="create link",
                expected_representation="missing",
                desired_representation="link",
            )
            plan = build_skill_plan(ws, "demo", [op], config_cas=cas)

            with patch("aikito.skill_runtime.safe_symlink", return_value=False):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                # agent.toml MUST NOT have been updated
                self.assertEqual(cfg.read_text(encoding="utf-8"), pre_text)


class SelectionMutationTransactionTests(TestCase):
    def test_selection_transaction_stops_after_recovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            config = ws / "projects" / "p1" / "agent.toml"
            config.parent.mkdir(parents=True)
            config.write_text('name = "p1"\n', encoding="utf-8")

            with patch(
                "aikito.skill_runtime.run_recovery_pass",
                return_value=(True, "Rolled back interrupted transaction tx-old"),
            ):
                ok, error = execute_selection_transaction(
                    home,
                    ws,
                    ["p1"],
                    [(config, 'name = "p1"\n', 'name = "p1"\nskills = []\n')],
                )

            self.assertFalse(ok)
            self.assertIn("Please re-run command", error or "")
            self.assertEqual(config.read_text(encoding="utf-8"), 'name = "p1"\n')

    def test_selection_rollback_preserves_concurrent_canonical_replacement(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            canonical = ws / "skills" / "demo-skill"
            canonical.mkdir(parents=True)
            (canonical / "SKILL.md").write_text("original", encoding="utf-8")
            original_write = write_transaction_journal

            def fail_commit(h: Path, journal: SkillTransactionJournal):
                if journal.phase == "committed":
                    canonical.mkdir(parents=True)
                    (canonical / "SKILL.md").write_text(
                        "concurrent replacement", encoding="utf-8"
                    )
                    return None, "simulated commit failure"
                return original_write(h, journal)

            with patch(
                "aikito.skill_runtime.write_transaction_journal",
                side_effect=fail_commit,
            ):
                ok, error = execute_selection_transaction(
                    home,
                    ws,
                    [],
                    [],
                    canonical_backup_update=(canonical, root / "unused-backup"),
                )

            self.assertFalse(ok)
            self.assertIn("rollback incomplete", error or "")
            self.assertEqual(
                (canonical / "SKILL.md").read_text(encoding="utf-8"),
                "concurrent replacement",
            )
            self.assertTrue(any(get_transactions_dir(home).glob("*/journal.json")))

    def test_selection_transaction_success_and_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            p1.write_text("p1-orig", encoding="utf-8")

            # Successful transaction
            ok, err = execute_selection_transaction(
                home, ws, ["p1"], [(p1, "p1-orig", "p1-new")]
            )
            self.assertTrue(ok)
            self.assertEqual(p1.read_text(encoding="utf-8"), "p1-new")

            # Failed transaction rolls back
            with patch("pathlib.Path.write_text", side_effect=OSError("Disk error")):
                ok2, err2 = execute_selection_transaction(
                    home, ws, ["p1"], [(p1, "p1-new", "p1-corrupted")]
                )
                self.assertFalse(ok2)
                # p1 must be rolled back to p1-new
                self.assertEqual(p1.read_text(encoding="utf-8"), "p1-new")

    def test_selection_transaction_propagates_state_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            repo = home / "repo"
            repo.mkdir(parents=True)

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            p1.write_text(
                f'name = "p1"\npath = "{repo.as_posix()}"\n', encoding="utf-8"
            )

            # Seed an active state record for some-skill
            init_doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=ws.as_posix(),
                project_name="p1",
                physical_checkout=repo.as_posix(),
                records={
                    "some-skill": SkillStateRecord(
                        skill_name="some-skill",
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint="fp1",
                        baseline_origin="write",
                        last_observed_selected=True,
                    )
                },
            )
            save_project_skill_state(home, init_doc)

            # Mock save_project_skill_state to fail during execution
            with patch(
                "aikito.skill_runtime.save_project_skill_state",
                return_value=(False, "State disk full"),
            ):
                ok, err = execute_selection_transaction(
                    home,
                    ws,
                    ["p1"],
                    [
                        (
                            p1,
                            f'name = "p1"\npath = "{repo.as_posix()}"\n',
                            f'name = "p1"\npath = "{repo.as_posix()}"\n# modified\n',
                        )
                    ],
                    deactivate_skills=["some-skill"],
                )
                self.assertFalse(ok)
                self.assertIn("State save failed", err or "")
                # File must be rolled back to original
                self.assertEqual(
                    p1.read_text(encoding="utf-8"),
                    f'name = "p1"\npath = "{repo.as_posix()}"\n',
                )

    def test_selection_transaction_pre_image_cas_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            p1.write_text("actual-content-does-not-match-pre", encoding="utf-8")

            ok, err = execute_selection_transaction(
                home, ws, ["p1"], [(p1, "expected-pre-image", "new-content")]
            )
            self.assertFalse(ok)
            self.assertIn("Concurrent modification detected", err or "")
            self.assertEqual(
                p1.read_text(encoding="utf-8"), "actual-content-does-not-match-pre"
            )

    def test_selection_transaction_concurrent_modification_avoids_clobber_on_rollback(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            repo = home / "repo"
            repo.mkdir(parents=True)

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            orig_toml = f'name = "p1"\npath = "{repo.as_posix()}"\n'
            p1.write_text(orig_toml, encoding="utf-8")

            # Seed an active state record for some-skill
            init_doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=ws.as_posix(),
                project_name="p1",
                physical_checkout=repo.as_posix(),
                records={
                    "some-skill": SkillStateRecord(
                        skill_name="some-skill",
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint="fp1",
                        baseline_origin="write",
                        last_observed_selected=True,
                    )
                },
            )
            save_project_skill_state(home, init_doc)

            new_toml = orig_toml + "# modified\n"

            def failing_save(*args: Any, **kwargs: Any) -> tuple[bool, str]:
                p1.write_text("p1-concurrently-written", encoding="utf-8")
                return False, "Simulated state error"

            with patch(
                "aikito.skill_runtime.save_project_skill_state",
                side_effect=failing_save,
            ):
                ok, err = execute_selection_transaction(
                    home,
                    ws,
                    ["p1"],
                    [(p1, orig_toml, new_toml)],
                    deactivate_skills=["some-skill"],
                )
                self.assertFalse(ok)
                # p1 should NOT be overwritten back to orig_toml because it was modified concurrently
                self.assertEqual(
                    p1.read_text(encoding="utf-8"), "p1-concurrently-written"
                )

    def test_multiple_copy_ops_same_plan_sequential_generation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            co = root / "checkout"
            co.mkdir()

            s1_src = ws / "skills" / "skill-one"
            s1_src.mkdir(parents=True)
            (s1_src / "SKILL.md").write_text("s1", encoding="utf-8")
            s1_fp, _ = calculate_directory_fingerprint(s1_src)

            s2_src = ws / "skills" / "skill-two"
            s2_src.mkdir(parents=True)
            (s2_src / "SKILL.md").write_text("s2", encoding="utf-8")
            s2_fp, _ = calculate_directory_fingerprint(s2_src)

            t1 = SkillTarget(
                ws,
                "ws",
                "demo",
                co,
                "skill-one",
                co / ".agents" / "skills" / "skill-one",
            )
            op1 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t1,
                reason="create s1 copy",
                expected_representation="missing",
                desired_representation="copy",
                desired_fingerprint=s1_fp,
                expected_generation=0,
            )

            t2 = SkillTarget(
                ws,
                "ws",
                "demo",
                co,
                "skill-two",
                co / ".agents" / "skills" / "skill-two",
            )
            op2 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t2,
                reason="create s2 copy",
                expected_representation="missing",
                desired_representation="copy",
                desired_fingerprint=s2_fp,
                expected_generation=0,
            )

            plan = build_skill_plan(ws, "demo", [op1, op2])
            res = execute_skill_plan(plan, home, dry_run=False)
            self.assertTrue(res.is_success)
            self.assertEqual(len(res.applied_ops), 2)

            doc, _ = load_project_skill_state(home, ws, "demo", co)
            self.assertIsNotNone(doc)
            self.assertEqual(doc.generation, 2)
            self.assertIn("skill-one", doc.records)
            self.assertIn("skill-two", doc.records)

    def test_plan_preserves_prior_commit_on_later_failure_with_config_cas(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            co = root / "checkout"
            co.mkdir()

            cfg = ws / "projects" / "demo" / "agent.toml"
            cfg.parent.mkdir(parents=True)
            pre_text = 'name = "demo"\n'
            cfg.write_text(pre_text, encoding="utf-8")
            pre_bytes = pre_text.encode("utf-8")
            post_text = f'name = "demo"\npath = "{co.as_posix()}"\n'
            post_bytes = post_text.encode("utf-8")

            cas = CandidatePathCAS(
                config_path=cfg,
                pre_image_bytes=pre_bytes,
                pre_image_hash=hashlib.sha256(pre_bytes).hexdigest(),
                post_image_bytes=post_bytes,
                post_image_hash=hashlib.sha256(post_bytes).hexdigest(),
                is_noop=False,
            )

            s1_src = ws / "skills" / "skill-a"
            s1_src.mkdir(parents=True)
            (s1_src / "SKILL.md").write_text("ok", encoding="utf-8")

            s2_src = ws / "skills" / "skill-z"
            s2_src.mkdir(parents=True)
            (s2_src / "SKILL.md").write_text("fail", encoding="utf-8")

            t1 = SkillTarget(
                ws, "ws", "demo", co, "skill-a", co / ".agents" / "skills" / "skill-a"
            )
            op1 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t1,
                reason="create s1 link",
                expected_representation="missing",
                desired_representation="link",
            )

            t2 = SkillTarget(
                ws, "ws", "demo", co, "skill-z", co / ".agents" / "skills" / "skill-z"
            )
            op2 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t2,
                reason="create s2 link",
                expected_representation="missing",
                desired_representation="link",
            )

            plan = build_skill_plan(ws, "demo", [op1, op2], config_cas=cas)

            orig_symlink = safe_symlink

            def partial_symlink(src: Path, dest: Path) -> bool:
                if "skill-z" in str(dest):
                    return False
                return orig_symlink(src, dest)

            with patch(
                "aikito.skill_runtime.safe_symlink", side_effect=partial_symlink
            ):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertEqual(len(res.applied_ops), 1)
                self.assertEqual(res.applied_ops[0].target.skill_name, "skill-a")
                self.assertEqual(res.rolled_back_ops, ())
                self.assertTrue(t1.target_path.is_symlink())
                self.assertEqual(cfg.read_text(encoding="utf-8"), post_text)

    def test_unlink_commit_is_preserved_when_later_operation_fails(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            co = root / "checkout"
            co.mkdir()

            canon_source = ws / "skills" / "a-skill-unlink"
            canon_source.mkdir(parents=True)
            (canon_source / "SKILL.md").write_text("canon", encoding="utf-8")

            other_source = ws / "skills" / "other-skill"
            other_source.mkdir(parents=True)
            (other_source / "SKILL.md").write_text("other", encoding="utf-8")

            t1 = SkillTarget(
                ws,
                "ws",
                "demo",
                co,
                "a-skill-unlink",
                co / ".agents" / "skills" / "a-skill-unlink",
            )
            t1.target_path.parent.mkdir(parents=True, exist_ok=True)
            safe_symlink(other_source, t1.target_path)
            self.assertTrue(t1.target_path.is_symlink())

            op1 = SkillOperation(
                action="UNLINK",
                rule_id="INV-TR-05",
                target=t1,
                reason="unlink skill",
                expected_representation="link",
            )

            fail_canon = ws / "skills" / "z-skill-failing"
            fail_canon.mkdir(parents=True)
            (fail_canon / "SKILL.md").write_text("failing canon", encoding="utf-8")

            t2 = SkillTarget(
                ws,
                "ws",
                "demo",
                co,
                "z-skill-failing",
                co / ".agents" / "skills" / "z-skill-failing",
            )
            op2 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t2,
                reason="create link",
                expected_representation="missing",
                desired_representation="link",
            )

            plan = build_skill_plan(ws, "demo", [op1, op2])

            orig_symlink = safe_symlink

            def partial_symlink(src: Path, dest: Path) -> bool:
                if "z-skill-failing" in str(dest):
                    return False
                return orig_symlink(src, dest)

            with patch(
                "aikito.skill_runtime.safe_symlink", side_effect=partial_symlink
            ):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertEqual(res.rolled_back_ops, ())
                self.assertEqual(len(res.applied_ops), 1)
                self.assertEqual(res.applied_ops[0].target.skill_name, "a-skill-unlink")
                self.assertFalse(t1.target_path.exists())

    def test_committed_journal_cleans_up_leftovers_without_rollback(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            co = root / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-committed-test"
            tx_checkout_root = co / ".agents" / ".aikito-tx" / tx_id
            stage_dir = tx_checkout_root / "stage-my-skill"
            stage_dir.parent.mkdir(parents=True)
            stage_dir.mkdir()
            (stage_dir / "SKILL.md").write_text("stage", encoding="utf-8")

            rec_dir = tx_checkout_root / "prev-my-skill"
            rec_dir.mkdir()
            (rec_dir / "SKILL.md").write_text("prev", encoding="utf-8")

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.mkdir()
            (target_path / "SKILL.md").write_text(
                "new committed content", encoding="utf-8"
            )

            from aikito.skill_state import (
                SkillTransactionJournal,
                run_recovery_pass,
                write_transaction_journal,
            )

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="committed",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": str(stage_dir),
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            self.assertIn("Finalized committed transaction", msg or "")
            # target_path must NOT be rolled back
            self.assertEqual(
                (target_path / "SKILL.md").read_text(encoding="utf-8"),
                "new committed content",
            )
            # Leftovers in .aikito-tx must be removed
            self.assertFalse(stage_dir.exists())
            self.assertFalse(rec_dir.exists())
            self.assertFalse(tx_checkout_root.exists())
            # Journal must be removed
            self.assertFalse((get_transactions_dir(home) / tx_id).exists())

    def test_selection_transaction_records_metadata_and_recovers_deactivation(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            repo = home / "repo"
            repo.mkdir(parents=True)

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            orig_toml = f'name = "p1"\npath = "{repo.as_posix()}"\n'
            p1.write_text(orig_toml, encoding="utf-8")

            # Seed state document with generation 2 (save increments generation by 1)
            init_doc = ProjectSkillStateDocument(
                version=1,
                generation=1,
                workspace_root=ws.as_posix(),
                project_name="p1",
                physical_checkout=repo.as_posix(),
                records={
                    "some-skill": SkillStateRecord(
                        skill_name="some-skill",
                        representation="copy",
                        lifecycle="active",
                        baseline_fingerprint="fp1",
                        baseline_origin="write",
                        last_observed_selected=True,
                    )
                },
            )
            save_project_skill_state(home, init_doc)

            captured_journals = []
            orig_write_j = write_transaction_journal

            def capture_journal(h, j):
                captured_journals.append(j)
                return orig_write_j(h, j)

            with patch(
                "aikito.skill_runtime.write_transaction_journal",
                side_effect=capture_journal,
            ):
                ok, err = execute_selection_transaction(
                    home,
                    ws,
                    ["p1"],
                    [(p1, orig_toml, orig_toml + "# update\n")],
                    deactivate_skills=["some-skill"],
                )
                self.assertTrue(ok, f"Selection transaction failed: {err}")

            self.assertTrue(len(captured_journals) >= 2)
            pending_j = captured_journals[0]
            self.assertEqual(
                pending_j.checkout_paths, [repo.resolve(strict=False).as_posix()]
            )
            self.assertEqual(len(pending_j.state_transitions), 1)
            st = pending_j.state_transitions[0]
            self.assertEqual(st["workspace_root"], ws.as_posix())
            self.assertEqual(st["project_name"], "p1")
            self.assertEqual(
                st["physical_checkout"], repo.resolve(strict=False).as_posix()
            )
            self.assertEqual(st["pre_generation"], 2)
            self.assertEqual(len(st["post_docs"]), 1)
            self.assertEqual(st["post_docs"][0]["generation"], 3)
            self.assertEqual(
                st["post_docs"][0]["records"]["some-skill"]["lifecycle"],
                "inactive",
            )

            # Now simulate crash of an interrupted selection transaction:
            # Re-write state with deactivated state (generation 3) and re-create pending journal
            doc_after, _ = load_project_skill_state(home, ws, "p1", repo)
            self.assertIsNotNone(doc_after)
            self.assertEqual(doc_after.generation, 3)
            self.assertEqual(doc_after.records["some-skill"].lifecycle, "inactive")

            crash_tx_id = "tx-selection-crash"
            pending_j.tx_id = crash_tx_id
            write_transaction_journal(home, pending_j)

            from aikito.skill_state import run_recovery_pass

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["p1"])
            self.assertTrue(recovered, f"Recovery failed: {msg}")

            # State document should be restored to generation 2 and active lifecycle
            doc_restored, _ = load_project_skill_state(home, ws, "p1", repo)
            self.assertIsNotNone(doc_restored)
            self.assertEqual(doc_restored.generation, 2)
            self.assertEqual(doc_restored.records["some-skill"].lifecycle, "active")

    def test_run_skill_plan_committed_cleanup_failure_reports_error(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()
            co = root / "checkout"
            co.mkdir()

            s1_src = ws / "skills" / "s1"
            s1_src.mkdir(parents=True)
            (s1_src / "SKILL.md").write_text("s1", encoding="utf-8")
            s1_fp, _ = calculate_directory_fingerprint(s1_src)

            t1 = SkillTarget(
                ws, "ws", "demo", co, "s1", co / ".agents" / "skills" / "s1"
            )
            op1 = SkillOperation(
                action="CREATE",
                rule_id="INV-TR-01",
                target=t1,
                reason="create s1 copy",
                expected_representation="missing",
                desired_representation="copy",
                desired_fingerprint=s1_fp,
                expected_generation=0,
            )
            plan = build_skill_plan(ws, "demo", [op1])

            orig_rmtree = shutil.rmtree

            def failing_rmtree(path, *args, **kwargs):
                # Fail only when cleaning up active_tx_roots
                if ".aikito-tx" in str(path):
                    raise OSError("Simulated cleanup lock")
                return orig_rmtree(path, *args, **kwargs)

            with patch("shutil.rmtree", side_effect=failing_rmtree):
                res = execute_skill_plan(plan, home, dry_run=False)
                self.assertFalse(res.is_success)
                self.assertTrue(res.recovery_required)
                self.assertIsNotNone(res.error_message)
                self.assertIn("cleanup failed", res.error_message)

    def test_execute_selection_transaction_committed_cleanup_failure_retains_journal(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            home = root / "home"
            ws = root / "workspace"
            home.mkdir()
            ws.mkdir()

            repo = home / "repo"
            repo.mkdir(parents=True)

            p1 = ws / "projects" / "p1" / "agent.toml"
            p1.parent.mkdir(parents=True)
            orig_toml = f'name = "p1"\npath = "{repo.as_posix()}"\n'
            p1.write_text(orig_toml, encoding="utf-8")

            # Mock shutil.rmtree to fail during committed cleanup
            with patch(
                "shutil.rmtree", side_effect=OSError("Permission denied on tx dir")
            ):
                # Provide canonical backup to exercise cleanup
                canon_dir = ws / "skills" / "canon-skill"
                canon_dir.mkdir(parents=True)
                backup_dir = ws / ".aikito-tx" / "backup"

                ok, err = execute_selection_transaction(
                    home,
                    ws,
                    ["p1"],
                    [(p1, orig_toml, orig_toml + "# update\n")],
                    canonical_backup_update=(canon_dir, backup_dir),
                )
                self.assertFalse(ok)
                self.assertIn(
                    "Committed selection transaction cleanup failed", err or ""
                )
