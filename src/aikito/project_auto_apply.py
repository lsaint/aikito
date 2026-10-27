"""Reconcile approved local checkouts without trusting remotely supplied paths.

Each checkout is planned and applied under the shared writer lock. Resource
phases retain their existing rollback boundaries; retries use actual state.
"""

from __future__ import annotations

import os
import time
import tomllib
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from .project_approval import candidate_checkout, checkout_approval_error
from .project_config import get_project_candidate_paths, project_sync_trigger
from .project_sync import apply_project_sync_batch, build_project_sync_batch
from .skill_state import WorkspaceWriterLock
from .workspace.resource_state import PENDING_COMMIT_STATE, state_path
from .workspace.transactions import has_pending


@dataclass(frozen=True)
class CheckoutResult:
    project: str
    checkout: str
    status: str
    reason: str = ""
    changes: int = 0


@dataclass(frozen=True)
class AutoProjectResult:
    checkouts: tuple[CheckoutResult, ...] = ()

    @property
    def failed(self) -> bool:
        return any(item.status in ("blocked", "failed") for item in self.checkouts)

    def to_dict(self) -> dict:
        return {"checkouts": [asdict(item) for item in self.checkouts]}


def _pending_workspace(workspace: Path) -> bool:
    # Never consume a partially installed workspace while offline or recovering.
    return (
        has_pending((workspace,))
        or state_path(workspace, PENDING_COMMIT_STATE).exists()
    )


def _reconcile_checkout(
    workspace: Path, home: Path, project: str, checkout: Path, *, dry_run: bool
) -> CheckoutResult:
    with nullcontext() if dry_run else WorkspaceWriterLock(home):
        config = workspace / "projects" / project / "agent.toml"
        data = tomllib.loads(config.read_text(encoding="utf-8"))
        trigger = project_sync_trigger(data)
        if trigger == "manual":
            return CheckoutResult(project, str(checkout), "manual")
        # Configuration may have changed while waiting for the lock.
        candidates = {
            candidate_checkout(raw, home)
            for _, raw in get_project_candidate_paths(data)
        }
        if checkout not in candidates:
            return CheckoutResult(
                project,
                str(checkout),
                "unbound",
                "Checkout removed from project configuration",
            )
        if not checkout.is_dir():
            return CheckoutResult(
                project, str(checkout), "offline", "Checkout directory unavailable"
            )
        error = checkout_approval_error(workspace, home, project, checkout)
        if error:
            return CheckoutResult(project, str(checkout), "unapproved", error)
        if _pending_workspace(workspace):
            return CheckoutResult(
                project, str(checkout), "blocked", "Workspace recovery is pending"
            )
        batch = build_project_sync_batch(
            workspace,
            home,
            project,
            data,
            explicit_path=checkout.resolve(),
            register_explicit_path=False,
            force=False,
        )
        if not batch.can_apply:
            reasons = [finding.message for finding in batch.observe().findings]
            return CheckoutResult(
                project,
                str(checkout),
                "blocked",
                "; ".join(reasons) or "Checkout contains conflicting resources",
            )
        # Recheck physical identity immediately before execution as well as planning.
        error = checkout_approval_error(workspace, home, project, checkout)
        if error:
            return CheckoutResult(project, str(checkout), "unapproved", error)
        changes = sum(
            op.action not in ("NOOP", "SKIP", "OFFLINE", "SHARED_PATH")
            for op in batch.skill_plan.operations
        )
        for plan in (batch.instruction_plan, batch.memory_plan):
            if plan is not None:
                changes += sum(
                    op.action not in ("NOOP", "SKIP", "OFFLINE", "SHARED_PATH")
                    for op in plan.operations
                )
        if not dry_run:
            result = apply_project_sync_batch(batch, data, home)
            if not result.is_success:
                return CheckoutResult(
                    project,
                    str(checkout),
                    "failed",
                    result.error_message or "Project application failed",
                    changes,
                )
        return CheckoutResult(
            project,
            str(checkout),
            "applied" if changes else "noop",
            "Preview only" if dry_run else "",
            changes,
        )


def reconcile_local_projects(
    workspace: Path,
    home: Path,
    *,
    dry_run: bool = False,
    reuse_checkout: Callable[[str, Path], CheckoutResult | None] | None = None,
) -> AutoProjectResult:
    """Apply only approved checkouts; isolate invalid configs and individual failures."""
    projects = workspace / "projects"
    if not projects.is_dir():
        return AutoProjectResult()
    results: list[CheckoutResult] = []
    for directory in sorted(projects.iterdir()):
        if not directory.is_dir():
            continue
        project = directory.name
        try:
            if directory.is_symlink():
                raise ValueError("Project resource directory cannot be a symbolic link")
            data = tomllib.loads((directory / "agent.toml").read_text(encoding="utf-8"))
            if project_sync_trigger(data) == "manual":
                results.append(CheckoutResult(project, "", "manual"))
                continue
            candidates = dict.fromkeys(
                candidate_checkout(raw, home)
                for _, raw in get_project_candidate_paths(data)
            )
        except (OSError, ValueError) as exc:
            results.append(CheckoutResult(project, "", "blocked", str(exc)))
            continue
        if not candidates:
            results.append(
                CheckoutResult(project, "", "unbound", "No local checkout configured")
            )
        for checkout in candidates:
            try:
                cached = reuse_checkout(project, checkout) if reuse_checkout else None
                results.append(
                    cached
                    if cached is not None
                    else _reconcile_checkout(
                        workspace, home, project, checkout, dry_run=dry_run
                    )
                )
            except Exception as exc:  # noqa: BLE001 - One checkout cannot stop others.
                results.append(
                    CheckoutResult(project, str(checkout), "failed", str(exc))
                )
    return AutoProjectResult(tuple(results))


def render_auto_project_result(result: AutoProjectResult) -> str:
    lines = []
    for item in result.checkouts:
        label = (
            "PROJECT_APPLY_FAILED"
            if item.status == "failed"
            else "PROJECT_APPLY_BLOCKED"
            if item.status == "blocked"
            else "PROJECT_APPLY_OK"
        )
        lines.append(
            f"[{label}] {item.project} {item.checkout}: {item.status} ({item.changes} changes)"
            + (f"; {item.reason}" if item.reason else "")
        )
    return "\n".join(lines)


@dataclass
class _CheckoutRetry:
    result: CheckoutResult
    next_scan: float
    failures: int = 0


class ProjectAutoApply:
    """Worker-local throttling and deduplicated reporting, independent of remote I/O.

    Workspace events invalidate cached plans. Each checkout retries independently;
    blocked resources retry every thirty seconds without repeating unchanged logs.
    """

    def __init__(
        self, workspace: Path, home: Path, logger: Callable[[str], None]
    ) -> None:
        self.workspace = workspace
        self.home = home
        self.logger = logger
        self._retries: dict[tuple[str, str], _CheckoutRetry] = {}
        self._next_probe = 0.0
        self._signature: tuple | None = None
        self._last_result: AutoProjectResult | None = None
        self.status: dict = {}

    def _workspace_signature(self) -> tuple:
        entries = []
        for directory in (
            self.workspace / "skills",
            self.workspace / "projects",
            self.workspace / "agents",
            self.workspace / "global",
            self.workspace / "memory",
        ):
            for root, dirs, files in os.walk(directory):
                dirs[:] = [
                    name for name in dirs if name not in (".git", ".local", ".agents")
                ]
                for name in sorted(files):
                    path = Path(root) / name
                    try:
                        info = path.stat()
                    except (FileNotFoundError, NotADirectoryError):
                        # Editors may replace or remove files during a metadata scan.
                        continue
                    entries.append((str(path), info.st_mtime_ns, info.st_size))
        return tuple(sorted(entries))

    def _reuse_checkout(
        self, project: str, checkout: Path, now: float
    ) -> CheckoutResult | None:
        retry = self._retries.get((project, str(checkout)))
        if retry is None:
            return None
        if retry.result.status == "offline":
            return retry.result if not checkout.is_dir() else None
        if retry.result.status == "unapproved":
            # Approval changes are host-local and do not emit workspace events.
            error = checkout_approval_error(
                self.workspace, self.home, project, checkout
            )
            return retry.result if error == retry.result.reason else None
        return retry.result if now < retry.next_scan else None

    def run(self, *, force: bool = False) -> dict:
        now = time.monotonic()
        if not force and now < self._next_probe:
            return self.status
        signature = self._workspace_signature()
        self._next_probe = now + 30
        invalidate = force or signature != self._signature
        reused = set()

        def reuse(project: str, checkout: Path) -> CheckoutResult | None:
            cached = self._reuse_checkout(project, checkout, now)
            if cached is not None:
                reused.add((project, str(checkout)))
            return cached

        result = reconcile_local_projects(
            self.workspace,
            self.home,
            reuse_checkout=None if invalidate else reuse,
        )
        retries = {}
        for item in result.checkouts:
            key = (item.project, item.checkout)
            previous = self._retries.get(key)
            # Reused results retain their deadline and failure count.
            if key in reused and previous is not None:
                retries[key] = previous
                continue
            failures = 0
            if item.status == "failed":
                failures = (previous.failures if previous else 0) + 1
                delay = min(300, 30 * 2 ** min(failures - 1, 4))
            elif item.status == "blocked":
                delay = 30
            else:
                delay = 300 if previous and item == previous.result else 30
            retries[key] = _CheckoutRetry(item, now + delay, failures)
        self._retries = retries
        self._signature = signature
        previous_results = (
            {
                (item.project, item.checkout): item
                for item in self._last_result.checkouts
            }
            if self._last_result is not None
            else {}
        )
        changed = AutoProjectResult(
            tuple(
                item
                for item in result.checkouts
                if item != previous_results.get((item.project, item.checkout))
            )
        )
        rendered = render_auto_project_result(changed)
        if rendered:
            self.logger(rendered)
        self._last_result = result
        self.status = {
            "last_checked": datetime.now(UTC).isoformat(),
            **result.to_dict(),
            "last_error": next(
                (
                    item.reason
                    for item in result.checkouts
                    if item.status in ("failed", "blocked")
                ),
                None,
            ),
        }
        return self.status
