"""Public workspace API; implementation modules are imported directly internally."""

from .api import (
    InvalidWorkspaceError,
    Workspace,
    WorkspaceError,
    WorkspaceFinding,
    WorkspaceInspectionResult,
    WorkspaceNotFoundError,
    WorkspaceOperationView,
    WorkspaceProjectView,
    WorkspaceSyncPreview,
)
from .paths import (
    get_workspace_pointer_path,
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)

__all__ = [
    "InvalidWorkspaceError",
    "Workspace",
    "WorkspaceError",
    "WorkspaceFinding",
    "WorkspaceInspectionResult",
    "WorkspaceNotFoundError",
    "WorkspaceOperationView",
    "WorkspaceProjectView",
    "WorkspaceSyncPreview",
    "get_workspace_pointer_path",
    "persist_workspace",
    "resolve_workspace",
    "resolve_workspace_with_source",
]
