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


def _extract_section_lines(
    text: str, start_tag: str, end_tag: str | None = None
) -> list[str]:
    lines = text.splitlines()
    in_section = False
    result = []
    for line in lines:
        if start_tag in line:
            in_section = True
            continue
        if in_section and end_tag and end_tag in line:
            in_section = False
            break
        if in_section:
            stripped = line.strip()
            if stripped:
                result.append(stripped)
    return result


@dataclass(frozen=True)
class SyncPlan:
    """A read-only workspace sync result collected before any writes occur."""

    stdout: str
    stderr: str
    can_apply: bool
    skill_batches: tuple[Any, ...] = ()
    subagent_plan: Any | None = None
    mcp_plan: Any | None = None

    def __post_init__(self) -> None:
        can_apply = self.can_apply
        if self.skill_batches and not all(
            getattr(b, "can_apply", True) for b in self.skill_batches
        ):
            can_apply = False
        if self.subagent_plan is not None and not getattr(
            self.subagent_plan, "can_apply", True
        ):
            can_apply = False
        if self.mcp_plan is not None and not getattr(
            self.mcp_plan, "can_apply", True
        ):
            can_apply = False
        object.__setattr__(self, "can_apply", can_apply)

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
        skill_changes = 0
        if self.skill_batches:
            for b in self.skill_batches:
                for op in b.skill_plan.operations:
                    if op.action in ("CREATE", "UPDATE", "UNLINK") and op.is_authorized:
                        skill_changes += 1

        subagent_changes = 0
        if self.subagent_plan is not None:
            subagent_changes = sum(
                1
                for op in self.subagent_plan.operations
                if op.action in ("CREATE", "UPDATE", "REMOVE") and op.is_authorized
            )

        mcp_changes = 0
        if self.mcp_plan is not None:
            mcp_changes = self.mcp_plan.changes_count

        # If structured plans are provided, exclude their stdout markers completely
        if self.skill_batches or self.subagent_plan is not None or self.mcp_plan is not None:
            if "[1/4] Global Resources" in self.stdout:
                unmanaged_lines = _extract_section_lines(
                    self.stdout, "[1/4] Global Resources", "[2/4] Subagents"
                )
                other_changes = sum(
                    any(marker in line for marker in _CHANGE_MARKERS) for line in unmanaged_lines
                )
            else:
                excluded_lines = set()
                if self.mcp_plan is not None:
                    for l in self.lines:
                        if "[DRY-RUN]" in l:
                            excluded_lines.add(l)
                        elif any(op.target.logical_identity in l for op in self.mcp_plan.operations):
                            excluded_lines.add(l)
                if self.subagent_plan is not None:
                    for l in self.lines:
                        if any(m in l for m in ("[CREATE]", "[UPDATE]", "[REMOVE]", "[ORPHAN]", "[PRUNE]")):
                            excluded_lines.add(l)
                        elif any(op.target.logical_identity in l for op in self.subagent_plan.operations):
                            excluded_lines.add(l)
                if self.skill_batches:
                    for l in self.lines:
                        if any(m in l for m in ("[DRY RUN LINK]", "[DRY RUN COPY]", "[DRY RUN CLEANUP]")):
                            excluded_lines.add(l)

                other_lines = [l for l in self.lines if l not in excluded_lines]
                other_changes = sum(
                    any(marker in line for marker in _CHANGE_MARKERS) for line in other_lines
                )
            return skill_changes + subagent_changes + mcp_changes + other_changes

        return self._count(_CHANGE_MARKERS)

    @property
    def unchanged(self) -> int:
        if self.subagent_plan is not None or self.mcp_plan is not None:
            count = 0
            if self.subagent_plan is not None:
                count += sum(1 for op in self.subagent_plan.operations if op.action == "NOOP")
            if self.mcp_plan is not None:
                count += sum(1 for op in self.mcp_plan.operations if op.action == "NOOP")
            return count
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
        conflicts_list: list[str] = []
        if self.skill_batches:
            for b in self.skill_batches:
                for op in b.skill_plan.operations:
                    if op.action == "CONFLICT" or (
                        op.requires_force and not op.is_authorized
                    ):
                        msg = op.finding or op.reason
                        if msg not in conflicts_list:
                            conflicts_list.append(msg)

        if self.subagent_plan is not None:
            for op in self.subagent_plan.operations:
                if op.action == "CONFLICT" or (op.requires_force and not op.is_authorized):
                    msg = f"{op.target.agent}/{op.target.logical_identity}: {op.reason}"
                    if msg not in conflicts_list:
                        conflicts_list.append(msg)

        if self.mcp_plan is not None:
            for op in self.mcp_plan.operations:
                if (op.action == "CONFLICT" and not op.is_authorized) or op.action == "ERROR":
                    msg = f"{op.target.agent}/{op.target.logical_identity}: {op.reason}"
                    if msg not in conflicts_list:
                        conflicts_list.append(msg)

        # Include any other unmanaged conflicts from lines
        if "[1/4] Global Resources" in self.stdout:
            unmanaged_conflicts = [
                l for l in _extract_section_lines(self.stdout, "[1/4] Global Resources", "[2/4] Subagents")
                if any(m in l for m in _CONFLICT_MARKERS)
            ]
            for c in unmanaged_conflicts:
                if c not in conflicts_list:
                    conflicts_list.append(c)
        else:
            base_conflicts = list(self._important_lines(_CONFLICT_MARKERS))
            for c in base_conflicts:
                if self.mcp_plan is not None and any(op.target.logical_identity in c for op in self.mcp_plan.operations):
                    continue
                if self.subagent_plan is not None and any(op.target.logical_identity in c for op in self.subagent_plan.operations):
                    continue
                if c not in conflicts_list and not any(c in cl or cl in c for cl in conflicts_list):
                    conflicts_list.append(c)

        return tuple(conflicts_list)

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
    subagent_plan: Any | None = None,
    subagent_plan_fn: Callable[[], Any | None] | None = None,
    mcp_plan: Any | None = None,
    mcp_plan_fn: Callable[[], Any | None] | None = None,
) -> SyncPlan:
    """Run a dry-run callback and retain its complete diagnostic output.

    *skill_batches_fn*, *subagent_plan_fn*, and *mcp_plan_fn* are evaluated
    **after** run_preview() so that callers can pass lambdas reading plans
    populated during preview.
    """
    stdout = StringIO()
    stderr = StringIO()
    with redirect_stdout(stdout), redirect_stderr(stderr):
        preview_ok = run_preview()

    resolved_skill_batches = (
        tuple(skill_batches_fn())
        if skill_batches_fn is not None
        else tuple(skill_batches or ())
    )
    resolved_subagent_plan = (
        subagent_plan_fn()
        if subagent_plan_fn is not None
        else subagent_plan
    )
    resolved_mcp_plan = (
        mcp_plan_fn()
        if mcp_plan_fn is not None
        else mcp_plan
    )

    can_apply = preview_ok
    if resolved_skill_batches:
        can_apply = can_apply and all(
            getattr(b, "can_apply", True) for b in resolved_skill_batches
        )
    if resolved_subagent_plan is not None and not resolved_subagent_plan.can_apply:
        can_apply = False
    if resolved_mcp_plan is not None and not resolved_mcp_plan.can_apply:
        can_apply = False

    return SyncPlan(
        stdout=stdout.getvalue(),
        stderr=stderr.getvalue(),
        can_apply=can_apply,
        skill_batches=resolved_skill_batches,
        subagent_plan=resolved_subagent_plan,
        mcp_plan=resolved_mcp_plan,
    )
