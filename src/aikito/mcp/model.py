"""Domain models, dataclasses, constants, and exceptions for MCP."""

import base64
import hashlib
import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.request import HTTPRedirectHandler

from ..config_runtime import ConfigTarget, FileSnapshot, StaleConfigPlanError
from ..diagnostics import Finding, is_error_finding
from ..plan_observation import (
    PlanObservation,
    PlanOperationView,
)
from .redact import redact_mcp_entry

STATE_VERSION = 1
DEFAULT_MCPS_DIR = Path("mcps")
DEFAULT_AGENTS_CONFIG = Path("agents.toml")
STATE_FILE = Path(".local/state/aikito/mcp-state.json")
BACKUP_DIR = Path(".local/state/aikito/backups")
LEGACY_PLACEHOLDER_TOKEN = "placeholder-token-set-environment-variable"
BROWSER_HELPER = """#!/usr/bin/env python3
import os
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


urls = [argument for argument in sys.argv[1:] if argument.startswith(("http://", "https://"))]
url_file = os.environ.get("AIKITO_AUTH_URL_FILE")
if url_file and urls:
    with Path(url_file).open("a", encoding="utf-8") as handle:
        handle.writelines(f"{url}\\n" for url in urls)

if os.environ.get("AIKITO_OPEN_BROWSER") == "1":
    for url in urls:
        if sys.platform == "win32" and hasattr(os, "startfile"):
            try:
                os.startfile(url)
                continue
            except OSError:
                pass
        browser_env = os.environ.copy()
        browser_env.pop("BROWSER", None)
        if sys.platform == "darwin":
            subprocess.Popen(
                ["/usr/bin/open", url],
                env=browser_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        else:
            opener = shutil.which("xdg-open")
            if opener:
                subprocess.Popen(
                    [opener, url],
                    env=browser_env,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            else:
                webbrowser.open(url)
"""


class MCPConfigError(RuntimeError):
    """Raised when an MCP definition or target config cannot be safely managed."""


class _MCPProbeError(RuntimeError):
    pass


class _RejectRedirects(HTTPRedirectHandler):
    """Keep configured credentials on exactly the configured MCP origin."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None


@dataclass(frozen=True)
class Token:
    kind: str
    text: str
    start: int
    end: int

    @property
    def value(self) -> Any:
        return json.loads(self.text) if self.kind == "string" else self.text


@dataclass(frozen=True)
class AgentSpec:
    agent: str
    server: str
    config_path: Path
    config_format: str
    target_name: str
    desired: dict[str, Any]
    enabled: bool = True
    reason: str = ""
    live_command: tuple[str, ...] = ()
    auth_command: tuple[str, ...] = ()
    contains_secret: bool = False
    missing_credential_env: str = ""
    home: Path | None = None

    @property
    def state_key(self) -> str:
        return f"{self.agent}:{self.server}"


@dataclass(frozen=True)
class BasicTokenAuth:
    """Keeps credential policy canonical while resolving secrets only at runtime."""

    account_email: str
    token_env: str
    authorization_env: str

    def authorization_header(self) -> str:
        token = os.environ.get(self.token_env)
        if not token:
            raise MCPConfigError(
                f"Required MCP credential environment variable is missing: "
                f"{self.token_env}"
            )
        credentials = f"{self.account_email}:{token}".encode()
        return f"Basic {base64.b64encode(credentials).decode()}"


@dataclass(frozen=True)
class LiveMCPResult:
    """Result of one agent CLI's live MCP status command."""

    agent: str
    command: tuple[str, ...]
    status: str
    returncode: int | None
    output: str = ""


@dataclass(frozen=True)
class MCPToolProbeResult:
    """Read-only result of discovering one Agent's tools for one MCP server."""

    agent: str
    status: str
    auth_method: str
    tool_names: tuple[str, ...] = ()
    error: str = ""


def _state_file_hash(state_path: Path) -> str:
    """Return SHA-256 hex digest of the state file content, or 'absent' if it does not exist."""
    if not state_path.is_file():
        return "absent"
    try:
        return hashlib.sha256(state_path.read_bytes()).hexdigest()
    except OSError:
        return "error"


@dataclass(frozen=True)
class MCPConfigTarget(ConfigTarget):
    """A logical configuration target node representing an MCP server in an agent config."""

    target_name: str = ""


@dataclass(frozen=True)
class MCPObservedEntry:
    """Observed runtime state of an MCP server entry in an agent config file."""

    target: MCPConfigTarget
    exists: bool
    fingerprint: str | None
    managed_fingerprint: str | None
    is_managed: bool

    def __init__(
        self,
        target: MCPConfigTarget,
        exists: bool,
        fingerprint: str | None,
        managed_fingerprint: str | None,
        is_managed: bool,
        raw_entry: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "exists", exists)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "managed_fingerprint", managed_fingerprint)
        object.__setattr__(self, "is_managed", is_managed)
        object.__setattr__(self, "_raw_entry", raw_entry)

    @property
    def entry(self) -> dict[str, Any] | None:
        """Display-safe observed entry with credentials redacted."""
        raw = getattr(self, "_raw_entry", None)
        return redact_mcp_entry(raw) if raw is not None else None

    @property
    def raw_entry(self) -> dict[str, Any] | None:
        """Raw unredacted entry for internal use only."""
        return getattr(self, "_raw_entry", None)

    def __repr__(self) -> str:
        return (
            f"MCPObservedEntry(target={self.target!r}, exists={self.exists!r}, "
            f"fingerprint={self.fingerprint!r}, managed_fingerprint={self.managed_fingerprint!r}, "
            f"is_managed={self.is_managed!r}, entry={self.entry!r})"
        )


@dataclass(frozen=True)
class MCPDesiredEntry:
    """Desired configuration state of an MCP server."""

    target: MCPConfigTarget
    fingerprint: str | None
    contains_secret: bool = False
    missing_credential_env: str = ""
    live_command: tuple[str, ...] = ()
    auth_command: tuple[str, ...] = ()

    def __init__(
        self,
        target: MCPConfigTarget,
        fingerprint: str | None,
        contains_secret: bool = False,
        missing_credential_env: str = "",
        live_command: tuple[str, ...] = (),
        auth_command: tuple[str, ...] = (),
        raw_desired: dict[str, Any] | None = None,
    ) -> None:
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "fingerprint", fingerprint)
        object.__setattr__(self, "contains_secret", contains_secret)
        object.__setattr__(self, "missing_credential_env", missing_credential_env)
        object.__setattr__(self, "live_command", live_command)
        object.__setattr__(self, "auth_command", auth_command)
        object.__setattr__(self, "_raw_desired", raw_desired)

    @property
    def desired(self) -> dict[str, Any] | None:
        """Display-safe desired entry with credentials redacted."""
        raw = getattr(self, "_raw_desired", None)
        return redact_mcp_entry(raw) if raw is not None else None

    @property
    def raw_desired(self) -> dict[str, Any] | None:
        """Raw unredacted desired payload for internal use only."""
        return getattr(self, "_raw_desired", None)

    def __repr__(self) -> str:
        return (
            f"MCPDesiredEntry(target={self.target!r}, fingerprint={self.fingerprint!r}, "
            f"contains_secret={self.contains_secret!r}, "
            f"missing_credential_env={self.missing_credential_env!r}, desired={self.desired!r})"
        )


@dataclass(frozen=True)
class MCPOperation:
    """A planned logical mutation for an MCP server in an agent config."""

    target: MCPConfigTarget
    action: str  # "NOOP", "CREATE", "UPDATE", "REMOVE", "CONFLICT", "SKIP", "ERROR"
    reason: str = ""
    observed: MCPObservedEntry | None = None
    desired: MCPDesiredEntry | None = None
    requires_force: bool = False
    force_identity: str | None = None
    is_authorized: bool = True
    state_transition: tuple[str, str | None] | None = None

    def __init__(
        self,
        target: MCPConfigTarget,
        action: str,
        reason: str = "",
        observed: MCPObservedEntry | None = None,
        desired: MCPDesiredEntry | None = None,
        requires_force: bool = False,
        force_identity: str | None = None,
        is_authorized: bool = True,
        state_transition: tuple[str, str | None] | None = None,
        spec: AgentSpec | None = None,
    ) -> None:
        object.__setattr__(self, "target", target)
        object.__setattr__(self, "action", action)
        object.__setattr__(self, "reason", reason)
        object.__setattr__(self, "observed", observed)
        object.__setattr__(self, "desired", desired)
        object.__setattr__(self, "requires_force", requires_force)
        object.__setattr__(self, "force_identity", force_identity)
        object.__setattr__(self, "is_authorized", is_authorized)
        object.__setattr__(self, "state_transition", state_transition)
        object.__setattr__(self, "_spec", spec)

    @property
    def spec(self) -> AgentSpec | None:
        return getattr(self, "_spec", None)

    @property
    def is_drift(self) -> bool:
        return self.requires_force or self.action == "CONFLICT"

    def __repr__(self) -> str:
        return (
            f"MCPOperation(target={self.target!r}, action={self.action!r}, "
            f"reason={self.reason!r}, requires_force={self.requires_force!r}, "
            f"is_authorized={self.is_authorized!r})"
        )


@dataclass(frozen=True)
class MCPFilePlan:
    """Aggregates all operations targeting a single physical agent configuration file."""

    path: Path
    physical_identity: str
    format: str
    sensitive: bool
    pre_image: FileSnapshot
    operations: tuple[MCPOperation, ...] = ()
    orig_content: str | None = field(default=None, repr=False)
    final_content: str | None = field(default=None, repr=False)

    def __repr__(self) -> str:
        return (
            f"MCPFilePlan(path={self.path!r}, physical_identity={self.physical_identity!r}, "
            f"format={self.format!r}, sensitive={self.sensitive!r}, "
            f"pre_image={self.pre_image!r}, operations={self.operations!r})"
        )

    @property
    def will_mutate(self) -> bool:
        return any(
            op.action in ("CREATE", "UPDATE", "REMOVE") and op.is_authorized
            for op in self.operations
        )

    @property
    def should_backup(self) -> bool:
        return (
            self.will_mutate
            and self.pre_image.exists
            and not self.sensitive
            and self.format not in ("claude_json", "agy_json")
        )

    def validate_precondition(self) -> None:
        valid, msg = self.pre_image.validate_precondition(self.path)
        if not valid:
            raise StaleConfigPlanError(msg)


@dataclass(frozen=True)
class MCPPlan:
    """Immutable, fully-evaluated synchronization plan for MCP servers."""

    operations: tuple[MCPOperation, ...]
    file_plans: tuple[MCPFilePlan, ...]
    state_snapshot_hash: str
    specs: tuple[AgentSpec, ...] = field(default=(), repr=False)

    def __repr__(self) -> str:
        return (
            f"MCPPlan(operations={self.operations!r}, file_plans={self.file_plans!r}, "
            f"state_snapshot_hash={self.state_snapshot_hash!r})"
        )

    @property
    def can_apply(self) -> bool:
        return not any(
            (op.action == "CONFLICT" and not op.is_authorized) or op.action == "ERROR"
            for op in self.operations
        )

    @property
    def changes_count(self) -> int:
        return sum(
            1
            for op in self.operations
            if op.action in ("CREATE", "UPDATE", "REMOVE") and op.is_authorized
        )

    @property
    def conflicts_count(self) -> int:
        return sum(
            1
            for op in self.operations
            if op.action == "CONFLICT" and not op.is_authorized
        )

    @property
    def has_conflicts(self) -> bool:
        return self.conflicts_count > 0

    def validate_preconditions(self, home: Path) -> None:
        state_path = home / STATE_FILE
        curr_hash = _state_file_hash(state_path)
        if curr_hash != self.state_snapshot_hash:
            raise StaleConfigPlanError(
                f"MCP state store '{state_path}' has been modified externally since plan generation"
            )
        for fp in self.file_plans:
            fp.validate_precondition()

    def observe(self) -> PlanObservation:
        """Project plan into a pure PlanObservation."""
        from . import observe_mcp_operation

        views: list[PlanOperationView] = []
        findings: list[Finding] = []
        for op in self.operations:
            view, finding = observe_mcp_operation(op)
            views.append(view)
            if finding is not None:
                findings.append(finding)
        can_apply = self.can_apply and not any(is_error_finding(f) for f in findings)
        return PlanObservation(
            operations=tuple(views),
            findings=tuple(findings),
            can_apply=can_apply,
        )


@dataclass(frozen=True)
class MCPExecutionResult:
    """Structured execution result of applying an MCPPlan."""

    success: bool
    applied_count: int
    noop_count: int
    skipped_count: int
    conflict_count: int
    failed_count: int
    backups_created: tuple[Path, ...] = ()
    failed_files: tuple[Path, ...] = ()
    backup_warnings: tuple[str, ...] = ()
    error_message: str | None = None
    recovery_required: bool = False
    recovery_guidance: str | None = None
