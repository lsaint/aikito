"""Unit tests for skill_state: fingerprints, state store, writer lock, and transactions."""

from __future__ import annotations

import base64
import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest import TestCase

from aikito.skill_state import (
    ProjectSkillStateDocument,
    SkillStateRecord,
    SkillTransactionJournal,
    SkillWriterLock,
    calculate_directory_fingerprint,
    delete_transaction_journal,
    get_binding_hash,
    get_skill_state_dir,
    get_transactions_dir,
    load_project_skill_state,
    run_recovery_pass,
    save_project_skill_state,
    write_transaction_journal,
)


class SkillStateFingerprintTests(TestCase):
    def test_fingerprint_deterministic_and_empty_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "my-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")
            (skill / "sub").mkdir()
            (skill / "sub" / "run.sh").write_text("echo hi\n", encoding="utf-8")
            (skill / "empty_dir").mkdir()

            fp1, err1 = calculate_directory_fingerprint(skill)
            self.assertIsNone(err1)
            self.assertIsNotNone(fp1)
            self.assertTrue(fp1.startswith("v1:"))

            # Recalculating produces identical fingerprint
            fp2, err2 = calculate_directory_fingerprint(skill)
            self.assertEqual(fp1, fp2)

            # Modifying file changes fingerprint
            (skill / "sub" / "run.sh").write_text("echo modified\n", encoding="utf-8")
            fp3, _ = calculate_directory_fingerprint(skill)
            self.assertNotEqual(fp1, fp3)

    def test_fingerprint_rejects_internal_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            skill = root / "my-skill"
            skill.mkdir()
            (skill / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")
            try:
                (skill / "link.txt").symlink_to(skill / "SKILL.md")
            except OSError:
                return  # Skip if symlinks unsupported on platform
            fp, err = calculate_directory_fingerprint(skill)
            self.assertIsNone(fp)
            self.assertIn("Symbolic links are not supported", err or "")

    def test_fingerprint_rejects_symlink_or_reparse_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            real_skill = root / "real-skill"
            real_skill.mkdir()
            (real_skill / "SKILL.md").write_text("content", encoding="utf-8")
            link_skill = root / "link-skill"
            try:
                link_skill.symlink_to(real_skill)
            except OSError:
                return
            fp, err = calculate_directory_fingerprint(link_skill)
            self.assertIsNone(fp)
            self.assertIn("Directory root cannot be a symbolic link", err or "")


class SkillStateStoreTests(TestCase):
    def test_load_nonexistent_returns_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            co = home / "checkout"
            doc, err = load_project_skill_state(home, ws, "demo", co)
            self.assertIsNone(doc)
            self.assertIsNone(err)

    def test_save_and_load_roundtrip(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            co = home / "checkout"
            rec = SkillStateRecord(
                skill_name="test-skill",
                representation="copy",
                lifecycle="active",
                baseline_fingerprint="v1:abc123hash",
                baseline_origin="write",
                last_observed_selected=True,
            )
            doc = ProjectSkillStateDocument(
                version=1,
                generation=0,
                workspace_root=ws.as_posix(),
                project_name="demo",
                physical_checkout=co.as_posix(),
                records={"test-skill": rec},
            )
            ok, err = save_project_skill_state(home, doc)
            self.assertTrue(ok)
            self.assertIsNone(err)

            loaded_doc, load_err = load_project_skill_state(home, ws, "demo", co)
            self.assertIsNone(load_err)
            self.assertIsNotNone(loaded_doc)
            self.assertEqual(loaded_doc.generation, 1)
            self.assertEqual(loaded_doc.project_name, "demo")
            self.assertIn("test-skill", loaded_doc.records)
            self.assertEqual(
                loaded_doc.records["test-skill"].baseline_fingerprint, "v1:abc123hash"
            )

    def test_generation_mismatch_fails_save(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            co = home / "checkout"
            doc = ProjectSkillStateDocument(
                version=1,
                generation=5,
                workspace_root=ws.as_posix(),
                project_name="demo",
                physical_checkout=co.as_posix(),
                records={
                    "s": SkillStateRecord("s", "copy", "active", "v1:a", "write", True)
                },
            )
            save_project_skill_state(home, doc)

            # Saving with wrong expected generation must fail
            ok, err = save_project_skill_state(home, doc, expected_generation=999)
            self.assertFalse(ok)
            self.assertIn("generation mismatch", err or "")

    def test_empty_records_removes_state_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            co = home / "checkout"
            doc = ProjectSkillStateDocument(
                version=1,
                generation=0,
                workspace_root=ws.as_posix(),
                project_name="demo",
                physical_checkout=co.as_posix(),
                records={
                    "s": SkillStateRecord("s", "copy", "active", "v1:a", "write", True)
                },
            )
            save_project_skill_state(home, doc)
            b_hash = get_binding_hash(ws, "demo", co)
            state_file = get_skill_state_dir(home) / f"{b_hash}.json"
            self.assertTrue(state_file.exists())

            # Now save with empty records
            doc.records.clear()
            ok, _ = save_project_skill_state(home, doc)
            self.assertTrue(ok)
            self.assertFalse(state_file.exists())


class SkillWriterLockTests(TestCase):
    def test_writer_lock_reentrancy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            lock = SkillWriterLock(home)
            with lock:
                self.assertEqual(SkillWriterLock._lock_depth, 1)
                # Re-entrant acquire
                with SkillWriterLock(home):
                    self.assertEqual(SkillWriterLock._lock_depth, 2)
                self.assertEqual(SkillWriterLock._lock_depth, 1)
            self.assertEqual(SkillWriterLock._lock_depth, 0)
            self.assertIsNone(SkillWriterLock._lock_file_obj)


class SkillTransactionRecoveryTests(TestCase):
    def test_pending_transaction_rolled_back(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-rollback-test"
            modified_file = co / "test.txt"
            modified_file.write_text("corrupted content\n", encoding="utf-8")

            import base64

            pre_b64 = base64.b64encode(b"original content\n").decode("ascii")

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                files=[{"path": str(modified_file), "pre_image_base64": pre_b64}],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            self.assertIn("Rolled back", msg or "")
            self.assertEqual(
                modified_file.read_text(encoding="utf-8"), "original content\n"
            )

    def test_committed_transaction_finalized(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-committed-test"
            recovery_dir = co / ".agents" / ".aikito-tx" / tx_id / "lingering_recovery"
            recovery_dir.mkdir(parents=True)
            (recovery_dir / "old.txt").write_text("old", encoding="utf-8")

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
                        "recovery_dir": str(recovery_dir),
                        "staging_dir": "",
                        "target_path": "",
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            self.assertIn("Finalized", msg or "")
            self.assertFalse(recovery_dir.exists())

    def test_forged_journal_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            # 1. tx_id mismatch between folder and journal content
            tx_dir = (
                home
                / ".local"
                / "state"
                / "aikito"
                / "project-skills"
                / "transactions"
                / "tx-dir-name"
            )
            tx_dir.mkdir(parents=True)
            journal_path = tx_dir / "journal.json"
            journal_path.write_text(
                json.dumps(
                    {
                        "tx_id": "different-tx-id",
                        "workspace_root": ws.as_posix(),
                        "phase": "pending",
                    }
                ),
                encoding="utf-8",
            )
            ok, err = run_recovery_pass(home, ws)
            self.assertFalse(ok)
            self.assertIn("Transaction journal ID mismatch", err or "")

            # 2. Path escaping sandbox
            shutil.rmtree(tx_dir)
            sensitive_file = home / "sensitive.txt"
            sensitive_file.write_text("precious", encoding="utf-8")
            tx_id = "tx-sandbox-test"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                files=[{"path": str(sensitive_file), "pre_image_base64": ""}],
            )
            write_transaction_journal(home, journal)
            ok2, err2 = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(ok2)
            self.assertIn("escapes sandbox", err2 or "")
            self.assertTrue(sensitive_file.exists())

            # 3. Forged committed journal attempting to delete workspace projects
            delete_transaction_journal(home, tx_id)
            tx_id3 = "tx-sandbox-projects"
            journal3 = SkillTransactionJournal(
                tx_id=tx_id3,
                kind="runtime_apply",
                phase="committed",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": "",
                        "recovery_dir": str(ws / "projects"),
                        "staging_dir": "",
                    }
                ],
            )
            write_transaction_journal(home, journal3)
            ok3, err3 = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(ok3)
            self.assertIn("escapes sandbox", err3 or "")
            self.assertTrue((ws / "projects").exists())

            # 4. Forged checkout path not in project configuration
            delete_transaction_journal(home, tx_id3)
            unauth_co = home / "other_unconfigured_checkout"
            unauth_co.mkdir()
            tx_id4 = "tx-sandbox-unauth-co"
            journal4 = SkillTransactionJournal(
                tx_id=tx_id4,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[unauth_co.as_posix()],
                affected_skills=["my-skill"],
                files=[],
            )
            write_transaction_journal(home, journal4)
            ok4, err4 = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(ok4)
            self.assertIn("unauthorized checkout path", err4 or "")

    def test_concurrent_modified_file_not_clobbered(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-concurrent-test"
            f_path = co / "file.txt"
            f_path.write_text("concurrently edited by other process", encoding="utf-8")

            import base64

            pre_b64 = base64.b64encode(b"initial").decode("ascii")
            post_b64 = base64.b64encode(b"tx_written").decode("ascii")

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                files=[
                    {
                        "path": str(f_path),
                        "pre_image_base64": pre_b64,
                        "post_image_base64": post_b64,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("Concurrent modification detected", msg or "")
            self.assertEqual(
                f_path.read_text(encoding="utf-8"),
                "concurrently edited by other process",
            )
            # Pending journal MUST be preserved for site inspection
            j_dir = get_transactions_dir(home) / tx_id
            self.assertTrue(j_dir.exists())

    def test_forged_journal_files_cannot_authorize_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "unauthorized_checkout"
            co.mkdir()

            import base64

            tx_id = "tx-forged-checkout"
            forged_agent_toml = f'name = "demo"\npath = "{co.as_posix()}"\n'
            forged_b64 = base64.b64encode(forged_agent_toml.encode("utf-8")).decode(
                "ascii"
            )

            # Journal attempts to inject checkout by placing agent.toml in journal.files
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                files=[
                    {
                        "path": str(ws / "projects" / "demo" / "agent.toml"),
                        "pre_image_base64": None,
                        "post_image_base64": forged_b64,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("unauthorized checkout path", msg or "")

    def test_forged_journal_files_with_matching_pre_image_cannot_authorize_checkout(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            valid_co = home / "valid_checkout"
            valid_co.mkdir()
            unauth_co = home / "unauthorized_checkout"
            unauth_co.mkdir()

            # Legitimate project configuration exists on disk
            real_toml = f'name = "demo"\npath = "{valid_co.as_posix()}"\n'
            cfg_file = ws / "projects" / "demo" / "agent.toml"
            cfg_file.parent.mkdir(parents=True)
            cfg_file.write_text(real_toml, encoding="utf-8")

            import base64

            tx_id = "tx-forged-matching-pre"
            forged_agent_toml = f'name = "demo"\npath = "{unauth_co.as_posix()}"\n'
            forged_b64 = base64.b64encode(forged_agent_toml.encode("utf-8")).decode(
                "ascii"
            )
            real_b64 = base64.b64encode(real_toml.encode("utf-8")).decode("ascii")

            # Malicious journal matches pre_image with on-disk agent.toml, but has malicious post_image
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[unauth_co.as_posix()],
                affected_skills=["my-skill"],
                files=[
                    {
                        "path": str(cfg_file),
                        "pre_image_base64": real_b64,
                        "post_image_base64": forged_b64,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("unauthorized checkout path", msg or "")

    def test_link_to_copy_recovery_when_rec_dir_is_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            canon_dir = ws / "skills" / "my-skill"
            canon_dir.mkdir(parents=True)
            (canon_dir / "SKILL.md").write_text("# Canon\n", encoding="utf-8")

            tx_id = "tx-link-copy-rec"
            rec_dir = co / ".agents" / ".aikito-tx" / tx_id / "prev-my-skill"
            rec_dir.parent.mkdir(parents=True)

            # In link->copy, the original symlink pointing to canon_dir was moved to rec_dir
            from aikito.compat import safe_symlink

            safe_symlink(canon_dir, rec_dir)
            self.assertTrue(rec_dir.is_symlink())

            # Target path currently has the new copy directory
            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.mkdir()
            (target_path / "SKILL.md").write_text(
                "# Copied content\n", encoding="utf-8"
            )
            post_fp, _ = calculate_directory_fingerprint(target_path)

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": "",
                        "pre_fingerprint": None,
                        "post_fingerprint": post_fp,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered, f"Recovery failed: {msg}")
            # Target path should now be restored as the original symlink
            self.assertTrue(target_path.is_symlink())
            self.assertEqual(os.readlink(target_path), canon_dir.as_posix())

    def test_copy_to_link_recovery_checks_symlink_target(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-copy-link-check"
            rec_dir = co / ".agents" / ".aikito-tx" / tx_id / "prev-my-skill"
            rec_dir.parent.mkdir(parents=True)
            rec_dir.mkdir()
            (rec_dir / "SKILL.md").write_text("# Original Copy\n", encoding="utf-8")

            canon_dir = ws / "skills" / "my-skill"
            canon_dir.mkdir(parents=True)
            (canon_dir / "SKILL.md").write_text("# Canon\n", encoding="utf-8")

            foreign_dir = home / "foreign_skill"
            foreign_dir.mkdir()

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            from aikito.compat import safe_symlink

            # Point symlink to unexpected foreign location (concurrent modification)
            safe_symlink(foreign_dir, target_path)

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": "",
                        "pre_fingerprint": "prev_fp",
                        "post_fingerprint": None,
                        "expected_symlink_target": str(canon_dir),
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("symlink target mismatch", msg or "")
            self.assertTrue(target_path.is_symlink())
            self.assertEqual(os.readlink(target_path), foreign_dir.as_posix())

    def test_multi_skill_transaction_crash_recovers_state(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            b_hash = get_binding_hash(ws, "demo", co)
            st_file = get_skill_state_dir(home) / f"{b_hash}.json"
            st_file.parent.mkdir(parents=True, exist_ok=True)

            # Initial state doc before transaction had generation 1
            orig_doc = {
                "version": 1,
                "generation": 1,
                "workspace_root": ws.as_posix(),
                "project_name": "demo",
                "physical_checkout": co.as_posix(),
                "records": {},
            }
            # Transaction applied 2 skills before crashing, bumping generation to 3
            crashed_doc = dict(orig_doc)
            crashed_doc["generation"] = 3
            st_file.write_text(json.dumps(crashed_doc), encoding="utf-8")

            tx_id = "tx-multi-skill-crash"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["skill1", "skill2"],
                state_transitions=[
                    {
                        "binding_hash": b_hash,
                        "workspace_root": ws.as_posix(),
                        "project_name": "demo",
                        "physical_checkout": co.as_posix(),
                        "pre_doc": orig_doc,
                        "pre_generation": 1,
                        "post_docs": [crashed_doc],
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered, f"Recovery failed: {msg}")
            restored = json.loads(st_file.read_text(encoding="utf-8"))
            self.assertEqual(restored["generation"], 1)

    def test_state_transition_missing_identity_metadata_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            b_hash = get_binding_hash(ws, "demo", co)
            tx_id = "tx-missing-id"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["skill"],
                state_transitions=[
                    {
                        "binding_hash": b_hash,
                        # Missing workspace_root, project_name, physical_checkout
                        "pre_doc": None,
                        "pre_generation": 0,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("missing identity metadata", msg or "")

    def test_copy_to_link_recovery_with_existing_symlink(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-copy-link-rec"
            rec_dir = co / ".agents" / ".aikito-tx" / tx_id / "prev-my-skill"
            rec_dir.parent.mkdir(parents=True)
            rec_dir.mkdir()
            (rec_dir / "SKILL.md").write_text(
                "# Original Copy Content\n", encoding="utf-8"
            )

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            # Create a symlink pointing to an arbitrary canon directory
            canon_dir = ws / "skills" / "my-skill"
            canon_dir.mkdir(parents=True)
            (canon_dir / "SKILL.md").write_text("# Canon\n", encoding="utf-8")
            from aikito.compat import safe_symlink

            safe_symlink(canon_dir, target_path)
            self.assertTrue(target_path.is_symlink())

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": "",
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            # Target must now be a real directory restored from rec_dir, NOT a symlink
            self.assertFalse(target_path.is_symlink())
            self.assertTrue(target_path.is_dir())
            self.assertEqual(
                (target_path / "SKILL.md").read_text(encoding="utf-8"),
                "# Original Copy Content\n",
            )

    def test_symlink_recovery_and_concurrent_abort(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            from aikito.compat import safe_symlink

            tx_id = "tx-symlink-rec"
            target_path = co / ".agents" / "skills" / "linked-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)

            canon_dir = ws / "skills" / "linked-skill"
            canon_dir.mkdir(parents=True)

            # Transaction created symlink pointing to canon_dir
            safe_symlink(canon_dir, target_path)

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["linked-skill"],
                symlinks=[
                    {
                        "target_path": str(target_path),
                        "pre_link": None,
                        "post_link": str(canon_dir),
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            # CREATE link should be rolled back to pre_link=None (unlinked)
            self.assertFalse(target_path.exists())
            self.assertFalse(target_path.is_symlink())

    def test_create_copy_interrupted_recovery_cleans_up_unmanaged_copy(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-create-copy-rec"
            target_path = co / ".agents" / "skills" / "new-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.mkdir()
            (target_path / "SKILL.md").write_text(
                "# Interrupted newly created copy\n", encoding="utf-8"
            )

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["new-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": "",
                        "staging_dir": "",
                        "created": True,
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            # Newly created target copy directory must be cleaned up!
            self.assertFalse(target_path.exists())

    def test_recovery_authorizes_candidate_checkout_from_frozen_post_image(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co1 = home / "checkout1"
            co2 = home / "checkout2"
            co1.mkdir()
            co2.mkdir()

            demo_dir = ws / "projects" / "demo"
            demo_dir.mkdir(parents=True)
            demo_cfg = demo_dir / "agent.toml"
            pre_content = f'name = "demo"\npath = "{co1.as_posix()}"\n'
            post_content = (
                f'name = "demo"\npaths = ["{co1.as_posix()}", "{co2.as_posix()}"]\n'
            )
            # On disk, pre_content is present (crash before agent.toml was written)
            demo_cfg.write_text(pre_content, encoding="utf-8")

            tx_id = "tx-new-checkout-crash"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co1.as_posix(), co2.as_posix()],
                affected_skills=["skill1"],
                files=[
                    {
                        "path": str(demo_cfg),
                        "pre_image_base64": base64.b64encode(
                            pre_content.encode("utf-8")
                        ).decode("ascii"),
                        "post_image_base64": base64.b64encode(
                            post_content.encode("utf-8")
                        ).decode("ascii"),
                    }
                ],
                state_transitions=[
                    {
                        "binding_hash": get_binding_hash(ws, "demo", co2),
                        "workspace_root": ws.as_posix(),
                        "project_name": "demo",
                        "physical_checkout": co2.as_posix(),
                        "pre_doc": None,
                        "pre_generation": 0,
                        "post_docs": [],
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered, msg)
            self.assertEqual(demo_cfg.read_text(encoding="utf-8"), pre_content)
            self.assertFalse((get_transactions_dir(home) / tx_id).exists())

    def test_recovery_aborts_and_retains_journal_if_copy_target_externally_modified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-copy-external-mod"
            tx_checkout_root = co / ".agents" / ".aikito-tx" / tx_id
            rec_dir = tx_checkout_root / "prev-my-skill"
            rec_dir.parent.mkdir(parents=True)
            rec_dir.mkdir()
            (rec_dir / "SKILL.md").write_text("old baseline", encoding="utf-8")
            old_fp, _ = calculate_directory_fingerprint(rec_dir)

            target_path = co / ".agents" / "skills" / "my-skill"
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.mkdir()
            # User externally modified the directory after crash
            (target_path / "SKILL.md").write_text(
                "user edited content", encoding="utf-8"
            )

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                recovery_dirs=[
                    {
                        "target_path": str(target_path),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": "",
                        "created": False,
                        "pre_fingerprint": old_fp,
                        "post_fingerprint": "v1:desired_fp",
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn(
                "Concurrent modification detected in target directory", msg or ""
            )
            # Journal MUST be retained
            self.assertTrue((get_transactions_dir(home) / tx_id).exists())
            # User edits must NOT be destroyed
            self.assertEqual(
                (target_path / "SKILL.md").read_text(encoding="utf-8"),
                "user edited content",
            )

    def test_recovery_aborts_and_retains_journal_if_file_externally_deleted(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-file-deleted"
            # File was supposed to be updated, but was externally deleted
            f_path = co / ".agents" / "some-file.txt"
            f_path.parent.mkdir(parents=True, exist_ok=True)
            # Ensure it does NOT exist on disk

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                files=[
                    {
                        "path": str(f_path),
                        "pre_image_base64": base64.b64encode(b"old content").decode(
                            "ascii"
                        ),
                        "post_image_base64": base64.b64encode(b"new content").decode(
                            "ascii"
                        ),
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("externally deleted", msg or "")
            self.assertTrue((get_transactions_dir(home) / tx_id).exists())

    def test_recovery_aborts_and_retains_journal_if_state_externally_modified(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            b_hash = get_binding_hash(ws, "demo", co)
            st_file = get_skill_state_dir(home) / f"{b_hash}.json"
            st_file.parent.mkdir(parents=True, exist_ok=True)
            # An external writer reused the transaction's expected generation.
            st_file.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "generation": 2,
                        "records": {},
                        "external": True,
                    }
                ),
                encoding="utf-8",
            )

            tx_id = "tx-state-cas-fail"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                state_transitions=[
                    {
                        "binding_hash": b_hash,
                        "workspace_root": ws.as_posix(),
                        "project_name": "demo",
                        "physical_checkout": co.as_posix(),
                        "pre_doc": {"version": 1, "generation": 1, "records": {}},
                        "pre_generation": 1,
                        "post_docs": [{"version": 1, "generation": 2, "records": {}}],
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertFalse(recovered)
            self.assertIn("Concurrent modification detected in state file", msg or "")
            self.assertTrue((get_transactions_dir(home) / tx_id).exists())

    def test_recovery_cleans_up_new_state_file_when_pre_doc_is_none(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            b_hash = get_binding_hash(ws, "demo", co)
            st_file = get_skill_state_dir(home) / f"{b_hash}.json"
            st_file.parent.mkdir(parents=True, exist_ok=True)
            # Transaction wrote generation 1
            st_file.write_text(
                json.dumps({"version": 1, "generation": 1, "records": {}}),
                encoding="utf-8",
            )

            tx_id = "tx-first-state-none"
            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="pending",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["my-skill"],
                state_transitions=[
                    {
                        "binding_hash": b_hash,
                        "workspace_root": ws.as_posix(),
                        "project_name": "demo",
                        "physical_checkout": co.as_posix(),
                        "pre_doc": None,
                        "pre_generation": 0,
                        "post_docs": [{"version": 1, "generation": 1, "records": {}}],
                    }
                ],
            )
            write_transaction_journal(home, journal)

            recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
            self.assertTrue(recovered)
            # State file should be unlinked completely, not left empty
            self.assertFalse(st_file.exists())

    def test_committed_cleanup_failure_retains_journal(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            ws = home / "workspace"
            ws.mkdir()
            co = home / "checkout"
            co.mkdir()

            demo_cfg = ws / "projects" / "demo" / "agent.toml"
            demo_cfg.parent.mkdir(parents=True)
            demo_cfg.write_text(
                f'name = "demo"\npath = "{co.as_posix()}"\n', encoding="utf-8"
            )

            tx_id = "tx-committed-fail-clean"
            tx_checkout_root = co / ".agents" / ".aikito-tx" / tx_id
            rec_dir = tx_checkout_root / "prev-skill"
            rec_dir.parent.mkdir(parents=True)
            rec_dir.mkdir()

            journal = SkillTransactionJournal(
                tx_id=tx_id,
                kind="runtime_apply",
                phase="committed",
                workspace_root=ws.as_posix(),
                project_names=["demo"],
                checkout_paths=[co.as_posix()],
                affected_skills=["skill"],
                recovery_dirs=[
                    {
                        "target_path": str(co / ".agents" / "skills" / "skill"),
                        "recovery_dir": str(rec_dir),
                        "staging_dir": "",
                    }
                ],
            )
            write_transaction_journal(home, journal)

            from unittest.mock import patch

            with patch("shutil.rmtree", side_effect=OSError("permission denied")):
                recovered, msg = run_recovery_pass(home, ws, affected_projects=["demo"])
                self.assertFalse(recovered)
                self.assertIn("Journal retained for retry", msg or "")
                self.assertTrue((get_transactions_dir(home) / tx_id).exists())
