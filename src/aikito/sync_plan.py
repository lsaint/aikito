"""Workspace-wide synchronization planning and concise output rendering."""

from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from dataclasses import dataclass
from io import StringIO
from typing import Any, Callable, Sequence


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
    skill_batches: tuple[Any, ...] = ()

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
        if not self.skill_batches:
            return self._count(_CHANGE_MARKERS)
        skill_changes = 0
        for b in self.skill_batches:
            for op in b.skill_plan.operations:
                if op.action in ("CREATE", "UPDATE", "UNLINK") and op.is_authorized:
                    skill_changes += 1
        other_lines = [
            line
            for line in self.lines
            if not any(
                marker in line
                for marker in ("[DRY RUN LINK]", "[DRY RUN COPY]", "[DRY RUN CLEANUP]")
            )
        ]
        other_changes = sum(
            any(marker in line for marker in _CHANGE_MARKERS) for line in other_lines
        )
        return skill_changes + other_changes

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
        base_conflicts = list(self._important_lines(_CONFLICT_MARKERS))
        if not self.skill_batches:
            return tuple(base_conflicts)
        for b in self.skill_batches:
            for op in b.skill_plan.operations:
                if op.action == "CONFLICT" or (
                    op.requires_force and not op.is_authorized
                ):
                    msg = op.finding or op.reason
                    if msg not in base_conflicts:
                        base_conflicts.append(msg)
        return tuple(base_conflicts)

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


def capture_sync_plan(
    run_preview: Callable[[], bool],
    skill_batches: Sequence[Any] | None = None,
    skill_batches_fn: Callable[[], Sequence[Any]] | None = None,
) -> SyncPlan:
    """Run a dry-run callback and retain its complete diagnostic output.

    *skill_batches_fn* is evaluated **after** run_preview() so that callers can
    pass a lambda that reads a dict populated during the preview run.
    *skill_batches* is retained for backward-compatibility but is deprecated when
    used with a lazy source (values would be empty at call time).
    """
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        can_apply = run_preview()
    # Resolve batches after preview has populated them
    if skill_batches_fn is not None:
        batches = tuple(skill_batches_fn())
    else:
        batches = tuple(skill_batches) if skill_batches else ()
    if batches:
        can_apply = can_apply and all(b.can_apply for b in batches)
    return SyncPlan(
        stdout.getvalue(), stderr.getvalue(), can_apply, skill_batches=batches
    )
