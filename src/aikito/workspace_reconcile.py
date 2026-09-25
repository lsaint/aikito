"""Internal reconciliation of two workspaces against a shared baseline."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from pathlib import Path

from .compat import secure_directory_permissions
from .skill_state import WorkspaceWriterLock
from .workspace_core import (
    Change,
    Decision,
    PathPolicy,
    StateUpdate,
    Version,
    WorkspaceCoreError,
    apply,
    compare_versions,
    entry_type,
    has_pending,
    recover,
    supported_snapshot,
    validate_resource_path,
    validate_roots,
)


class WorkspaceReconcileError(WorkspaceCoreError):
    """Workspace versions cannot be reconciled safely."""


ResourceVersion = Version
ReconcileItem = Decision
_BASELINE_RELATIVE = ".local/state/aikito/workspace-reconcile/baseline.json"
_PATH_POLICY = PathPolicy(states=(_BASELINE_RELATIVE,))


@dataclass(frozen=True)
class ReconcilePlan:
    left: Path
    right: Path
    generation: int
    base: dict[str, Version]
    left_snapshot: dict[str, Version]
    right_snapshot: dict[str, Version]
    items: tuple[Decision, ...]

    @property
    def conflicts(self) -> tuple[Decision, ...]:
        return tuple(item for item in self.items if item.action == "CONFLICT")

    @property
    def changes(self) -> tuple[Decision, ...]:
        return tuple(item for item in self.items if item.action in ("COPY", "DELETE"))


def _roots(left: Path, right: Path) -> tuple[Path, Path]:
    try:
        return tuple(sorted(validate_roots(left, right), key=str))  # type: ignore[return-value]
    except WorkspaceCoreError as exc:
        raise WorkspaceReconcileError(str(exc)) from exc


def _baseline_path(root: Path, *, create: bool = False) -> Path:
    current = root
    for part in (".local", "state", "aikito", "workspace-reconcile"):
        current /= part
        kind = entry_type(current)
        if kind == "missing" and create:
            current.mkdir(mode=0o700)
            if not secure_directory_permissions(current):
                raise WorkspaceReconcileError(
                    f"Cannot secure baseline directory: {current}"
                )
        elif kind != "directory":
            raise WorkspaceReconcileError(
                f"Missing or unsafe baseline directory: {current}"
            )
    return current / "baseline.json"


def _encode_snapshot(snapshot: dict[str, Version]) -> dict[str, list[str]]:
    return {
        key: [item.kind, item.fingerprint] for key, item in sorted(snapshot.items())
    }


def _decode_snapshot(raw: object) -> dict[str, Version]:
    if not isinstance(raw, dict):
        raise WorkspaceReconcileError("Invalid baseline resources")
    result = {}
    for path, value in raw.items():
        if (
            not isinstance(path, str)
            or not isinstance(value, list)
            or len(value) != 2
            or value[0] not in ("memory", "skill")
            or not isinstance(value[1], str)
            or len(value[1]) != 64
            or any(char not in "0123456789abcdef" for char in value[1])
        ):
            raise WorkspaceReconcileError("Invalid baseline resource")
        validate_resource_path(path, value[0])
        result[path] = Version(value[0], value[1])
    return result


def _baseline_state(
    left: Path,
    right: Path,
    baseline_id: str,
    generation: int,
    snapshot: dict[str, Version],
) -> dict[str, object]:
    return {
        "version": 1,
        "roots": [str(left), str(right)],
        "baseline_id": baseline_id,
        "generation": generation,
        "base": _encode_snapshot(snapshot),
    }


def _read_baseline(
    left: Path, right: Path
) -> tuple[dict[str, object], dict[str, Version]]:
    states = []
    for root in (left, right):
        path = _baseline_path(root)
        if entry_type(path) != "file":
            raise WorkspaceReconcileError("Workspaces have no shared baseline")
        try:
            states.append(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError) as exc:
            raise WorkspaceReconcileError(f"Cannot read baseline: {path}") from exc
    if states[0] != states[1]:
        raise WorkspaceReconcileError("Baseline versions differ")
    state = states[0]
    if (
        not isinstance(state, dict)
        or state.get("version") != 1
        or state.get("roots") != [str(left), str(right)]
        or type(state.get("generation")) is not int
        or state["generation"] < 0
        or not isinstance(state.get("baseline_id"), str)
        or len(state["baseline_id"]) != 32
    ):
        raise WorkspaceReconcileError("Invalid baseline state")
    return state, _decode_snapshot(state.get("base"))


def baseline_workspaces(left: Path, right: Path, home: Path) -> None:
    """Record identical supported resources as the initial common version."""
    left, right = _roots(left, right)
    roots = (left, right)
    with WorkspaceWriterLock(home):
        if has_pending(roots, policy=_PATH_POLICY):
            raise WorkspaceReconcileError(
                "Pending workspace transaction needs recovery"
            )
        for root in roots:
            path = _baseline_path(root, create=True)
            if entry_type(path) != "missing":
                raise WorkspaceReconcileError(
                    f"Workspace already has a baseline: {root}"
                )
        a, _ = supported_snapshot(left)
        b, _ = supported_snapshot(right)
        if a != b:
            raise WorkspaceReconcileError(
                "Supported resources differ; baseline requires identical workspaces"
            )
        state = _baseline_state(left, right, uuid.uuid4().hex, 0, a)
        encoded = json.dumps(state, sort_keys=True)
        try:
            apply(
                roots,
                (),
                states=tuple(
                    StateUpdate(index, _BASELINE_RELATIVE, None, encoded)
                    for index in range(2)
                ),
                policy=_PATH_POLICY,
            )
        except WorkspaceCoreError as exc:
            raise WorkspaceReconcileError(str(exc)) from exc


def build_reconcile_plan(left: Path, right: Path) -> ReconcilePlan:
    """Compare two independent snapshots with their common baseline."""
    left, right = _roots(left, right)
    if has_pending((left, right), policy=_PATH_POLICY):
        raise WorkspaceReconcileError("Pending round needs recovery before preview")
    state, base = _read_baseline(left, right)
    a, _ = supported_snapshot(left)
    b, _ = supported_snapshot(right)
    return ReconcilePlan(
        left, right, state["generation"], base, a, b, compare_versions(base, a, b)
    )


def recover_reconciliation(left: Path, right: Path) -> bool:
    """Recover an interrupted round while preserving external changes."""
    roots = _roots(left, right)
    try:
        return recover(roots, policy=_PATH_POLICY)
    except WorkspaceCoreError as exc:
        raise WorkspaceReconcileError(str(exc)) from exc


def apply_reconcile_plan(plan: ReconcilePlan, home: Path) -> None:
    """Apply a fresh plan and advance the common version after verification."""
    roots = (plan.left, plan.right)
    with WorkspaceWriterLock(home):
        if recover_reconciliation(*roots):
            raise WorkspaceReconcileError("Recovered an interrupted round; run again")
        fresh = build_reconcile_plan(*roots)
        if fresh != plan:
            raise WorkspaceReconcileError(
                "Workspaces changed after planning; run again"
            )
        if plan.conflicts:
            raise WorkspaceReconcileError(
                "Reconciliation has conflicts; no files changed"
            )
        state, _ = _read_baseline(*roots)
        if not plan.changes and plan.left_snapshot == plan.base:
            return
        changes = []
        for item in plan.changes:
            index = 0 if item.target == "left" else 1
            before = (plan.left_snapshot if index == 0 else plan.right_snapshot).get(
                item.path
            )
            after = (plan.right_snapshot if index == 0 else plan.left_snapshot).get(
                item.path
            )
            changes.append(
                Change(
                    index,
                    item.path,
                    item.kind,
                    roots[1 - index] / item.path if after else None,
                    before.fingerprint if before else None,
                    after.fingerprint if after else None,
                )
            )

        def verify() -> None:
            a, _ = supported_snapshot(plan.left)
            b, _ = supported_snapshot(plan.right)
            if a != b:
                raise WorkspaceReconcileError("Workspaces differ after apply")

        target_snapshot = dict(plan.left_snapshot)
        for item in plan.changes:
            version = (
                plan.right_snapshot if item.target == "left" else plan.left_snapshot
            ).get(item.path)
            if version is None:
                target_snapshot.pop(item.path, None)
            else:
                target_snapshot[item.path] = version
        new_state = _baseline_state(
            plan.left,
            plan.right,
            state["baseline_id"],
            plan.generation + 1,
            target_snapshot,
        )
        states = tuple(
            StateUpdate(
                index,
                _BASELINE_RELATIVE,
                _baseline_path(root).read_text(encoding="utf-8"),
                json.dumps(new_state, sort_keys=True),
            )
            for index, root in enumerate(roots)
        )
        try:
            apply(
                roots,
                tuple(changes),
                states=states,
                verify=verify,
                policy=_PATH_POLICY,
            )
        except WorkspaceCoreError as exc:
            raise WorkspaceReconcileError(str(exc)) from exc


def run_reconciliation(
    left: Path, right: Path, home: Path, *, dry_run: bool
) -> ReconcilePlan:
    """Preview or apply one reconciliation round."""
    left, right = _roots(left, right)
    if has_pending((left, right), policy=_PATH_POLICY):
        if dry_run:
            raise WorkspaceReconcileError("Pending round needs recovery before preview")
        with WorkspaceWriterLock(home):
            recover_reconciliation(left, right)
        raise WorkspaceReconcileError("Recovered an interrupted round; run again")
    plan = build_reconcile_plan(left, right)
    if not dry_run and not plan.conflicts:
        apply_reconcile_plan(plan, home)
    return plan
