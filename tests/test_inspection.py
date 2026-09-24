"""Unit tests for ResourceInspectionView and inspection contract."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.diagnostics import Finding
from aikito.inspection import (
    InspectionStatus,
    ResourceInspectionView,
    WorkspaceResourceInspection,
)
from aikito.mcp import MCPConfigTarget, MCPOperation, MCPPlan
from aikito.workspace_inspection import inspect_workspace


class InspectionContractTests(unittest.TestCase):
    def test_inspection_status_enum_values_and_str_compatibility(self) -> None:
        self.assertEqual(InspectionStatus.OK, "OK")
        self.assertEqual(InspectionStatus.MISSING, "MISSING")
        self.assertEqual(InspectionStatus.UPDATE, "UPDATE")
        self.assertEqual(InspectionStatus.REMOVE, "REMOVE")
        self.assertEqual(InspectionStatus.DRIFT, "DRIFT")
        self.assertEqual(InspectionStatus.CONFLICT, "CONFLICT")
        self.assertEqual(InspectionStatus.SKIP, "SKIP")
        self.assertEqual(InspectionStatus.ERROR, "ERROR")
        self.assertEqual(InspectionStatus.ORPHAN, "ORPHAN")

        # Test string comparison
        status: InspectionStatus = InspectionStatus.OK
        self.assertEqual(status, "OK")
        self.assertIn(status, {"OK", "NOOP"})

    def test_resource_inspection_view_immutability(self) -> None:
        view = ResourceInspectionView(
            resource_type="instruction",
            resource_name="AGENTS.md",
            status=InspectionStatus.OK,
            agent="claude-code",
            target_path=Path("/tmp/AGENTS.md"),
            reason="Linked correctly",
        )
        self.assertEqual(view.resource_type, "instruction")
        self.assertEqual(view.resource_name, "AGENTS.md")
        self.assertEqual(view.status, InspectionStatus.OK)
        self.assertEqual(view.agent, "claude-code")

        with self.assertRaises(Exception):
            # Frozen dataclass should reject mutations
            view.status = InspectionStatus.DRIFT  # type: ignore[misc]

    def test_workspace_resource_inspection_queries_and_counts(self) -> None:
        r1 = ResourceInspectionView(
            resource_type="instruction",
            resource_name="AGENTS.md",
            status=InspectionStatus.OK,
            agent="claude-code",
        )
        r2 = ResourceInspectionView(
            resource_type="mcp",
            resource_name="github",
            status=InspectionStatus.DRIFT,
            agent="claude-code",
            reason="External config modification detected",
        )
        r3 = ResourceInspectionView(
            resource_type="subagent",
            resource_name="verifier",
            status=InspectionStatus.UPDATE,
            agent="codex",
            reason="Canonical definition changed",
        )
        r4 = ResourceInspectionView(
            resource_type="project_skill",
            resource_name="my-skill",
            status=InspectionStatus.CONFLICT,
            project="proj-alpha",
            finding=Finding(status="FAIL", message="Symlink points elsewhere"),
        )

        inspection = WorkspaceResourceInspection(
            resources=(r1, r2, r3, r4),
            findings=(Finding(status="FAIL", message="Workspace conflict detected"),),
        )

        # for_agent
        claude_resources = inspection.for_agent("claude-code")
        self.assertEqual(len(claude_resources), 2)
        self.assertEqual(
            {r.resource_name for r in claude_resources}, {"AGENTS.md", "github"}
        )

        # for_resource_type
        mcp_resources = inspection.for_resource_type("mcp")
        self.assertEqual(len(mcp_resources), 1)
        self.assertEqual(mcp_resources[0].resource_name, "github")

        # for_project
        proj_resources = inspection.for_project("proj-alpha")
        self.assertEqual(len(proj_resources), 1)
        self.assertEqual(proj_resources[0].resource_name, "my-skill")

        # status_counts
        counts = inspection.status_counts()
        self.assertEqual(counts[InspectionStatus.OK], 1)
        self.assertEqual(counts[InspectionStatus.DRIFT], 1)
        self.assertEqual(counts[InspectionStatus.UPDATE], 1)
        self.assertEqual(counts[InspectionStatus.CONFLICT], 1)
        self.assertEqual(counts[InspectionStatus.MISSING], 0)

        # has_failures
        self.assertTrue(inspection.has_failures)

    def test_workspace_resource_inspection_clean_passes_has_failures(self) -> None:
        r1 = ResourceInspectionView(
            resource_type="instruction",
            resource_name="AGENTS.md",
            status=InspectionStatus.OK,
        )
        inspection = WorkspaceResourceInspection(resources=(r1,))
        self.assertFalse(inspection.has_failures)

    def test_mcp_remove_has_inspection_status(self) -> None:
        target = MCPConfigTarget(
            path=Path("/tmp/agent.json"), logical_identity="old-server", agent="test"
        )
        plan = MCPPlan(
            operations=(MCPOperation(target=target, action="REMOVE"),),
            file_plans=(),
            state_snapshot_hash="",
        )

        (view,) = plan.inspect()
        self.assertEqual(view.status, InspectionStatus.REMOVE)

    def test_workspace_inspection_reuses_subagent_plan(self) -> None:
        class Plan:
            def inspect(self) -> tuple[ResourceInspectionView, ...]:
                return (
                    ResourceInspectionView(
                        resource_type="subagent",
                        resource_name="test",
                        status=InspectionStatus.OK,
                    ),
                )

        inspection = inspect_workspace(Path("/tmp/ws"), Path("/tmp/home"))
        with patch(
            "aikito.workspace_inspection.build_subagent_plan", return_value=Plan()
        ) as build:
            first = inspection.subagent_views
            second = inspection.subagent_views

        self.assertIs(first, second)
        build.assert_called_once()


if __name__ == "__main__":
    unittest.main()
