"""Run the shared end-to-end acceptance scenario under the unit-test matrix."""

from pathlib import Path

import pytest

from workspace_reconcile_acceptance import exercise, exercise_behavior
from workspace_reconcile_backend import BACKEND_FACTORIES


@pytest.mark.parametrize("backend_name", tuple(BACKEND_FACTORIES))
def test_two_replicas_converge_after_conflicts_stale_plans_recovery_and_relocation(
    tmp_path: Path,
    backend_name: str,
):
    exercise_behavior(tmp_path, BACKEND_FACTORIES[backend_name](tmp_path / "center"))


def test_filesystem_center_relocation_preserves_identity(tmp_path: Path):
    exercise(tmp_path)
