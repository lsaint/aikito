"""Safe access to host-local approval records."""

import json
from pathlib import Path

from .compat import _atomic_write_text, is_reparse_point, secure_file_permissions


def read_approval(path: Path) -> object | None:
    if is_reparse_point(path):
        raise ValueError(f"Unsafe approval file: {path}")
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def write_approval(path: Path, payload: dict) -> None:
    if is_reparse_point(path):
        raise ValueError(f"Unsafe approval file: {path}")
    _atomic_write_text(path, json.dumps(payload, sort_keys=True) + "\n")
    secure_file_permissions(path)
