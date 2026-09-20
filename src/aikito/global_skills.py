"""Global skills batch model, three-tier Target representation, and pure planning."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

from .agents import AgentRegistry, Target, check_target_availability, resolve_targets
from .link import LinkOperation, ObservedLink, inspect_link_target, plan_link_target

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib


def load_global_skills_list(aikito_dir: Path) -> tuple[str, ...]:
    """Load the list of selected skill names from workspace skills.toml."""
    skills_toml = aikito_dir / "skills.toml"
    if not skills_toml.is_file():
        return ()
    try:
        with open(skills_toml, "rb") as f:
            data = tomllib.load(f)
        skills = data.get("skills", [])
        if isinstance(skills, list):
            return tuple(str(s) for s in skills)
        return ()
    except Exception:
        return ()


@dataclass(frozen=True)
class GlobalSkillBatch:
    """Three-tier target structure representing a complete global skill sync request.

    - container: Managed container target (~/.agents/skills)
    - selected_entries: Targets for currently configured skills in skills.toml
    - stale_entries: Targets for unmanaged or deselected entries found inside container
    - consumers: Deduplicated physical consumer targets (~/.claude/skills, etc.)
    """

    workspace_root: Path
    container: Target
    selected_entries: tuple[Target, ...]
    stale_entries: tuple[Target, ...]
    consumers: tuple[Target, ...]

    @property
    def resource_count(self) -> int:
        """Count of selected global skills in configuration."""
        return len(self.selected_entries)

    @property
    def consumer_count(self) -> int:
        """Total count of Agent platforms declaring skills_path."""
        return sum(len(c.consumers) for c in self.consumers)

    @property
    def consumer_target_count(self) -> int:
        """Count of deduplicated physical consumer targets on this host."""
        return len(self.consumers)


@dataclass(frozen=True)
class GlobalSkillBatchPlan:
    """Immutable, fully-evaluated synchronization plan for global skills."""

    batch: GlobalSkillBatch
    container_op: LinkOperation
    entry_ops: tuple[LinkOperation, ...]
    consumer_ops: tuple[LinkOperation, ...]

    @property
    def all_operations(self) -> tuple[LinkOperation, ...]:
        return (self.container_op, *self.entry_ops, *self.consumer_ops)

    @property
    def conflicts(self) -> tuple[LinkOperation, ...]:
        return tuple(op for op in self.all_operations if op.action == "CONFLICT")

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)

    @property
    def can_apply(self) -> bool:
        return not self.has_conflicts and all(op.is_authorized for op in self.all_operations)

    @property
    def planned_change_count(self) -> int:
        """Count of write operations that mutate disk (CREATE, UNLINK, MIGRATE_CONTAINER)."""
        return sum(
            1
            for op in self.all_operations
            if op.action in ("CREATE", "UNLINK", "MIGRATE_CONTAINER")
        )

    @property
    def same_object_count(self) -> int:
        return sum(1 for op in self.all_operations if op.action == "SHARED_PATH")

    @property
    def noop_count(self) -> int:
        return sum(1 for op in self.all_operations if op.action == "NOOP")

    @property
    def skip_count(self) -> int:
        return sum(1 for op in self.all_operations if op.action == "SKIP")


def build_global_skill_batch(
    aikito_dir: Path,
    home: Path,
    *,
    skills: Sequence[str] | None = None,
    registry: AgentRegistry | None = None,
) -> GlobalSkillBatch:
    """Construct GlobalSkillBatch from workspace configuration and host filesystem facts."""
    if skills is None:
        skills = load_global_skills_list(aikito_dir)
    selected_set = set(skills)

    container_path = home / ".agents" / "skills"
    canonical_skills_root = aikito_dir / "skills"

    container_target = Target(
        kind="managed_container",
        scope="global",
        path=container_path,
        canonical_source=canonical_skills_root,
    )

    selected_entries = tuple(
        Target(
            kind="managed_entry",
            scope="global",
            path=container_path / name,
            canonical_source=canonical_skills_root / name,
        )
        for name in skills
    )

    # Discover stale entries if container exists and is a real directory
    stale_entries: list[Target] = []
    if container_path.is_dir() and not container_path.is_symlink():
        try:
            for entry in sorted(container_path.iterdir(), key=lambda p: p.name):
                if entry.name not in selected_set:
                    stale_entries.append(
                        Target(
                            kind="managed_entry",
                            scope="global",
                            path=entry,
                            canonical_source=canonical_skills_root / entry.name,
                        )
                    )
        except OSError:
            pass

    consumer_targets = resolve_targets(
        "global_skills", aikito_dir, home, registry=registry
    )

    return GlobalSkillBatch(
        workspace_root=aikito_dir,
        container=container_target,
        selected_entries=selected_entries,
        stale_entries=tuple(stale_entries),
        consumers=consumer_targets,
    )


def plan_global_skills(
    batch: GlobalSkillBatch,
    home: Path,
    *,
    dry_run: bool = False,
    refreshed_bundled: set[str] | None = None,
) -> GlobalSkillBatchPlan:
    """Pure planning function producing GlobalSkillBatchPlan from a GlobalSkillBatch."""
    refreshed = refreshed_bundled or set()

    # 1. Plan Container
    is_legacy = batch.container.path.is_symlink()
    container_obs = inspect_link_target(
        batch.container.path,
        batch.container.canonical_source,
        target_kind="managed_container",
    )
    container_op = plan_link_target(
        container_obs,
        desired_mode="link",
        is_legacy_container=is_legacy,
    )

    # 2. Plan Selected Entries
    entry_ops: list[LinkOperation] = []
    for entry in batch.selected_entries:
        canonical_path = entry.canonical_source
        canonical_valid = True
        canonical_error = None

        if canonical_path is not None:
            # Check canonical source validity
            if dry_run and entry.path.name in refreshed and not canonical_path.exists():
                canonical_valid = True
            elif not canonical_path.exists():
                canonical_valid = False
                canonical_error = f"Canonical skill source does not exist: {canonical_path}"
            elif not canonical_path.is_dir():
                canonical_valid = False
                canonical_error = f"Canonical skill source is not a directory: {canonical_path}"

        obs = inspect_link_target(
            entry.path,
            canonical_path,
            canonical_valid=canonical_valid,
            canonical_error=canonical_error,
            target_kind="managed_entry",
        )
        op = plan_link_target(
            obs,
            desired_mode="link",
            resource_name=entry.path.name,
        )
        entry_ops.append(op)

    # 3. Plan Stale Entries (deselected)
    for stale in batch.stale_entries:
        canonical_path = stale.canonical_source
        obs = inspect_link_target(
            stale.path,
            canonical_path,
            canonical_valid=True,
            target_kind="managed_entry",
        )
        op = plan_link_target(
            obs,
            desired_mode="absent",
            resource_name=stale.path.name,
        )
        entry_ops.append(op)

    # 4. Plan Consumer Targets
    consumer_ops: list[LinkOperation] = []
    for c_target in batch.consumers:
        # Check availability
        avail = check_target_availability(c_target, home)
        parent_exists = c_target.path.parent.exists()

        obs = inspect_link_target(
            c_target.path,
            c_target.canonical_source,
            target_kind="consumer_link",
            is_same_object=c_target.is_same_object,
        )
        op = plan_link_target(
            obs,
            desired_mode="link",
            availability_status=avail.status,
            parent_exists=parent_exists,
            resource_name="/".join(c_target.consumer_display_names),
        )
        consumer_ops.append(op)

    return GlobalSkillBatchPlan(
        batch=batch,
        container_op=container_op,
        entry_ops=tuple(entry_ops),
        consumer_ops=tuple(consumer_ops),
    )
