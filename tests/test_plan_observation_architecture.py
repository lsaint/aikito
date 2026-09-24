"""Architecture verification tests for PlanObservation subsystem.

Covers INV-APP-10 through INV-APP-13:
- Protocol implementation across all plan classes (§61)
- Purity and No-I/O guarantees (§62)
- Determinism guarantees (§63)
- Sensitive credential redaction (§64)
- Fail-closed behavior for unknown actions (§1.11 / INV-APP-13)
"""

from __future__ import annotations

import builtins
import socket
import subprocess
from pathlib import Path
from typing import Any
import pytest

from aikito.agents import Target
from aikito.config_runtime import ConfigOperation, ConfigTarget
from aikito.global_skills import GlobalSkillBatch, GlobalSkillBatchPlan
from aikito.instructions import InstructionBatch, InstructionPlan
from aikito.link import LinkOperation
from aikito.mcp import (
    MCPConfigTarget,
    MCPOperation,
    MCPPlan,
    AgentSpec,
)
from aikito.memory_runtime import MemoryBatch, MemoryPlan
from aikito.plan_observation import (
    ObservablePlan,
    PlanObservation,
)
from aikito.project_sync import ProjectSyncBatch
from aikito.skill_plan import (
    SkillOperation,
    SkillPlan,
    SkillTarget,
)
from aikito.subagent import SubagentPlan
from aikito.workspace_sync import (
    BundledSkillRefreshPlan,
    GlobalSyncPlan,
    ProjectSyncEntry,
    WorkspaceSyncPlan,
)


def _make_sample_plans(tmp_path: Path) -> dict[str, Any]:
    """Construct realistic, minimal immutable plan fixtures for all 10 plan types."""
    # 1. BundledSkillRefreshPlan
    bundled_plan = BundledSkillRefreshPlan()

    # 2. GlobalSkillBatchPlan
    global_batch = GlobalSkillBatch(
        workspace_root=tmp_path,
        container=Target(
            kind="managed_container", scope="global", path=tmp_path / "skills"
        ),
        selected_entries=(),
        stale_entries=(),
        consumers=(),
    )
    global_skill_plan = GlobalSkillBatchPlan(
        batch=global_batch,
        container_op=LinkOperation(action="NOOP", rule_id="R0", target_path=tmp_path),
        entry_ops=(
            LinkOperation(
                action="CREATE", rule_id="R1", target_path=tmp_path / "skills" / "s1"
            ),
        ),
        consumer_ops=(),
    )

    # 3. InstructionPlan
    inst_plan = InstructionPlan(
        batch=InstructionBatch(
            scope="global",
            canonical_source=tmp_path / "AGENTS.md",
        ),
        operations=(
            LinkOperation(
                action="NOOP", rule_id="R2", target_path=tmp_path / "AGENTS.md"
            ),
        ),
    )

    # 4. MemoryPlan
    mem_plan = MemoryPlan(
        batch=MemoryBatch(
            project_name="p1",
            workspace_root=tmp_path,
            active_checkouts=(tmp_path / "co",),
        ),
        operations=(
            LinkOperation(action="NOOP", rule_id="R3", target_path=tmp_path / "mem.md"),
        ),
    )

    # 5. SkillPlan
    target = SkillTarget(
        workspace_root=tmp_path,
        workspace_id="ws",
        project_name="p1",
        physical_checkout=tmp_path / "co",
        skill_name="sk1",
        target_path=tmp_path / "co" / ".agents" / "skills" / "sk1",
    )
    skill_plan = SkillPlan(
        workspace_root=tmp_path,
        project_name="p1",
        operations=(
            SkillOperation(
                action="NOOP",
                rule_id="INV-TR-01",
                target=target,
                reason="up to date",
            ),
        ),
        findings=(),
        authorizations=(),
        can_apply=True,
    )

    # 6. MCPPlan
    mcp_target = MCPConfigTarget(
        agent="codex",
        path=tmp_path / "mcp.json",
        format="json",
        logical_identity="server1",
    )
    mcp_plan = MCPPlan(
        state_snapshot_hash="dummy_hash",
        operations=(
            MCPOperation(
                target=mcp_target,
                action="NOOP",
                reason="already configured",
            ),
        ),
        file_plans=(),
    )

    # 7. SubagentPlan
    subagent_target = ConfigTarget(
        path=tmp_path / "subagents.toml",
        format="codex_toml",
        agent="codex",
        logical_identity="agent1",
    )
    subagent_plan = SubagentPlan(
        operations=(
            ConfigOperation(
                target=subagent_target,
                action="NOOP",
                reason="identical",
            ),
        ),
        file_plans=(),
    )

    # 8. ProjectSyncBatch
    batch = ProjectSyncBatch(
        workspace_root=tmp_path,
        project_name="p1",
        active_checkouts=(tmp_path / "co",),
        offline_checkouts=(),
        skill_plan=skill_plan,
        preflight_findings=(),
        can_apply=True,
        instruction_plan=inst_plan,
        memory_plan=mem_plan,
    )

    # 9. GlobalSyncPlan
    global_plan = GlobalSyncPlan(
        bundled_refresh_plan=bundled_plan,
        skill_plan=global_skill_plan,
        instruction_plan=inst_plan,
        findings=(),
        can_apply=True,
    )

    # 10. WorkspaceSyncPlan
    entry = ProjectSyncEntry(
        project_name="p1",
        binding_status="active",
        batch=batch,
    )
    workspace_plan = WorkspaceSyncPlan(
        workspace_root=tmp_path,
        home=tmp_path,
        global_plan=global_plan,
        subagent_plan=subagent_plan,
        mcp_plan=mcp_plan,
        project_entries=(entry,),
        findings=(),
        can_apply=True,
    )

    return {
        "BundledSkillRefreshPlan": bundled_plan,
        "GlobalSkillBatchPlan": global_skill_plan,
        "InstructionPlan": inst_plan,
        "MemoryPlan": mem_plan,
        "SkillPlan": skill_plan,
        "MCPPlan": mcp_plan,
        "SubagentPlan": subagent_plan,
        "ProjectSyncBatch": batch,
        "GlobalSyncPlan": global_plan,
        "WorkspaceSyncPlan": workspace_plan,
    }


def test_all_plans_implement_observable_protocol(tmp_path: Path) -> None:
    """§61: All 10 plan types must implement ObservablePlan protocol."""
    plans = _make_sample_plans(tmp_path)
    assert len(plans) == 10

    for name, plan in plans.items():
        assert isinstance(plan, ObservablePlan), (
            f"{name} does not implement ObservablePlan protocol"
        )
        obs = plan.observe()
        assert isinstance(obs, PlanObservation), (
            f"{name}.observe() did not return a PlanObservation"
        )


def test_plan_observation_no_io_guarantee(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§62: Calling plan.observe() must perform zero filesystem, network, or process I/O."""
    plans = _make_sample_plans(tmp_path)

    def _io_forbidden(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "I/O is strictly forbidden during PlanObservation generation"
        )

    monkeypatch.setattr(Path, "read_text", _io_forbidden)
    monkeypatch.setattr(Path, "read_bytes", _io_forbidden)
    monkeypatch.setattr(Path, "write_text", _io_forbidden)
    monkeypatch.setattr(Path, "write_bytes", _io_forbidden)
    monkeypatch.setattr(Path, "mkdir", _io_forbidden)
    monkeypatch.setattr(Path, "unlink", _io_forbidden)
    monkeypatch.setattr(builtins, "open", _io_forbidden)
    monkeypatch.setattr(subprocess, "run", _io_forbidden)
    monkeypatch.setattr(subprocess, "Popen", _io_forbidden)
    monkeypatch.setattr(socket, "socket", _io_forbidden)

    for name, plan in plans.items():
        # Must execute without triggering any I/O exception
        obs = plan.observe()
        assert isinstance(obs, PlanObservation), f"Failed observing {name}"


def test_plan_observation_determinism(tmp_path: Path) -> None:
    """§63: plan.observe() == plan.observe() must hold deterministically."""
    plans = _make_sample_plans(tmp_path)

    for name, plan in plans.items():
        obs1 = plan.observe()
        obs2 = plan.observe()
        assert obs1 == obs2, f"Non-deterministic observation on {name}"
        assert obs1.operations == obs2.operations
        assert obs1.findings == obs2.findings
        assert obs1.summary == obs2.summary


def test_plan_observation_sensitive_redaction(tmp_path: Path) -> None:
    """§64 & INV-APP-08: Sensitive credentials must not leak into observation representations."""
    secret_token = "SUPER_SECRET_BEARER_TOKEN_xyz123"
    secret_key = "MY_PRIVATE_API_KEY_987654"

    server_spec = AgentSpec(
        agent="codex",
        server="secure_service",
        config_path=tmp_path / "mcp.json",
        config_format="json",
        target_name="secure_service",
        desired={"API_KEY": secret_key, "AUTH_HEADER": f"Bearer {secret_token}"},
    )

    target = MCPConfigTarget(
        agent="codex",
        path=tmp_path / "mcp.json",
        format="json",
        logical_identity="secure_service",
    )
    op = MCPOperation(
        target=target,
        action="CREATE",
        reason="Add server securely",
        spec=server_spec,
    )
    plan = MCPPlan(operations=(op,), file_plans=(), state_snapshot_hash="dummy_hash")

    obs = plan.observe()
    obs_repr = repr(obs)

    # Assert secrets do not leak anywhere
    assert secret_token not in obs_repr
    assert secret_key not in obs_repr

    for view in obs.operations:
        assert secret_token not in repr(view)
        assert secret_key not in repr(view)
        assert secret_token not in view.reason
        assert secret_key not in view.reason
        assert secret_token not in view.target
        assert secret_key not in view.target

    for finding in obs.findings:
        assert secret_token not in finding.message
        assert secret_key not in finding.message


def test_workspace_facades_do_not_interpret_domain_actions() -> None:
    """Gate P0-B': workspace coordinators and sync rendering must not read domain `.action`
    or reflectively probe operations outside observe() implementations."""
    import ast

    src_dir = Path(__file__).resolve().parents[1] / "src" / "aikito"
    files = [src_dir / "workspace.py", src_dir / "workspace_sync.py"]
    render_tree = ast.parse((src_dir / "render.py").read_text(encoding="utf-8"))
    render_sync = next(
        (
            node
            for node in ast.walk(render_tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "render_workspace_sync_plan"
        ),
        None,
    )

    violations: list[str] = []

    def is_inside_observe(stack: list[str]) -> bool:
        return any(name == "observe" or name.startswith("observe_") for name in stack)

    def scan(tree: ast.AST, label: str) -> None:
        stack: list[str] = []

        def visit(node: ast.AST) -> None:
            is_func = isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            if is_func:
                stack.append(node.name)
            if not is_inside_observe(stack):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == "action"
                    and isinstance(node.ctx, ast.Load)
                ):
                    violations.append(f"{label}:{node.lineno} reads '.action'")
                if (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Name)
                    and node.func.id in ("getattr", "hasattr")
                ):
                    violations.append(f"{label}:{node.lineno} uses {node.func.id}()")
            for child in ast.iter_child_nodes(node):
                visit(child)
            if is_func:
                stack.pop()

        visit(tree)

    for file_path in files:
        scan(
            ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path)),
            file_path.name,
        )
    if render_sync is not None:
        scan(render_sync, "render.py")

    assert violations == [], "Architecture invariant violated:\n" + "\n".join(
        violations
    )
