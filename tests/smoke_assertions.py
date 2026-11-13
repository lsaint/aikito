"""Filesystem assertions shared by standalone smoke scenarios."""

from pathlib import Path


def require_file(path: Path) -> None:
    if not path.is_file():
        raise AssertionError(f"{path}: expected a regular file; missing or non-file")


def require_directory(path: Path) -> None:
    if not path.is_dir():
        raise AssertionError(f"{path}: expected a directory; missing or non-directory")


def require_absent(path: Path, *, kind: str = "path") -> None:
    # Match the original shell predicates: -e follows links, -f/-d test type.
    predicate = {"path": Path.exists, "file": Path.is_file, "directory": Path.is_dir}[
        kind
    ]
    if predicate(path):
        raise AssertionError(f"{path}: expected no {kind}; found one")


def require_matching_files(directory: Path, pattern: str) -> None:
    if not any(path.is_file() for path in directory.glob(pattern)):
        raise AssertionError(
            f"{directory}: expected at least one regular {pattern} file; found none"
        )
