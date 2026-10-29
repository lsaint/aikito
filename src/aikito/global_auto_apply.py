"""Reconcile selected global skills inside a locally approved container."""

from __future__ import annotations

import time
import tomllib
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .add import validate_resource_name
from .agents import AgentRegistry
from .global_approval import global_approval_error
from .global_skills import build_global_skill_batch, plan_global_skills
from .instructions import build_global_instruction_batch, plan_instructions
from .link import apply_link_operation
from .project_auto_apply import _pending_workspace
from .skill_state import WorkspaceWriterLock


@dataclass(frozen=True)
class AutoGlobalResult:
    status: str
    changes: int = 0
    reason: str = ""
    runtime: str = "ok"

    @property
    def failed(self) -> bool:
        return self.status == "failed"


def _runtime_status(workspace: Path, home: Path) -> str:
    try:
        runtime_plan = plan_global_skills(
            build_global_skill_batch(workspace, home, skills=()),
            home,
            refreshed_bundled=(),
        )
        instructions = plan_instructions(
            build_global_instruction_batch(workspace, home), home
        )
        operations = (*runtime_plan.consumer_ops, *instructions.operations)
        if any(op.action == "CONFLICT" for op in operations):
            return "conflict"
        if any(op.action not in ("NOOP", "SKIP", "SHARED_PATH") for op in operations):
            return "drift"
        return "ok"
    except (OSError, ValueError):
        return "conflict"


def reconcile_local_global(
    workspace: Path, home: Path, *, dry_run: bool = False
) -> AutoGlobalResult:
    container = home / ".agents" / "skills"
    try:
        with nullcontext() if dry_run else WorkspaceWriterLock(home):
            runtime = _runtime_status(workspace, home)
            error = global_approval_error(workspace, home, container)
            if error:
                return AutoGlobalResult("unapproved", reason=error, runtime=runtime)
            if _pending_workspace(workspace):
                return AutoGlobalResult(
                    "blocked", reason="Workspace recovery is pending", runtime=runtime
                )
            config = workspace / "skills.toml"
            if not config.is_file():
                return AutoGlobalResult(
                    "blocked",
                    reason="Global skill configuration is missing",
                    runtime=runtime,
                )
            data = tomllib.loads(config.read_text(encoding="utf-8"))
            skills = data.get("skills")
            if not isinstance(skills, list) or any(
                not isinstance(name, str)
                or name != name.strip()
                or validate_resource_name(name, "skill")
                for name in skills
            ):
                return AutoGlobalResult(
                    "blocked", reason="Invalid global skill selection", runtime=runtime
                )
            plan = plan_global_skills(
                build_global_skill_batch(
                    workspace, home, skills=skills, registry=AgentRegistry({})
                ),
                home,
                refreshed_bundled=(),
            )
            if plan.container_op.action != "NOOP":
                return AutoGlobalResult(
                    "blocked",
                    reason="Skill container requires manual repair",
                    runtime=runtime,
                )
            changes = 0
            conflicts = []
            for op in plan.entry_ops:
                if op.action == "CONFLICT" or op.action not in (
                    "CREATE",
                    "UNLINK",
                    "NOOP",
                ):
                    conflicts.append(op.reason or str(op.target_path))
                    continue
                if op.action == "NOOP":
                    continue
                if not dry_run:
                    result = apply_link_operation(op, quiet_unchanged=True)
                    if not result.success:
                        return AutoGlobalResult(
                            "failed",
                            changes,
                            result.error_message or "Skill application failed",
                            runtime,
                        )
                changes += 1
            return AutoGlobalResult(
                "blocked" if conflicts else "applied" if changes else "noop",
                changes,
                "; ".join(conflicts),
                runtime,
            )
    except Exception as exc:  # noqa: BLE001 - Local reconciliation is isolated from remote sync.
        return AutoGlobalResult("failed", reason=str(exc))


def render_auto_global_result(
    result: AutoGlobalResult, *, dry_run: bool = False
) -> str:
    label = (
        "FAILED"
        if result.status == "failed"
        else "NOTICE"
        if result.status == "unapproved"
        else "BLOCKED"
        if result.status == "blocked"
        else "OK"
    )
    status = "would apply" if dry_run and result.status == "applied" else result.status
    count = f"{result.changes} changes {'planned' if dry_run else 'applied'}"
    message = (
        "" if result.status == "noop" else f"[GLOBAL_APPLY_{label}] {status} ({count})"
    )
    if result.reason:
        message += f"; {result.reason}"
    if result.runtime != "ok":
        message += (
            "\n" if message else ""
        ) + f"[GLOBAL_RUNTIME_DRIFT] {result.runtime}; run 'aikito sync global'"
    return message


class GlobalAutoApply:
    def __init__(self, workspace: Path, home: Path, logger) -> None:
        self.workspace, self.home, self.logger = workspace, home, logger
        self.status = {}
        self._result = None
        self._next = 0.0
        self._failures = 0

    def run(self, *, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and now < self._next:
            return self.status
        result = reconcile_local_global(self.workspace, self.home)
        if result != self._result:
            rendered = render_auto_global_result(result)
            if rendered:
                self.logger(rendered)
        self._failures = self._failures + 1 if result.failed else 0
        delay = (
            min(300, 30 * 2 ** min(self._failures - 1, 4))
            if self._failures
            else (
                30
                if result.status in ("blocked", "unapproved") or result != self._result
                else 300
            )
        )
        self._next = now + delay
        self._result = result
        self.status = {
            "last_checked": datetime.now(UTC).isoformat(),
            **asdict(result),
            "last_error": result.reason
            if result.status in ("failed", "blocked")
            else None,
        }
        return self.status
