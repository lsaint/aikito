"""Workspace-wide synchronization planning and concise output rendering."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Callable


_CHANGE_MARKERS = (
    "[CREATE]",
    "[UPDATE]",
    "[FORCE UPDATE]",
    "[PRUNE]",
    "[DRY-RUN]",
    "[DRY RUN LINK]",
    "[DRY RUN COPY]",
    "[DRY RUN CLEANUP]",
    "[RELINK]",
)
_WARNING_MARKERS = ("[WARN]", "[WARNING]", "[ORPHAN]")
_CONFLICT_MARKERS = ("[CONFLICT]",)
_ERROR_MARKERS = ("[ERROR]",)


@dataclass(frozen=True)
class SyncPlan:
    """A read-only workspace sync result collected before any writes occur."""

    stdout: str
    stderr: str
    can_apply: bool

    @property
    def lines(self) -> tuple[str, ...]:
        return tuple(
            line.strip()
            for line in (*self.stdout.splitlines(), *self.stderr.splitlines())
            if line.strip()
        )

    def _count(self, markers: tuple[str, ...]) -> int:
        return sum(any(marker in line for marker in markers) for line in self.lines)

    @property
    def changes(self) -> int:
        return self._count(_CHANGE_MARKERS)

    @property
    def unchanged(self) -> int:
        return self._count(("[OK]",))

    @property
    def offline(self) -> int:
        return sum(
            "offline on this host" in line or "[SKIP]" in line for line in self.lines
        )

    @property
    def warnings(self) -> tuple[str, ...]:
        return self._important_lines(_WARNING_MARKERS)

    @property
    def conflicts(self) -> tuple[str, ...]:
        return self._important_lines(_CONFLICT_MARKERS)

    @property
    def errors(self) -> tuple[str, ...]:
        return self._important_lines(_ERROR_MARKERS)

    def _important_lines(self, markers: tuple[str, ...]) -> tuple[str, ...]:
        unique: list[str] = []
        for line in self.lines:
            if any(marker in line for marker in markers) and line not in unique:
                unique.append(line)
        return tuple(unique)

    def render(self, *, verbose: bool = False) -> str:
        lines = [
            "Sync plan",
            "",
            f"  Changes:   {self.changes}",
            f"  Unchanged: {self.unchanged}",
            f"  Offline:   {self.offline}",
            f"  Warnings:  {len(self.warnings)}",
            f"  Conflicts: {len(self.conflicts)}",
            f"  Errors:    {len(self.errors)}",
        ]
        important = (*self.warnings, *self.conflicts, *self.errors)
        if important:
            lines.extend(("", "Needs attention:"))
            lines.extend(f"  {line}" for line in important)
        lines.extend(
            (
                "",
                "Safe to apply" if self.can_apply else "Blocked; no changes were made",
            )
        )
        if verbose:
            details = "\n".join(
                part.rstrip() for part in (self.stdout, self.stderr) if part.strip()
            )
            if details:
                lines.extend(("", "Details", "", details))
        return "\n".join(lines)


def capture_sync_plan(run_preview: Callable[[], bool]) -> SyncPlan:
    """Run a dry-run callback and retain its complete diagnostic output."""
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        can_apply = run_preview()
    return SyncPlan(stdout.getvalue(), stderr.getvalue(), can_apply)
