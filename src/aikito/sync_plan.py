"""Workspace-wide synchronization planning and presentation models (INV-APP-03)."""

from __future__ import annotations

from .workspace_sync import WorkspaceSyncPlan

# Presentation-tier alias for the unified workspace sync plan.
SyncPlan = WorkspaceSyncPlan

__all__ = ["SyncPlan"]
