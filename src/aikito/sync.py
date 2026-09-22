"""Resource synchronization primitives shared by global and project sync flows.

Legacy synchronization helpers (sync_resource, apply_runtime_cleanup) have been
retired in Phase 6. All project and global resources are synchronized through
unified Target -> Inspect -> Plan -> Execute architectures.
"""

from __future__ import annotations

from pathlib import Path


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
