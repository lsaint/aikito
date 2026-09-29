"""Local state cleanup must preserve live, offline, unsafe, and recoverable bindings."""

from __future__ import annotations

import json

from contextlib import contextmanager
from unittest.mock import patch

import pytest

from aikito.doctor import check_local_state, run_doctor_fixes
from aikito.local_state import clean_local_state, inspect_local_state
from local_state_doctor_smoke import state_file


def test_doctor_reports_stale_files_read_only_and_fix_preserves_other_state(tmp_path):
    home = tmp_path / "home"
    stale = state_file(home, tmp_path / "gone-workspace", tmp_path / "gone-checkout")
    workspace, checkout = tmp_path / "workspace", tmp_path / "checkout"
    workspace.mkdir()
    checkout.mkdir()
    live = state_file(home, workspace, checkout)
    unavailable = state_file(home, workspace, tmp_path / "unavailable-checkout")
    invalid = stale.parent / ("0" * 64 + ".json")
    invalid.write_text("{", encoding="utf-8")
    before = {p: p.read_bytes() for p in (stale, live, unavailable, invalid)}
    section = check_local_state(home)
    finding = next(f for f in section.findings if f.code == "local-state.stale")
    assert finding.source == str(stale)
    assert finding.fix_hint == "aikito doctor --fix"
    assert finding.actions[0].command == finding.fix_hint
    assert all(p.read_bytes() == contents for p, contents in before.items())
    assert not (stale.parent / "writer.lock").exists()
    fixes = run_doctor_fixes(tmp_path / "unused-workspace", home)
    assert any(str(stale) in message for message in fixes)
    assert not stale.exists()
    for path in (live, unavailable, invalid):
        assert path.read_bytes() == before[path]


def test_missing_non_temporary_paths_are_not_cleanup_candidates(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    with patch("aikito.local_state._temporary", return_value=False):
        findings = inspect_local_state(home)
        assert findings[0].code == "local-state.unavailable"
        assert not findings[0].actions
        assert clean_local_state(home) == []
    assert path.exists()


def test_pending_journal_defers_all_state_cleanup(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    journal = path.parent / "transactions" / "pending" / "journal.json"
    journal.parent.mkdir(parents=True)
    journal.write_text("{}", encoding="utf-8")
    findings = inspect_local_state(home)
    assert any(f.code == "local-state.recovery-required" for f in findings)
    assert all(not f.actions for f in findings)
    assert clean_local_state(home) == []
    assert path.exists() and journal.exists()


def test_cleanup_rechecks_paths_after_acquiring_lock(tmp_path):
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    path = state_file(home, workspace, tmp_path / "checkout")

    @contextmanager
    def lock(_home):
        workspace.mkdir()
        yield

    with patch("aikito.local_state.WorkspaceWriterLock", lock):
        assert clean_local_state(home) == []
    assert path.exists()


def test_state_symlink_and_mismatched_binding_are_preserved(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    wrong = path.with_name("1" * 64 + ".json")
    wrong.write_bytes(path.read_bytes())
    link = path.with_name("2" * 64 + ".json")
    try:
        link.symlink_to(path)
    except OSError:
        pytest.skip("Symlinks unavailable")
    findings = inspect_local_state(home)
    assert sum(f.code == "local-state.invalid" for f in findings) == 2
    clean_local_state(home)
    assert wrong.exists() and link.is_symlink()


def test_absent_store_is_not_created_and_stat_errors_do_not_allow_cleanup(tmp_path):
    home = tmp_path / "home"
    assert inspect_local_state(home) == []
    assert clean_local_state(home) == []
    assert not home.exists()
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    with patch("aikito.local_state._missing", side_effect=PermissionError("denied")):
        assert inspect_local_state(home)[0].code == "local-state.invalid"
        assert clean_local_state(home) == []
    assert path.exists()


def test_journal_created_while_waiting_for_lock_blocks_cleanup(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")

    @contextmanager
    def lock(_home):
        journal = path.parent / "transactions" / "pending" / "journal.json"
        journal.parent.mkdir(parents=True)
        journal.write_text("{}", encoding="utf-8")
        yield

    with patch("aikito.local_state.WorkspaceWriterLock", lock):
        assert clean_local_state(home) == []
    assert path.exists()


@pytest.mark.parametrize("counter", ["generation", "revision"])
def test_binding_inspection_does_not_depend_on_counter_name(tmp_path, counter):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[counter] = raw.pop("generation")
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert inspect_local_state(home)[0].code == "local-state.stale"
    assert len(clean_local_state(home)) == 1
    assert not path.exists()


def test_symlinked_state_directory_is_reported_and_preserved(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    directory = path.parent
    target = directory.with_name("external-state")
    directory.rename(target)
    try:
        directory.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks unavailable")
    assert inspect_local_state(home)[0].code == "local-state.invalid"
    assert clean_local_state(home) == []
    assert (target / path.name).exists()


def test_unverifiable_transaction_storage_blocks_cleanup(tmp_path):
    home = tmp_path / "home"
    path = state_file(home, tmp_path / "workspace", tmp_path / "checkout")
    transactions = path.parent / "transactions"
    transactions.write_text("invalid", encoding="utf-8")
    assert inspect_local_state(home)[0].code == "local-state.invalid"
    assert clean_local_state(home) == []
    assert path.exists()


def test_temporary_path_resolution_and_unc_handling(tmp_path):
    from pathlib import Path
    from aikito.local_state import _resolve_existing_parent, _strip_unc, _temporary

    assert _strip_unc(Path(r"\\?\C:\Temp\test")) == Path(r"C:\Temp\test")
    assert _strip_unc(Path(r"\\?\UNC\server\share")) == Path(r"\\server\share")
    assert _strip_unc(Path("/tmp/normal")) == Path("/tmp/normal")

    nonexistent = tmp_path / "nonexistent" / "child"
    resolved = _resolve_existing_parent(nonexistent)
    assert resolved.is_relative_to(_strip_unc(tmp_path.resolve()))
    assert _temporary(nonexistent)
