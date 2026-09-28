"""Run the shared end-to-end acceptance scenario under the unit-test matrix."""

from pathlib import Path

from workspace_reconcile_acceptance import exercise


def test_two_replicas_converge_after_conflicts_stale_plans_recovery_and_relocation(
    tmp_path: Path,
):
    exercise(tmp_path)
