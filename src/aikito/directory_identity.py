"""Physical directory identity for host-local write approvals."""

from pathlib import Path

from .compat import is_reparse_point


def directory_identity(path: Path) -> dict:
    """Reject redirected paths and return the directory's physical identity."""
    current = path
    # System aliases above home (e.g. /var on macOS) are resolved in identity.
    while current.parent != current.parent.parent:
        if is_reparse_point(current):
            raise ValueError(
                f"Directory path contains a symbolic link or reparse point: {current}"
            )
        current = current.parent
    info = path.stat()
    if not path.is_dir() or not info.st_ino:
        raise ValueError(f"Cannot establish directory identity: {path}")
    return {"path": str(path.resolve()), "device": info.st_dev, "inode": info.st_ino}
