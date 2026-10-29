"""Host-local approval for the global skill container."""

from __future__ import annotations

from pathlib import Path

from .approval_io import read_approval, write_approval
from .directory_identity import directory_identity
from .skill_state import get_binding_hash, validate_state_store_root


def _path(
    workspace: Path, home: Path, container: Path, *, create: bool = False
) -> Path:
    root, error = validate_state_store_root(home, create_if_missing=create)
    if error:
        raise ValueError(error)
    return root / f"global-{get_binding_hash(workspace, 'global', container)}.approval"


def _payload(workspace: Path, container: Path) -> dict:
    return {
        "version": 1,
        "workspace": str(workspace.resolve()),
        **directory_identity(container),
    }


def global_approval_error(workspace: Path, home: Path, container: Path) -> str | None:
    """Check the on-disk approval and current directory identity without writes."""
    try:
        payload = _payload(workspace, container)
        path = _path(workspace, home, container)
        data = read_approval(path)
        if data is None:
            return "Run 'aikito sync global' once to approve the skill container"
        if data != payload:
            return "Container identity changed; run 'aikito sync global' again"
    except (OSError, ValueError) as exc:
        return f"{exc}; run 'aikito sync global' to approve the skill container"
    return None


def approve_global_container(workspace: Path, home: Path, container: Path) -> None:
    """Record an explicitly synchronized container under the writer lock."""
    payload = _payload(workspace, container)
    path = _path(workspace, home, container, create=True)
    for other in path.parent.glob("global-*.approval"):
        if other == path:
            continue
        try:
            previous = read_approval(other)
        except (OSError, ValueError):
            continue
        if (
            isinstance(previous, dict)
            and previous.get("version") == 1
            and isinstance(previous.get("workspace"), str)
            and Path(previous["workspace"]).is_absolute()
            and previous.get("path") == payload["path"]
            and other == _path(Path(previous["workspace"]), home, container)
        ):
            other.unlink()
    write_approval(path, payload)
