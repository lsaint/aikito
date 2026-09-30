"""Full reconciliation acceptance across a pure bytes boundary."""

from __future__ import annotations

from workspace_reconcile_acceptance import exercise_behavior
from workspace_reconcile_backend import (
    SerializedFilesystemBackend,
    SerializedInMemoryBackend,
)


def test_acceptance_over_serialized_memory(tmp_path):
    backend = SerializedInMemoryBackend()
    assert not hasattr(backend.remote, "root")
    exercise_behavior(tmp_path, backend)
    assert not (tmp_path / "center").exists()
    assert backend.remote.recover() is False


def test_acceptance_over_serialized_filesystem(tmp_path):
    backend = SerializedFilesystemBackend(tmp_path / "center")
    exercise_behavior(tmp_path, backend)
