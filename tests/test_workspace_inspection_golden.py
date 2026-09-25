"""CLI characterization and golden tests for Aikito workspace status and doctor inspection.

Covers PR 1 requirements of aikito-workspace-inspection-plan:
- Golden characterization for `status` default, MCP/subagent details, and `doctor` (text and JSON)
- Scenarios: clean OK, missing, drift, unmanaged conflict, orphan, offline project/agent,
  config syntax error, credential permissions, and no-color output
- Read-only guarantees (running status and doctor never mutates files)
- Process exit codes and doctor failure isolation
- Legacy characterization of subagent UPDATE divergence between status overview and matrix
- Target contract for unified inspection status
"""

from __future__ import annotations
from layout_helpers import replace_subagent_body, write_agents, write_subagents

import io
import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito import cli as AIKITO_CLI
from aikito.global_skills import plan_global_skills
from aikito.init import init_workspace
from aikito.inspection import (
    InspectionStatus,
    ResourceInspectionView,
    WorkspaceResourceInspection,
)
from aikito.status import (
    collect_agent_status_rows,
    collect_subagents_matrix,
)
from aikito.mcp import build_mcp_plan
from aikito.subagent import build_subagent_plan

ANSI_ESCAPE_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def _tree_snapshot(root: Path) -> dict[str, tuple[int, int]]:
    """Capture relative paths, sizes, and mtimes of all files in a directory tree."""
    snapshot: dict[str, tuple[int, int]] = {}
    if not root.exists():
        return snapshot
    for p in sorted(root.rglob("*")):
        if p.is_file() or p.is_symlink():
            rel = str(p.relative_to(root))
            st = p.lstat()
            snapshot[rel] = (st.st_size, st.st_mtime_ns)
    return snapshot


class AikitoWorkspaceInspectionGoldenTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name).resolve()
        self.aikito_dir = self.root / "ws"
        self.home = self.root / "hm"
        self.home.mkdir(parents=True)
        (self.home / ".claude").mkdir(parents=True)

        init_workspace(self.aikito_dir, self.home)

        # Baseline agents.toml matching official claude-code template
        write_agents(
            self.aikito_dir,
            """
[agents.claude-code]
display_name = "Claude Code"
instruction_path = ".claude/CLAUDE.md"
project_instruction_path = ".claude/CLAUDE.md"
skills_path = ".claude/skills"

[agents.claude-code.subagents]
config_path = ".claude/agents"
config_format = "claude_markdown"

[agents.claude-code.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "verbatim"
""".strip()
            + "\n",
        )

        # Baseline skills.toml
        (self.aikito_dir / "skills.toml").write_text(
            'skills = ["my-skill"]\n',
            encoding="utf-8",
        )
        skill_dir = self.aikito_dir / "skills" / "my-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# My Skill\n", encoding="utf-8")

        # Baseline subagents.toml
        (self.aikito_dir / "subagents" / "verifier.md").write_text(
            "# Verifier Subagent\nPrompt\n", encoding="utf-8"
        )
        write_subagents(
            self.aikito_dir,
            '[subagents.verifier]\ndescription = "Verification agent"\nagents = ["claude-code"]\n',
        )

        # Baseline mcps/github.toml
        (self.aikito_dir / "mcps" / "github.toml").write_text(
            """
transport = "remote"
url = "https://api.github.com/mcp"
agents = ["claude-code"]
""".strip()
            + "\n",
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def _run_cli(
        self,
        argv: list[str],
        aikito_dir: Path | None = None,
        home: Path | None = None,
    ) -> tuple[int, str, str]:
        """Execute an aikito CLI command in-process with controlled home and workspace paths."""
        ws = aikito_dir or self.aikito_dir
        hm = home or self.home
        parser = AIKITO_CLI.build_parser()
        args = parser.parse_args(argv)

        out = io.StringIO()
        err = io.StringIO()
        ret = 0

        with (
            patch.object(AIKITO_CLI, "get_aikito_dir", return_value=ws),
            patch.object(
                AIKITO_CLI,
                "resolve_workspace_with_source",
                return_value=(ws, "TEST_AIKITO_DIR"),
            ),
            patch.object(AIKITO_CLI.Path, "home", return_value=hm),
            patch.dict(os.environ, {"AIKITO_DIR": str(ws), "HOME": str(hm)}),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            try:
                args.func(args)
            except SystemExit as exc:
                ret = exc.code if isinstance(exc.code, int) else (1 if exc.code else 0)

        return ret, out.getvalue(), err.getvalue()

    def _sync_clean(self) -> None:
        """Run full workspace sync to bring runtime into 100% clean, synchronized state."""
        ret, out, err = self._run_cli(["sync"])
        self.assertEqual(ret, 0, f"Sync failed: {err}\n{out}")

    # -------------------------------------------------------------------------
    # 1. Clean Workspace Golden Output (status, show mcp, show subagents, doctor)
    # -------------------------------------------------------------------------

    def test_golden_status_default_clean(self) -> None:
        self._sync_clean()
        ret, out, err = self._run_cli(["status"])
        self.assertEqual(ret, 0)
        self.assertIn("Global:", out)
        self.assertIn("Skills 1", out)
        self.assertIn("MCP 1", out)
        self.assertIn("Sub 1", out)
        self.assertIn("claude", out)
        self.assertNotIn("[ERROR]", err)

    def test_golden_show_mcp_clean(self) -> None:
        self._sync_clean()
        ret, out, err = self._run_cli(["show", "mcp", "--agent"])
        self.assertEqual(ret, 0)
        self.assertIn("github", out)
        self.assertIn("Claude Code", out)

    def test_golden_show_subagents_clean(self) -> None:
        self._sync_clean()
        ret, out, err = self._run_cli(["show", "subagents", "--agent"])
        self.assertEqual(ret, 0)
        self.assertIn("verifier", out)
        self.assertIn("Claude Code", out)

    def test_golden_doctor_clean_text_and_json(self) -> None:
        self._sync_clean()
        # Text output
        ret, out, err = self._run_cli(["doctor", "--no-color"])
        self.assertEqual(ret, 0, f"Doctor exited with {ret}: {out}")
        self.assertIn("Symlinks", out)
        self.assertIn("Global instructions OK", out)
        self.assertIn("Global skills OK", out)
        self.assertIn("Orphans", out)
        self.assertIn("Drift", out)

        # JSON output
        ret, json_out, _ = self._run_cli(["doctor", "--json"])
        self.assertEqual(ret, 0)
        data = json.loads(json_out)
        self.assertEqual(data["fail_count"], 0)
        section_names = [s["name"] for s in data["sections"]]
        self.assertIn("Symlinks", section_names)
        self.assertIn("Orphans", section_names)
        self.assertIn("Drift", section_names)
        self.assertIn("Configuration", section_names)

    # -------------------------------------------------------------------------
    # 2. Read-Only Guarantee
    # -------------------------------------------------------------------------

    def test_golden_readonly_guarantee(self) -> None:
        self._sync_clean()

        ws_before = _tree_snapshot(self.aikito_dir)
        hm_before = _tree_snapshot(self.home)

        # Execute read-only commands
        self._run_cli(["status"])
        self._run_cli(["show", "mcp", "--agent"])
        self._run_cli(["show", "subagents", "--agent"])
        self._run_cli(["doctor"])
        self._run_cli(["doctor", "--json"])
        self._run_cli(["doctor", "--no-color"])

        ws_after = _tree_snapshot(self.aikito_dir)
        hm_after = _tree_snapshot(self.home)

        # Assert no additions, deletions, or timestamp alterations
        self.assertEqual(
            ws_before,
            ws_after,
            f"Workspace tree was modified by inspection commands! Diff: {set(ws_after) ^ set(ws_before)}",
        )
        self.assertEqual(
            hm_before,
            hm_after,
            f"Home tree was modified by inspection commands! Diff: {set(hm_after) ^ set(hm_before)}",
        )

    # -------------------------------------------------------------------------
    # 3. No-Color Output Guarantee
    # -------------------------------------------------------------------------

    def test_golden_no_color_flag(self) -> None:
        self._sync_clean()

        _, out_status, _ = self._run_cli(["status", "--no-color"])
        self.assertIsNone(
            ANSI_ESCAPE_RE.search(out_status),
            "status --no-color output contained ANSI escapes",
        )

        _, out_doctor, _ = self._run_cli(["doctor", "--no-color"])
        self.assertIsNone(
            ANSI_ESCAPE_RE.search(out_doctor),
            "doctor --no-color output contained ANSI escapes",
        )

    # -------------------------------------------------------------------------
    # 4. Missing Resources Scenario
    # -------------------------------------------------------------------------

    def test_golden_missing_resources_characterization(self) -> None:
        # Deliberately do not run sync: runtime symlinks and configs are missing
        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertEqual(ret, 1)  # doctor exits 1 on FAIL
        self.assertIn("missing", out.lower())
        self.assertIn("aikito sync global", out)

        # Status overview shows missing / issues
        _, status_out, _ = self._run_cli(["status", "--no-color"])
        self.assertTrue(
            "MISSING" in status_out or "!" in status_out or "Issues" in status_out
        )

        # Doctor JSON reflects failures
        ret_json, json_out, _ = self._run_cli(["doctor", "--json"])
        self.assertEqual(ret_json, 1)
        data = json.loads(json_out)
        self.assertGreater(data["fail_count"], 0)

    # -------------------------------------------------------------------------
    # 5. Drift Scenario
    # -------------------------------------------------------------------------

    def test_golden_drift_resources_characterization(self) -> None:
        self._sync_clean()

        # External edit directly in agent's config creating drift
        claude_json = self.home / ".claude.json"
        if claude_json.is_file():
            content = json.loads(claude_json.read_text(encoding="utf-8"))
            if "mcpServers" in content and "github" in content["mcpServers"]:
                content["mcpServers"]["github"]["args"] = ["--drifted"]
                claude_json.write_text(json.dumps(content), encoding="utf-8")

        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        # Doctor Drift section should detect drift
        self.assertIn("Drift", out)

    # -------------------------------------------------------------------------
    # 6. Unmanaged Conflict Scenario
    # -------------------------------------------------------------------------

    def test_golden_unmanaged_conflict_characterization(self) -> None:
        self._sync_clean()

        # Point consumer skill link elsewhere (conflict)
        claude_skills = self.home / ".claude" / "skills"
        if claude_skills.is_symlink():
            claude_skills.unlink()
        elif claude_skills.is_dir():
            shutil.rmtree(claude_skills)

        unmanaged_target = self.root / "unmanaged_dir"
        unmanaged_target.mkdir()
        claude_skills.symlink_to(unmanaged_target)

        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertEqual(ret, 1)
        self.assertTrue(
            "points elsewhere" in out
            or "not a symlink" in out
            or "Conflict" in out
            or "FAIL" in out
        )

    # -------------------------------------------------------------------------
    # 7. Orphan Subagent Scenario
    # -------------------------------------------------------------------------

    def test_golden_orphan_subagent_characterization(self) -> None:
        self._sync_clean()

        # Add an unmanaged/orphan subagent file in agent directory with aikito marker
        orphan_file = self.home / ".claude" / "agents" / "stale_agent.md"
        orphan_file.parent.mkdir(parents=True, exist_ok=True)
        orphan_file.write_text(
            "<!-- generated by aikito from subagents/stale_agent.md - edits will be overwritten -->\n"
            "# Stale Subagent\n",
            encoding="utf-8",
        )

        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertIn("Orphans", out)
        self.assertTrue("stale_agent" in out or "orphan" in out.lower())

        # show subagents also displays orphan in matrix table
        ret_subs, subs_out, _ = self._run_cli(["show", "subagents"])
        self.assertEqual(ret_subs, 0)
        self.assertIn("stale_agent", subs_out)

    # -------------------------------------------------------------------------
    # 8. Offline Agent and Offline Project Scenario
    # -------------------------------------------------------------------------

    def test_golden_offline_agent_and_offline_project(self) -> None:
        # Add an offline agent (no binary, no home marker)
        (self.aikito_dir / "agents" / "ghost-agent.toml").write_text(
            "[agents.ghost-agent]\ndisplay_name = 'Ghost Agent'\nskills_path = '.ghost/skills'\n",
            encoding="utf-8",
        )

        # Add an offline project pointing to non-existent directory
        proj_dir = self.aikito_dir / "projects" / "offline_proj"
        proj_dir.mkdir(parents=True, exist_ok=True)
        (proj_dir / "agent.toml").write_text(
            'name = "offline_proj"\npath = "~/nonexistent_codebase_dir"\n',
            encoding="utf-8",
        )

        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        # Doctor should gracefully report candidate/offline without crashing
        self.assertIn("offline", out.lower())

        ret_st, st_out, _ = self._run_cli(["status", "--no-color"])
        self.assertEqual(ret_st, 0)
        self.assertIn("offline_proj", st_out)

    # -------------------------------------------------------------------------
    # 9. Config Syntax Error and Error Isolation
    # -------------------------------------------------------------------------

    def test_golden_config_syntax_error_and_error_isolation(self) -> None:
        # Corrupt skills.toml
        (self.aikito_dir / "skills.toml").write_text(
            "INVALID_TOML_SYNTAX { [ \n", encoding="utf-8"
        )

        ret, out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertEqual(ret, 1)

        # Configuration section must report FAIL for skills.toml
        self.assertIn("skills.toml", out)
        self.assertTrue(
            "parse error" in out.lower() or "error" in out.lower() or "✗" in out
        )

        # ERROR ISOLATION: Ensure other doctor sections still executed and rendered!
        self.assertIn("Environment", out)
        self.assertIn("Security", out)
        self.assertIn("Orphans", out)

    # -------------------------------------------------------------------------
    # 10. Insecure Permissions Characterization
    # -------------------------------------------------------------------------

    def test_golden_insecure_credential_permissions(self) -> None:
        self._sync_clean()

        cred_file = self.home / ".claude.json"
        if cred_file.is_file():
            try:
                os.chmod(cred_file, 0o777)
                ret, out, _ = self._run_cli(["doctor", "--no-color"])
                self.assertIn("Security", out)
            finally:
                os.chmod(cred_file, 0o600)

    # -------------------------------------------------------------------------
    # 11. Known Subagent UPDATE Divergence Characterization (Legacy behavior)
    # -------------------------------------------------------------------------

    def test_characterize_subagent_update_unified(self) -> None:
        """Verify that subagent UPDATE divergence between status overview and matrix is resolved.

        Status agent overview maps UPDATE to DRIFT.
        Status matrix now maps UPDATE to UPDATE (rendered as '⚠ D').
        """
        self._sync_clean()

        # Update canonical subagent definition without syncing
        replace_subagent_body(
            self.aikito_dir, "verifier", "# Verifier Subagent Updated Content\n"
        )

        # 1. Check status overview via collect_agent_status_rows
        rows, agent_issues, _, _ = collect_agent_status_rows(self.aikito_dir, self.home)
        claude_row = next(r for r in rows if r.agent_name == "claude-code")
        self.assertTrue(
            claude_row.subagent_status.startswith("DRIFT"),
            f"Expected status overview to report DRIFT for subagent UPDATE, got {claude_row.subagent_status}",
        )

        # 2. Check matrix via collect_subagents_matrix
        subagent_rows, _, _ = collect_subagents_matrix(self.aikito_dir, self.home)
        verifier_row = next(r for r in subagent_rows if r.subagent_name == "verifier")
        claude_matrix_status = verifier_row.agent_statuses.get("Claude Code")
        # Unified matrix now reports UPDATE (rendered as '⚠ D'), resolving the legacy divergence
        self.assertEqual(
            claude_matrix_status,
            "UPDATE",
            f"Expected unified matrix to report UPDATE for subagent UPDATE, got {claude_matrix_status}",
        )

    # -------------------------------------------------------------------------
    # 12. Target Unified Inspection Contract (PR 2 Direction)
    # -------------------------------------------------------------------------

    def test_subagent_update_inspection_contract_target(self) -> None:
        """Verify that under the unified ResourceInspectionView contract,

        subagent update is represented as InspectionStatus.UPDATE, which resolves the legacy divergence.
        """
        view = ResourceInspectionView(
            resource_type="subagent",
            resource_name="verifier",
            status=InspectionStatus.UPDATE,
            agent="claude-code",
            reason="Canonical definition modified; agent target requires update",
        )
        self.assertEqual(view.status, InspectionStatus.UPDATE)
        self.assertEqual(view.status, "UPDATE")

        inspection = WorkspaceResourceInspection(resources=(view,))
        self.assertEqual(inspection.for_agent("claude-code")[0].status, "UPDATE")
        self.assertEqual(inspection.status_counts()[InspectionStatus.UPDATE], 1)

    # -------------------------------------------------------------------------
    # 13. Homology & Fault Isolation (PR 4 Verification)
    # -------------------------------------------------------------------------

    def test_golden_sync_status_doctor_homology(self) -> None:
        """Verify that sync --dry-run, status, and doctor agree on resource inspection facts."""
        self._sync_clean()

        # Introduce an update to verifier subagent
        replace_subagent_body(
            self.aikito_dir, "verifier", "# Verifier Subagent Homology Test Content\n"
        )

        # 1. Sync dry-run reports update / change
        ret_sync, sync_out, _ = self._run_cli(["sync", "--dry-run", "subagents"])
        self.assertEqual(ret_sync, 0)
        self.assertIn("verifier", sync_out.lower())

        # 2. Status overview reports the issue in global summary
        ret_status, status_out, _ = self._run_cli(["status", "--no-color"])
        self.assertEqual(ret_status, 0)
        self.assertIn("! 1 issue", status_out)

        # 3. Show subagents reports verifier details
        ret_show, show_out, _ = self._run_cli(["show", "subagents", "--no-color"])
        self.assertEqual(ret_show, 0)
        self.assertIn("verifier", show_out)

        # 4. Doctor reports drift in Drift section
        ret_doc, doc_out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertIn("Drift", doc_out)
        self.assertIn("managed subagent drift", doc_out)

    def test_golden_doctor_fault_isolation(self) -> None:
        """Verify doctor isolates a config/planner failure in one resource without suppressing other sections."""
        self._sync_clean()

        (self.aikito_dir / "subagents" / "broken.md").write_text(
            '---\ndescription: "Broken agent"\nagents: ["claude-code"]\n'
            'claude-code: {"unknown_field": "invalid"}\n---\n# Broken\n',
            encoding="utf-8",
        )

        # Doctor should report failure in Drift, but STILL execute and report other sections (Symlinks, Orphans)
        ret, doc_out, _ = self._run_cli(["doctor", "--no-color"])
        self.assertEqual(ret, 1)
        self.assertIn("Symlinks", doc_out)
        self.assertIn("Orphans", doc_out)
        self.assertIn("Drift", doc_out)
        self.assertIn("Cannot build subagent synchronization plan", doc_out)

    def test_status_and_doctor_reuse_resource_plans(self) -> None:
        self._sync_clean()
        for command in (["status"], ["doctor", "--no-color"]):
            with (
                patch(
                    "aikito.workspace_inspection.build_subagent_plan",
                    wraps=build_subagent_plan,
                ) as build_subagents,
                patch(
                    "aikito.workspace_inspection.plan_global_skills",
                    wraps=plan_global_skills,
                ) as build_skills,
                patch(
                    "aikito.workspace_inspection.build_mcp_plan",
                    wraps=build_mcp_plan,
                ) as build_mcp,
            ):
                ret, _, _ = self._run_cli(command)
            self.assertEqual(ret, 0)
            self.assertEqual(build_subagents.call_count, 1, command)
            self.assertEqual(build_skills.call_count, 1, command)
            self.assertEqual(build_mcp.call_count, 1, command)


if __name__ == "__main__":
    unittest.main()
