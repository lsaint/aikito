"""Project skill local state storage, locking, content fingerprinting, and transactions.

This module implements host-local copy state records, monotonic generation tracking,
cross-platform writer locks, whole-tree directory fingerprinting, and recovery journals
governed by Aikito engineering invariants.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

if sys.platform != "win32":
    import fcntl
else:
    import msvcrt

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from .compat import (
    check_directory_permissions,
    get_physical_path,
    is_reparse_point,
    is_windows,
    normalize_file_bytes,
    safe_symlink,
    secure_directory_permissions,
    secure_file_permissions,
)


@dataclass(frozen=True)
class SkillStateRecord:
    """Host-local state record for one synchronized project copy skill."""

    skill_name: str
    representation: str  # "copy"
    lifecycle: str  # "active" or "inactive"
    baseline_fingerprint: str  # Content fingerprint B at convergence
    baseline_origin: str  # "write", "reconcile", "claim", or "reactivate"
    last_observed_selected: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "skill_name": self.skill_name,
            "representation": self.representation,
            "lifecycle": self.lifecycle,
            "baseline_fingerprint": self.baseline_fingerprint,
            "baseline_origin": self.baseline_origin,
            "last_observed_selected": self.last_observed_selected,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkillStateRecord:
        return cls(
            skill_name=str(data["skill_name"]),
            representation=str(data.get("representation", "copy")),
            lifecycle=str(data["lifecycle"]),
            baseline_fingerprint=str(data["baseline_fingerprint"]),
            baseline_origin=str(data.get("baseline_origin", "write")),
            last_observed_selected=bool(data.get("last_observed_selected", True)),
        )


@dataclass
class ProjectSkillStateDocument:
    """Document holding all skill copy records for one workspace + project + checkout binding."""

    version: int
    generation: int
    workspace_root: str
    project_name: str
    physical_checkout: str
    records: dict[str, SkillStateRecord] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generation": self.generation,
            "workspace_root": self.workspace_root,
            "project_name": self.project_name,
            "physical_checkout": self.physical_checkout,
            "records": {k: v.to_dict() for k, v in sorted(self.records.items())},
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> ProjectSkillStateDocument:
        version = int(data.get("version", 1))
        generation = int(data.get("generation", 0))
        workspace_root = str(data["workspace_root"])
        project_name = str(data["project_name"])
        physical_checkout = str(data["physical_checkout"])
        raw_records = data.get("records", {})
        records = {
            str(k): SkillStateRecord.from_dict(v) for k, v in raw_records.items()
        }
        return cls(
            version=version,
            generation=generation,
            workspace_root=workspace_root,
            project_name=project_name,
            physical_checkout=physical_checkout,
            records=records,
        )


def calculate_directory_fingerprint(dir_path: Path) -> tuple[str | None, str | None]:
    """Calculate whole-tree content fingerprint for a directory.

    Returns (fingerprint, error).
    Fingerprint captures file relative paths, byte hashes, empty directories, and
    POSIX executable permissions. Excludes mtime and inodes.
    Rejects internal symlinks, sockets, FIFOs, and devices.
    """
    if not dir_path.exists():
        return None, f"Directory does not exist: {dir_path}"
    if is_reparse_point(dir_path):
        return (
            None,
            f"Directory root cannot be a symbolic link or reparse point: {dir_path}",
        )
    if not dir_path.is_dir():
        return None, f"Path is not a directory: {dir_path}"

    entries: list[str] = []
    try:
        for root_str, dir_names, file_names in os.walk(dir_path, followlinks=False):
            current_root = Path(root_str)
            if not dir_names and not file_names and current_root != dir_path:
                rel = current_root.relative_to(dir_path).as_posix()
                entries.append(f"d {rel}")

            for d_name in dir_names:
                sub_dir = current_root / d_name
                if is_reparse_point(sub_dir):
                    return (
                        None,
                        f"Symbolic links are not supported inside copied skills: {sub_dir}",
                    )

            for f_name in file_names:
                file_path = current_root / f_name
                if is_reparse_point(file_path):
                    return (
                        None,
                        f"Symbolic links are not supported inside copied skills: {file_path}",
                    )
                try:
                    st = file_path.lstat()
                except OSError as exc:
                    return None, f"Failed to inspect file {file_path}: {exc}"

                mode = st.st_mode
                if stat.S_ISREG(mode):
                    try:
                        content = file_path.read_bytes()
                    except OSError as exc:
                        return None, f"Failed to read file {file_path}: {exc}"
                    file_hash = hashlib.sha256(content).hexdigest()
                    rel = file_path.relative_to(dir_path).as_posix()
                    is_exec = not is_windows() and bool(
                        mode & (stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                    )
                    exec_suffix = " *" if is_exec else ""
                    entries.append(f"f {rel} {file_hash}{exec_suffix}")
                else:
                    return None, f"Unsupported filesystem entry: {file_path}"
    except Exception as exc:
        return None, str(exc)

    entries.sort()
    raw = "\n".join(entries).encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    return f"v1:{digest}", None


def get_skill_state_dir(home: Path) -> Path:
    """Return canonical path to project-skills state directory."""
    return home / ".local" / "state" / "aikito" / "project-skills"


def _normalize_identity_path(path: Path) -> str:
    """Normalize a path for cross-platform binding identity hashing."""
    phys = get_physical_path(path)
    posix_str = phys.as_posix()
    if is_windows():
        # Only fold case when the target directory is actually case-insensitive.
        # Per-directory case-sensitivity (Win10 1803+) means we must not lowercase
        # paths inside such directories, or distinct checkouts would share a binding.
        from .compat import is_directory_case_sensitive

        if not is_directory_case_sensitive(phys):
            posix_str = posix_str.lower()
    return posix_str


def get_binding_hash(
    workspace_root: Path, project_name: str, checkout_path: Path
) -> str:
    """Compute deterministic binding hash for workspace + project + checkout identity."""
    ws_norm = _normalize_identity_path(workspace_root)
    proj_norm = project_name.strip()
    co_norm = _normalize_identity_path(checkout_path)
    token = f"{ws_norm}:{proj_norm}:{co_norm}".encode("utf-8")
    return hashlib.sha256(token).hexdigest()


def validate_state_store_root(
    home: Path, create_if_missing: bool = False
) -> tuple[Path, str | None]:
    """Validate that state directory path components are trusted, unaliased, and secure."""
    state_dir = get_skill_state_dir(home)
    if not state_dir.exists():
        if not create_if_missing:
            return state_dir, None
        try:
            state_dir.mkdir(parents=True, exist_ok=True)
            secure_directory_permissions(state_dir)
        except OSError as exc:
            return state_dir, f"Failed to create state directory {state_dir}: {exc}"

    curr = state_dir
    while curr != home and curr != curr.parent:
        if is_reparse_point(curr):
            return (
                state_dir,
                f"State directory component is a symbolic link or reparse point: {curr}",
            )
        is_sec, desc = check_directory_permissions(curr)
        if not is_sec:
            return (
                state_dir,
                f"Insecure permissions on state directory {curr}: {desc}",
            )
        curr = curr.parent

    return state_dir, None


def load_project_skill_state(
    home: Path, workspace_root: Path, project_name: str, checkout_path: Path
) -> tuple[ProjectSkillStateDocument | None, str | None]:
    """Load project skill state document for a binding.

    Returns (document, error). If file does not exist, returns (None, None).
    """
    state_dir, error = validate_state_store_root(home, create_if_missing=False)
    if error:
        return None, error
    if not state_dir.exists():
        return None, None

    binding_hash = get_binding_hash(workspace_root, project_name, checkout_path)
    state_file = state_dir / f"{binding_hash}.json"
    if not state_file.exists():
        return None, None

    if is_reparse_point(state_file):
        return (
            None,
            f"State file cannot be a symbolic link or reparse point: {state_file}",
        )

    try:
        text = state_file.read_text(encoding="utf-8")
        data = json.loads(text)
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"Failed to read state file {state_file}: {exc}"

    try:
        doc = ProjectSkillStateDocument.from_dict(data)
    except Exception as exc:
        return None, f"Malformed state document schema in {state_file}: {exc}"

    # Validate binding identity embedded in document
    expected_ws = _normalize_identity_path(workspace_root)
    expected_co = _normalize_identity_path(checkout_path)
    if (
        _normalize_identity_path(Path(doc.workspace_root)) != expected_ws
        or doc.project_name != project_name
        or _normalize_identity_path(Path(doc.physical_checkout)) != expected_co
    ):
        return (
            None,
            f"State file {state_file} binding identity mismatch: "
            f"expected ({expected_ws}, {project_name}, {expected_co}), "
            f"found ({doc.workspace_root}, {doc.project_name}, {doc.physical_checkout})",
        )

    return doc, None


def save_project_skill_state(
    home: Path,
    doc: ProjectSkillStateDocument,
    expected_generation: int | None = None,
) -> tuple[bool, str | None]:
    """Atomically commit an updated state document with generation guard."""
    state_dir, error = validate_state_store_root(home, create_if_missing=True)
    if error:
        return False, error

    workspace_root = Path(doc.workspace_root)
    checkout_path = Path(doc.physical_checkout)
    binding_hash = get_binding_hash(workspace_root, doc.project_name, checkout_path)
    state_file = state_dir / f"{binding_hash}.json"

    # If all records are removed, clean up state file
    if not doc.records:
        if state_file.exists():
            try:
                state_file.unlink()
            except OSError as exc:
                return False, f"Failed to remove empty state file {state_file}: {exc}"
        return True, None

    # Verify generation if expected_generation provided
    if expected_generation is not None:
        if state_file.exists():
            current_doc, load_err = load_project_skill_state(
                home, workspace_root, doc.project_name, checkout_path
            )
            if load_err:
                return False, f"Cannot verify generation: {load_err}"
            if current_doc and current_doc.generation != expected_generation:
                return (
                    False,
                    f"State generation mismatch for {state_file}: "
                    f"expected {expected_generation}, found {current_doc.generation}",
                )

    doc.generation += 1
    content = json.dumps(doc.to_dict(), indent=2, sort_keys=True)

    tmp_fd, tmp_path_str = tempfile.mkstemp(
        prefix=f".{binding_hash}.", suffix=".tmp", dir=state_dir
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
        secure_file_permissions(tmp_path)
        os.replace(tmp_path, state_file)
    except Exception as exc:
        if tmp_path.exists():
            tmp_path.unlink()
        return False, f"Failed to atomically write state file {state_file}: {exc}"

    return True, None


class SkillWriterLock:
    """Re-entrant cross-platform exclusive writer lock for skill mutations."""

    _process_lock = threading.RLock()
    _lock_depth: int = 0
    _lock_file_obj: Any = None
    _active_lock_path: Path | None = None

    def __init__(self, home: Path) -> None:
        self.home = home
        self.state_dir = get_skill_state_dir(home)
        self.lock_path = self.state_dir / "writer.lock"
        self._instance_depth = 0

    def acquire(self) -> None:
        SkillWriterLock._process_lock.acquire()
        try:
            if SkillWriterLock._lock_depth > 0:
                if SkillWriterLock._active_lock_path != self.lock_path:
                    raise RuntimeError(
                        "Cannot nest skill writer locks for different state stores"
                    )
                SkillWriterLock._lock_depth += 1
                self._instance_depth += 1
                return

            state_dir, error = validate_state_store_root(
                self.home, create_if_missing=True
            )
            if error:
                raise RuntimeError(
                    f"Failed to validate state store root {state_dir}: {error}"
                )
            if is_reparse_point(self.lock_path):
                raise RuntimeError(
                    f"Writer lock file is a reparse point or symlink: {self.lock_path}"
                )

            f = open(self.lock_path, "a+", encoding="utf-8")
            try:
                secure_file_permissions(self.lock_path)
                if not is_windows():
                    fcntl.flock(f.fileno(), fcntl.LOCK_EX)
                else:
                    f.seek(0)
                    msvcrt.locking(f.fileno(), msvcrt.LK_LOCK, 1)
            except Exception:
                f.close()
                raise

            SkillWriterLock._lock_file_obj = f
            SkillWriterLock._active_lock_path = self.lock_path
            SkillWriterLock._lock_depth = 1
            self._instance_depth = 1
        except Exception:
            SkillWriterLock._process_lock.release()
            raise

    def release(self) -> None:
        if self._instance_depth <= 0:
            return
        try:
            self._instance_depth -= 1
            SkillWriterLock._lock_depth -= 1
            if SkillWriterLock._lock_depth == 0:
                f = SkillWriterLock._lock_file_obj
                SkillWriterLock._lock_file_obj = None
                SkillWriterLock._active_lock_path = None
                if f:
                    try:
                        if not is_windows():
                            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
                        else:
                            try:
                                f.seek(0)
                                msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
                            except Exception:
                                pass
                    finally:
                        f.close()
        finally:
            SkillWriterLock._process_lock.release()

    def __enter__(self) -> SkillWriterLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()


@dataclass
class SkillTransactionJournal:
    """Persistent transaction record for rollback and recovery passes."""

    tx_id: str
    kind: str  # "runtime_apply" or "selection_mutation"
    phase: str  # "pending" or "committed"
    workspace_root: str
    project_names: list[str] = field(default_factory=list)
    checkout_paths: list[str] = field(default_factory=list)
    affected_skills: list[str] = field(default_factory=list)
    files: list[dict[str, Any]] = field(default_factory=list)
    recovery_dirs: list[dict[str, str]] = field(default_factory=list)
    state_transitions: list[dict[str, Any]] = field(default_factory=list)
    symlinks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tx_id": self.tx_id,
            "kind": self.kind,
            "phase": self.phase,
            "workspace_root": self.workspace_root,
            "project_names": self.project_names,
            "checkout_paths": self.checkout_paths,
            "affected_skills": self.affected_skills,
            "files": self.files,
            "recovery_dirs": self.recovery_dirs,
            "state_transitions": self.state_transitions,
            "symlinks": self.symlinks,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> SkillTransactionJournal:
        return cls(
            tx_id=str(data["tx_id"]),
            kind=str(data.get("kind", "runtime_apply")),
            phase=str(data.get("phase", "pending")),
            workspace_root=str(data["workspace_root"]),
            project_names=[str(p) for p in data.get("project_names", [])],
            checkout_paths=[str(c) for c in data.get("checkout_paths", [])],
            affected_skills=[str(s) for s in data.get("affected_skills", [])],
            files=list(data.get("files", [])),
            recovery_dirs=list(data.get("recovery_dirs", [])),
            state_transitions=list(data.get("state_transitions", [])),
            symlinks=list(data.get("symlinks", [])),
        )


def get_transactions_dir(home: Path) -> Path:
    """Return transactions directory under project-skills state root."""
    return get_skill_state_dir(home) / "transactions"


def write_transaction_journal(
    home: Path, journal: SkillTransactionJournal
) -> tuple[Path | None, str | None]:
    """Persist a transaction journal in transactions/<tx_id>/journal.json."""
    tx_root = get_transactions_dir(home)
    tx_dir = tx_root / journal.tx_id
    try:
        tx_dir.mkdir(parents=True, exist_ok=True)
        secure_directory_permissions(tx_dir)
        journal_path = tx_dir / "journal.json"
        content = json.dumps(journal.to_dict(), indent=2, sort_keys=True)
        tmp_path = tx_dir / ".journal.tmp"
        tmp_path.write_text(content, encoding="utf-8")
        secure_file_permissions(tmp_path)
        os.replace(tmp_path, journal_path)
        return journal_path, None
    except Exception as exc:
        return None, f"Failed to persist transaction journal {journal.tx_id}: {exc}"


_TX_ID_RE = re.compile(r"^[a-zA-Z0-9_\-]{1,64}$")


def _is_safe_tx_id(tx_id: str) -> bool:
    """Return True only when *tx_id* is a safe non-empty alphanumeric/dash/underscore token."""
    return bool(_TX_ID_RE.match(tx_id))


def _assert_path_within(path: Path, root: Path, label: str) -> str | None:
    """Return an error string if path does not resolve strictly under root."""
    try:
        resolved = get_physical_path(path)
        root_resolved = get_physical_path(root)
        if resolved == root_resolved or not resolved.is_relative_to(root_resolved):
            return f"Journal {label} path escapes trusted root ({root_resolved}): {resolved}"
    except Exception as exc:
        return f"Journal {label} path resolution failed: {exc}"
    return None


def _is_valid_file_path(path: Path, ws_root: Path, checkouts: Sequence[Path]) -> bool:
    """Return True if file resolves strictly inside workspace or a valid checkout."""
    try:
        p_res = get_physical_path(path)
        ws_root_res = get_physical_path(ws_root)
        if p_res != ws_root_res and p_res.is_relative_to(ws_root_res):
            return True
        for co in checkouts:
            co_res = get_physical_path(co)
            if p_res != co_res and p_res.is_relative_to(co_res):
                return True
    except Exception:
        pass
    return False


def _is_valid_staging_or_recovery_dir(
    path: Path, tx_id: str, ws_root: Path, checkouts: Sequence[Path]
) -> bool:
    """Return True only if path is located strictly inside .aikito-tx/<tx_id> in workspace or checkout."""
    if not _is_safe_tx_id(tx_id):
        return False
    try:
        # Check parent directory resolution to avoid resolving path itself when it is a symlink
        parent_res = get_physical_path(path.parent)
        name = path.name
        if not name or name in (".", ".."):
            return False

        ws_tx = get_physical_path(ws_root / ".aikito-tx" / tx_id)
        if parent_res == ws_tx or parent_res.is_relative_to(ws_tx):
            return True
        for co in checkouts:
            co_tx = get_physical_path(co / ".agents" / ".aikito-tx" / tx_id)
            if parent_res == co_tx or parent_res.is_relative_to(co_tx):
                return True
    except Exception:
        pass
    return False


def _is_valid_target_path(path: Path, ws_root: Path, checkouts: Sequence[Path]) -> bool:
    """Return True if path is a valid skill target (a child of skills root in workspace or checkout)."""
    try:
        if not path.name or path.name in (".", ".."):
            return False
        parent_res = get_physical_path(path.parent)
        ws_skills = get_physical_path(ws_root / "skills")
        if parent_res == ws_skills:
            return True
        for co in checkouts:
            co_skills = get_physical_path(co / ".agents" / "skills")
            if parent_res == co_skills:
                return True
    except Exception:
        pass
    return False


def delete_transaction_journal(home: Path, tx_id: str) -> None:
    """Safely remove completed transaction directory."""
    if not _is_safe_tx_id(tx_id):
        # Refuse to delete an arbitrary path constructed from an untrusted tx_id.
        return
    tx_root = get_transactions_dir(home)
    tx_dir = tx_root / tx_id
    # Final safety: resolved path must stay within tx_root
    try:
        if not get_physical_path(tx_dir).is_relative_to(get_physical_path(tx_root)):
            return
    except Exception:
        return
    if tx_dir.is_dir():
        shutil.rmtree(tx_dir, ignore_errors=True)


def run_recovery_pass(
    home: Path,
    affected_workspace: Path,
    affected_projects: Sequence[str] | None = None,
    affected_skills: Sequence[str] | None = None,
    authorized_checkouts: Sequence[Path] | None = None,
) -> tuple[bool, str | None]:
    """Scan transactions and recover pending/committed transactions affecting this request.

    Must be called under writer lock.
    Returns (recovery_occurred, message_or_error).
    If an unrecoverable corrupted journal exists, raises or returns (False, error).
    """
    tx_root = get_transactions_dir(home)
    if not tx_root.exists() or not tx_root.is_dir():
        return False, None

    norm_ws = _normalize_identity_path(affected_workspace)
    proj_set = set(affected_projects) if affected_projects else set()
    skill_set = set(affected_skills) if affected_skills else set()

    recovery_occurred = False
    details: list[str] = []

    for item in sorted(tx_root.iterdir()):
        if not item.is_dir():
            continue
        if not _is_safe_tx_id(item.name):
            return (
                False,
                f"Unsafe transaction directory name {item.name}: must be a safe identifier. Manual recovery required.",
            )
        journal_path = item / "journal.json"
        if not journal_path.exists():
            # Incomplete transaction folder
            shutil.rmtree(item, ignore_errors=True)
            recovery_occurred = True
            details.append(f"Cleaned incomplete transaction directory: {item.name}")
            continue

        if is_reparse_point(journal_path):
            return (
                False,
                f"Transaction journal is an unsafe symlink/reparse point: {journal_path}",
            )

        try:
            data = json.loads(journal_path.read_text(encoding="utf-8"))
            journal = SkillTransactionJournal.from_dict(data)
        except Exception as exc:
            return (
                False,
                f"Corrupted transaction journal {journal_path}: {exc}. Manual recovery required.",
            )

        # Validate that the journal's tx_id matches the transaction directory name
        if journal.tx_id != item.name:
            return (
                False,
                f"Transaction journal ID mismatch ({journal.tx_id} != {item.name}) in {journal_path}. Manual recovery required.",
            )

        # Check if transaction affects this request
        j_ws = _normalize_identity_path(Path(journal.workspace_root))
        if j_ws != norm_ws:
            continue

        is_relevant = False
        if not proj_set and not skill_set:
            is_relevant = True
        else:
            if any(p in proj_set for p in journal.project_names):
                is_relevant = True
            if any(s in skill_set for s in journal.affected_skills):
                is_relevant = True

        if not is_relevant:
            continue

        # Enforce strict sandboxing: derive trusted checkouts from workspace configuration
        ws_resolved = get_physical_path(affected_workspace)
        derived_checkouts: set[Path] = set()
        if authorized_checkouts:
            derived_checkouts.update(
                get_physical_path(path) for path in authorized_checkouts
            )
        from .project import resolve_project_binding

        projects_dir = affected_workspace / "projects"
        if projects_dir.is_dir():
            for p_item in projects_dir.iterdir():
                if not p_item.is_dir():
                    continue
                cfg_file = p_item / "agent.toml"
                if cfg_file.is_file():
                    try:
                        cfg = tomllib.loads(cfg_file.read_text(encoding="utf-8"))
                        binding = resolve_project_binding(cfg, home)
                        for entry in binding.entries:
                            derived_checkouts.add(
                                get_physical_path(entry.resolved_path)
                            )
                    except Exception:
                        pass

        # A runtime transaction may have been persisted immediately before its
        # explicit candidate checkout was added to agent.toml.  The protected
        # journal's frozen post-image is the only durable authorization in that
        # crash window, so accept candidates only from the exact project config
        # entry and only while the live file is still one of the frozen images.
        if journal.kind == "runtime_apply":
            for file_entry in journal.files:
                try:
                    file_path = Path(str(file_entry["path"]))
                    project_name = file_path.parent.name
                    expected_path = projects_dir / project_name / "agent.toml"
                    if project_name not in journal.project_names or get_physical_path(
                        file_path
                    ) != get_physical_path(expected_path):
                        continue
                    pre_b64 = file_entry.get("pre_image_base64")
                    post_b64 = file_entry.get("post_image_base64")
                    if pre_b64 is None or post_b64 is None or not file_path.is_file():
                        continue
                    pre_bytes = base64.b64decode(pre_b64)
                    post_bytes = base64.b64decode(post_b64)
                    curr_bytes_norm = normalize_file_bytes(file_path.read_bytes())
                    if curr_bytes_norm not in (
                        normalize_file_bytes(pre_bytes),
                        normalize_file_bytes(post_bytes),
                    ):
                        continue
                    post_config = tomllib.loads(post_bytes.decode("utf-8"))
                    post_binding = resolve_project_binding(post_config, home)
                    for entry in post_binding.entries:
                        checkout = get_physical_path(entry.resolved_path)
                        has_binding_evidence = any(
                            transition.get("project_name") == project_name
                            and get_physical_path(
                                Path(str(transition.get("physical_checkout", "")))
                            )
                            == checkout
                            and transition.get("binding_hash")
                            == get_binding_hash(
                                affected_workspace, project_name, checkout
                            )
                            for transition in journal.state_transitions
                        )
                        if has_binding_evidence:
                            derived_checkouts.add(checkout)
                except Exception:
                    continue

        valid_checkouts: list[Path] = []
        for cp in journal.checkout_paths:
            try:
                cp_res = get_physical_path(Path(cp))
                if cp_res not in derived_checkouts:
                    return (
                        False,
                        f"Journal declared unauthorized checkout path not configured in workspace: {cp_res}",
                    )
                valid_checkouts.append(cp_res)
            except Exception as exc:
                return False, f"Failed to validate journal checkout path {cp}: {exc}"

        # Execute recovery for this relevant transaction
        if journal.phase == "committed":
            cleanup_errors: list[str] = []
            # Clean up lingering recovery/staging directories
            for rec in journal.recovery_dirs:
                rec_dir_str = rec.get("recovery_dir")
                stage_dir_str = rec.get("staging_dir")
                if rec_dir_str:
                    rec_dir = Path(rec_dir_str)
                    if not _is_valid_staging_or_recovery_dir(
                        rec_dir, journal.tx_id, ws_resolved, valid_checkouts
                    ):
                        return False, f"Journal recovery_dir escapes sandbox: {rec_dir}"
                    if rec_dir.exists() or rec_dir.is_symlink():
                        try:
                            if rec_dir.is_symlink() or rec_dir.is_file():
                                rec_dir.unlink()
                            elif rec_dir.is_dir():
                                shutil.rmtree(rec_dir)
                        except Exception as exc:
                            cleanup_errors.append(f"recovery_dir {rec_dir}: {exc}")
                if stage_dir_str:
                    stage_dir = Path(stage_dir_str)
                    if not _is_valid_staging_or_recovery_dir(
                        stage_dir, journal.tx_id, ws_resolved, valid_checkouts
                    ):
                        return (
                            False,
                            f"Journal staging_dir escapes sandbox: {stage_dir}",
                        )
                    if stage_dir.exists() or stage_dir.is_symlink():
                        try:
                            if stage_dir.is_symlink() or stage_dir.is_file():
                                stage_dir.unlink()
                            elif stage_dir.is_dir():
                                shutil.rmtree(stage_dir)
                        except Exception as exc:
                            cleanup_errors.append(f"staging_dir {stage_dir}: {exc}")

            for co in valid_checkouts:
                co_tx = co / ".agents" / ".aikito-tx" / journal.tx_id
                if co_tx.is_dir():
                    try:
                        shutil.rmtree(co_tx)
                    except Exception as exc:
                        cleanup_errors.append(f"checkout tx {co_tx}: {exc}")
            ws_tx = ws_resolved / ".aikito-tx" / journal.tx_id
            if ws_tx.is_dir():
                try:
                    shutil.rmtree(ws_tx)
                except Exception as exc:
                    cleanup_errors.append(f"workspace tx {ws_tx}: {exc}")

            if cleanup_errors:
                return (
                    False,
                    f"Failed to clean up committed transaction {journal.tx_id}: {'; '.join(cleanup_errors)}. Journal retained for retry.",
                )

            delete_transaction_journal(home, journal.tx_id)
            recovery_occurred = True
            details.append(f"Finalized committed transaction {journal.tx_id}")
        else:
            # Pending phase -> rollback
            state_root = get_physical_path(get_skill_state_dir(home))

            # 1. Restore files (strictly within workspace or valid checkouts)
            for file_entry in reversed(journal.files):
                f_path = Path(file_entry["path"])
                if not _is_valid_file_path(f_path, ws_resolved, valid_checkouts):
                    return False, f"Journal file path escapes sandbox: {f_path}"
                pre_b64 = file_entry.get("pre_image_base64")
                post_b64 = file_entry.get("post_image_base64")
                pre_bytes = base64.b64decode(pre_b64) if pre_b64 is not None else None
                post_bytes = (
                    base64.b64decode(post_b64) if post_b64 is not None else None
                )

                try:
                    if not f_path.exists():
                        if pre_bytes is None:
                            continue
                        elif post_bytes is None:
                            f_path.parent.mkdir(parents=True, exist_ok=True)
                            f_path.write_bytes(pre_bytes)
                            continue
                        else:
                            return (
                                False,
                                f"Concurrent modification detected in file {f_path} (externally deleted); recovery aborted to preserve pending journal {journal.tx_id}",
                            )

                    curr_bytes = f_path.read_bytes()
                    curr_norm = normalize_file_bytes(curr_bytes)
                    pre_norm = normalize_file_bytes(pre_bytes)
                    post_norm = normalize_file_bytes(post_bytes)

                    # If already pre-image, nothing to do
                    if pre_norm is not None and curr_norm == pre_norm:
                        continue

                    # If post_bytes was recorded and current matches neither pre nor post:
                    # File was modified concurrently; abort recovery to preserve pending journal
                    if post_norm is not None and curr_norm != post_norm:
                        return (
                            False,
                            f"Concurrent modification detected in file {f_path}; recovery aborted to preserve pending journal {journal.tx_id}",
                        )

                    # If matches post_bytes (the transaction's written state) or post_bytes was not recorded:
                    if pre_bytes is None:
                        f_path.unlink()
                    else:
                        f_path.write_bytes(pre_bytes)
                except Exception as exc:
                    return (
                        False,
                        f"Failed to restore file {f_path} during recovery: {exc}",
                    )

            # 2. Restore symlinks
            for sym_entry in reversed(journal.symlinks):
                target_path_str = sym_entry.get("target_path")
                if not target_path_str:
                    continue
                target_path = Path(target_path_str)
                if not _is_valid_target_path(target_path, ws_resolved, valid_checkouts):
                    return (
                        False,
                        f"Journal symlink target_path escapes sandbox: {target_path}",
                    )
                pre_link = sym_entry.get("pre_link")
                post_link = sym_entry.get("post_link")

                try:
                    curr_is_symlink = target_path.is_symlink()
                    curr_target = os.readlink(target_path) if curr_is_symlink else None

                    # If already pre-image, nothing to do
                    if curr_target == pre_link and (
                        pre_link is not None or not target_path.exists()
                    ):
                        continue

                    if post_link is not None:
                        # Transaction created or updated this symlink
                        if curr_is_symlink and (
                            curr_target == post_link
                            or get_physical_path(Path(curr_target))
                            == get_physical_path(Path(post_link))
                        ):
                            target_path.unlink()
                            if pre_link is not None:
                                safe_symlink(Path(pre_link), target_path)
                            continue
                        else:
                            return (
                                False,
                                f"Concurrent modification detected in symlink {target_path}; recovery aborted to preserve pending journal {journal.tx_id}",
                            )
                    else:
                        # Transaction unlinked this symlink (post_link is None)
                        if pre_link is not None:
                            if curr_is_symlink or target_path.exists():
                                return (
                                    False,
                                    f"Concurrent modification detected at {target_path}; recovery aborted to preserve pending journal {journal.tx_id}",
                                )
                            safe_symlink(Path(pre_link), target_path)
                        else:
                            if curr_is_symlink:
                                target_path.unlink()
                except Exception as exc:
                    return (
                        False,
                        f"Failed to restore symlink {target_path} during recovery: {exc}",
                    )

            # 3. Restore targets from recovery_dirs
            for rec in journal.recovery_dirs:
                target_path_str = rec.get("target_path")
                rec_dir = Path(rec["recovery_dir"]) if rec.get("recovery_dir") else None
                stage_dir = Path(rec["staging_dir"]) if rec.get("staging_dir") else None

                if stage_dir:
                    if not _is_valid_staging_or_recovery_dir(
                        stage_dir, journal.tx_id, ws_resolved, valid_checkouts
                    ):
                        return (
                            False,
                            f"Journal staging_dir escapes sandbox: {stage_dir}",
                        )
                if rec_dir:
                    if not _is_valid_staging_or_recovery_dir(
                        rec_dir, journal.tx_id, ws_resolved, valid_checkouts
                    ):
                        return False, f"Journal recovery_dir escapes sandbox: {rec_dir}"

                if target_path_str:
                    target_path = Path(target_path_str)
                    if not _is_valid_target_path(
                        target_path, ws_resolved, valid_checkouts
                    ):
                        return (
                            False,
                            f"Journal target_path escapes sandbox: {target_path}",
                        )

                if stage_dir and (stage_dir.exists() or stage_dir.is_symlink()):
                    try:
                        if stage_dir.is_symlink() or stage_dir.is_file():
                            stage_dir.unlink()
                        elif stage_dir.is_dir():
                            shutil.rmtree(stage_dir)
                    except Exception:
                        pass

                pre_fp = rec.get("pre_fingerprint")
                post_fp = rec.get("post_fingerprint")

                if (
                    rec_dir
                    and (rec_dir.exists() or rec_dir.is_symlink())
                    and target_path_str
                ):
                    try:
                        if target_path.is_symlink():
                            if post_fp is None:
                                expected_target = rec.get("expected_symlink_target")
                                if not expected_target and target_path_str:
                                    expected_target = str(
                                        ws_resolved / "skills" / target_path.name
                                    )
                                curr_link = os.readlink(target_path)
                                if expected_target:
                                    exp_res = get_physical_path(Path(expected_target))
                                    curr_res = get_physical_path(Path(curr_link))
                                    if (
                                        curr_link != expected_target
                                        and curr_res != exp_res
                                    ):
                                        return (
                                            False,
                                            f"Concurrent modification detected at {target_path} (symlink target mismatch: expected {expected_target}, found {curr_link}); recovery aborted to preserve pending journal {journal.tx_id}",
                                        )
                                target_path.unlink()
                                rec_dir.replace(target_path)
                            else:
                                return (
                                    False,
                                    f"Concurrent modification detected at {target_path} (expected dir, found symlink); recovery aborted to preserve pending journal {journal.tx_id}",
                                )
                        elif target_path.is_file():
                            return (
                                False,
                                f"Concurrent modification detected at {target_path} (expected dir, found file); recovery aborted to preserve pending journal {journal.tx_id}",
                            )
                        elif target_path.is_dir():
                            curr_fp, _ = calculate_directory_fingerprint(target_path)
                            if pre_fp is not None and curr_fp == pre_fp:
                                pass
                            elif post_fp is not None and curr_fp == post_fp:
                                shutil.rmtree(target_path)
                                rec_dir.replace(target_path)
                            elif post_fp is None and pre_fp is None:
                                shutil.rmtree(target_path)
                                rec_dir.replace(target_path)
                            else:
                                return (
                                    False,
                                    f"Concurrent modification detected in target directory {target_path}; recovery aborted to preserve pending journal {journal.tx_id}",
                                )
                        else:
                            rec_dir.replace(target_path)
                    except Exception as exc:
                        return (
                            False,
                            f"Failed to restore target {target_path} during recovery: {exc}",
                        )
                elif rec.get("created") and target_path_str:
                    if target_path.exists() or target_path.is_symlink():
                        try:
                            if target_path.is_symlink() or target_path.is_file():
                                return (
                                    False,
                                    f"Concurrent modification detected at {target_path} (unexpected non-directory created); recovery aborted to preserve pending journal {journal.tx_id}",
                                )
                            elif target_path.is_dir():
                                curr_fp, _ = calculate_directory_fingerprint(
                                    target_path
                                )
                                if post_fp is not None and curr_fp == post_fp:
                                    shutil.rmtree(target_path)
                                elif post_fp is None:
                                    shutil.rmtree(target_path)
                                else:
                                    return (
                                        False,
                                        f"Concurrent modification detected in newly created target directory {target_path}; recovery aborted to preserve pending journal {journal.tx_id}",
                                    )
                        except Exception as exc:
                            return (
                                False,
                                f"Failed to remove newly created target {target_path} during recovery: {exc}",
                            )

            # 4. Restore state transitions (binding_hash must be hex-only; path within state dir)
            _HASH_RE = re.compile(r"^[0-9a-f]{1,128}$")
            for st in journal.state_transitions:
                pre_doc_dict = st.get("pre_doc")
                b_hash = st.get("binding_hash")
                if not b_hash or not _HASH_RE.match(str(b_hash)):
                    return (
                        False,
                        f"Journal state_transitions binding_hash is missing or not safe hex: {b_hash!r}",
                    )

                st_ws = st.get("workspace_root")
                st_proj = st.get("project_name")
                st_co = st.get("physical_checkout")
                if not (st_ws and st_proj and st_co):
                    return (
                        False,
                        f"Journal state transition missing identity metadata for {b_hash}",
                    )
                if _normalize_identity_path(Path(st_ws)) != norm_ws:
                    return (
                        False,
                        f"Journal state transition workspace mismatch for {b_hash}",
                    )
                if st_proj not in journal.project_names:
                    return (
                        False,
                        f"Journal state transition project mismatch for {b_hash}",
                    )
                try:
                    co_resolved = get_physical_path(Path(st_co))
                    if co_resolved not in valid_checkouts:
                        return (
                            False,
                            f"Journal state transition checkout not authorized for {b_hash}",
                        )
                    expected_hash = get_binding_hash(Path(st_ws), st_proj, co_resolved)
                    if expected_hash != b_hash:
                        return (
                            False,
                            f"Journal state transition binding hash mismatch for {b_hash}",
                        )
                except Exception as exc:
                    return (
                        False,
                        f"Journal state transition validation error for {b_hash}: {exc}",
                    )

                st_file = get_skill_state_dir(home) / f"{b_hash}.json"
                if not get_physical_path(st_file).is_relative_to(state_root):
                    return (
                        False,
                        f"Journal state file path validation failed: {st_file} outside state root",
                    )

                pre_gen = st.get("pre_generation", 0)
                post_docs = st.get("post_docs")
                if not isinstance(post_docs, list):
                    return (
                        False,
                        f"Journal state transition lacks exact post-images for {b_hash}",
                    )

                try:
                    if not st_file.exists():
                        if pre_doc_dict is None:
                            continue
                        else:
                            return (
                                False,
                                f"Concurrent modification detected in state file {st_file} (externally deleted); recovery aborted to preserve pending journal {journal.tx_id}",
                            )

                    curr_data = json.loads(st_file.read_text(encoding="utf-8"))

                    if pre_doc_dict is None:
                        if curr_data not in post_docs:
                            return (
                                False,
                                f"Concurrent modification detected in state file {st_file}; recovery aborted to preserve pending journal {journal.tx_id}",
                            )
                        st_file.unlink()
                    else:
                        if curr_data == pre_doc_dict:
                            continue
                        if curr_data in post_docs:
                            content = json.dumps(pre_doc_dict, indent=2, sort_keys=True)
                            st_file.write_text(content, encoding="utf-8")
                            secure_file_permissions(st_file)
                        else:
                            return (
                                False,
                                f"Concurrent modification detected in state file {st_file} (generation {curr_data.get('generation', 0)} is not a recorded transaction post-image after {pre_gen}); recovery aborted to preserve pending journal {journal.tx_id}",
                            )
                except Exception as exc:
                    return (
                        False,
                        f"Failed to restore state {b_hash} during recovery: {exc}",
                    )

            # Clean up transaction staging directories across checkouts and workspace
            pending_cleanup_errors: list[str] = []
            for co in valid_checkouts:
                co_tx = co / ".agents" / ".aikito-tx" / journal.tx_id
                if co_tx.is_dir():
                    try:
                        shutil.rmtree(co_tx)
                    except Exception as exc:
                        pending_cleanup_errors.append(f"checkout tx {co_tx}: {exc}")
            ws_tx = ws_resolved / ".aikito-tx" / journal.tx_id
            if ws_tx.is_dir():
                try:
                    shutil.rmtree(ws_tx)
                except Exception as exc:
                    pending_cleanup_errors.append(f"workspace tx {ws_tx}: {exc}")

            if pending_cleanup_errors:
                return (
                    False,
                    f"Failed to clean up staging directories for pending transaction {journal.tx_id}: {'; '.join(pending_cleanup_errors)}. Journal retained for retry.",
                )

            delete_transaction_journal(home, journal.tx_id)
            recovery_occurred = True
            details.append(f"Rolled back interrupted transaction {journal.tx_id}")

    if recovery_occurred:
        return True, "; ".join(details)
    return False, None
