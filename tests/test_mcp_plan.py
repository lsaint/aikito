"""Unit tests for structured MCP planning, same-file aggregation, collisions, and display redaction.

Validates Step 7.6 implementation of:
- INV-MCP-01: Managed Fingerprint as Server Node Ownership and Drift Evidence
- INV-MCP-02: Same-File Multi-Server Chained Aggregation Without Overwrite
- INV-MCP-04: Stale Plan Invalidation on Runtime File or State Store Pre-Image Mutation
- INV-MCP-05: Structured and Plaintext Display Redaction of Sensitive Environment Secrets
"""

import dataclasses
import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito.config_runtime import ConfigCollisionError, StaleConfigPlanError
from aikito.mcp import (
    BACKUP_DIR,
    STATE_FILE,
    AgentSpec,
    MCPExecutionResult,
    MCPPlan,
    build_mcp_plan,
    execute_mcp_plan,
    sync_mcp_configs,
)


class TestMCPPlan(unittest.TestCase):
    def setUp(self) -> None:
        self.test_dir = Path(tempfile.mkdtemp(prefix="aikito_mcp_plan_test_"))
        self.ws = self.test_dir / "workspace"
        self.ws.mkdir()
        self.home = self.test_dir / "home"
        self.home.mkdir()

        # Workspace structure
        self.mcps_dir = self.ws / "mcps"
        self.mcps_dir.mkdir()

        # Setup agents.toml
        agents_toml = """[agents.claude]
display_name = "Claude"
instruction_path = "CLAUDE.md"

[agents.claude.mcp]
config_path = ".claude.json"
config_format = "claude_json"
name_style = "underscore"

[agents.agy]
display_name = "Antigravity"
instruction_path = "AGENTS.md"

[agents.agy.mcp]
config_path = ".gemini/config/mcp_config.json"
config_format = "agy_json"
name_style = "verbatim"
"""
        (self.ws / "agents.toml").write_text(agents_toml, encoding="utf-8")

        # Fake agent presence in home
        (self.home / ".claude.json").parent.mkdir(parents=True, exist_ok=True)
        (self.home / ".gemini/config").mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_pure_planning_does_not_mutate_disk_or_state(self) -> None:
        """build_mcp_plan must be completely side-effect free (INV-MCP-01, INV-MCP-02)."""
        (self.mcps_dir / "server-a.toml").write_text(
            """transport = "remote"
url = "https://example.com/sse"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        plan = build_mcp_plan(self.ws, self.home)
        self.assertIsInstance(plan, MCPPlan)
        self.assertEqual(len(plan.operations), 1)
        self.assertEqual(plan.operations[0].action, "CREATE")

        # Target file must not exist yet
        self.assertFalse((self.home / ".claude.json").exists())
        # State store must not exist yet
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_same_file_aggregation_and_chained_merge(self) -> None:
        """Multiple MCP servers targeting the same agent config must be aggregated into one file plan (INV-MCP-02)."""
        (self.mcps_dir / "server-a.toml").write_text(
            """transport = "remote"
url = "https://example.com/a"
agents = ["claude"]
""",
            encoding="utf-8",
        )
        (self.mcps_dir / "server-b.toml").write_text(
            """transport = "remote"
url = "https://example.com/b"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        plan = build_mcp_plan(self.ws, self.home)
        self.assertEqual(len(plan.operations), 2)
        # Both servers target .claude.json, so there must be exactly 1 file plan
        self.assertEqual(len(plan.file_plans), 1)

        fp = plan.file_plans[0]
        self.assertEqual(fp.path, self.home / ".claude.json")
        self.assertEqual(len(fp.operations), 2)
        self.assertTrue(fp.will_mutate)

        # Chained merge content must contain both servers
        data = json.loads(fp.final_content)
        self.assertIn("mcpServers", data)
        self.assertIn("server_a", data["mcpServers"])
        self.assertIn("server_b", data["mcpServers"])

    def test_managed_fingerprint_and_drift_conflict(self) -> None:
        """Drift against managed fingerprint produces CONFLICT; authorized with --force (INV-MCP-01)."""
        mcp_file = self.mcps_dir / "my-server.toml"
        mcp_file.write_text(
            """transport = "remote"
url = "https://example.com/mcp"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        # Sync once to establish state
        sync_ok = sync_mcp_configs(aikito_dir=self.ws, home=self.home)
        self.assertTrue(sync_ok)

        # External drift: user modifies target file externally
        claude_json = self.home / ".claude.json"
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        data["mcpServers"]["my_server"]["url"] = "https://hacked.com"
        claude_json.write_text(json.dumps(data), encoding="utf-8")

        # Planning without force should detect drift conflict
        plan_drift = build_mcp_plan(self.ws, self.home, force=False)
        self.assertEqual(len(plan_drift.operations), 1)
        op = plan_drift.operations[0]
        self.assertEqual(op.action, "CONFLICT")
        self.assertTrue(op.requires_force)
        self.assertFalse(op.is_authorized)
        self.assertFalse(plan_drift.can_apply)
        self.assertTrue(plan_drift.has_conflicts)

        # Planning with force authorizes overwrite
        plan_forced = build_mcp_plan(self.ws, self.home, force=True)
        self.assertEqual(len(plan_forced.operations), 1)
        forced_op = plan_forced.operations[0]
        self.assertEqual(forced_op.action, "UPDATE")
        self.assertTrue(forced_op.requires_force)
        self.assertTrue(forced_op.is_authorized)
        self.assertTrue(plan_forced.can_apply)

        # Targeted force by name
        plan_targeted = build_mcp_plan(
            self.ws, self.home, force_targets={"claude/my-server"}
        )
        self.assertEqual(plan_targeted.operations[0].action, "UPDATE")
        self.assertTrue(plan_targeted.operations[0].is_authorized)

    def test_normalized_target_name_collision(self) -> None:
        """Two servers normalizing to the same target name in the same file must raise ConfigCollisionError (INV-CFG-03)."""
        # claude agent uses name_style = "underscore"
        # 'foo-bar' and 'foo_bar' both normalize to 'foo_bar'
        (self.mcps_dir / "foo-bar.toml").write_text(
            """transport = "remote"
url = "https://example.com/1"
agents = ["claude"]
""",
            encoding="utf-8",
        )
        (self.mcps_dir / "foo_bar.toml").write_text(
            """transport = "remote"
url = "https://example.com/2"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        with self.assertRaises(ConfigCollisionError) as ctx:
            build_mcp_plan(self.ws, self.home)
        self.assertIn("Colliding MCP server names", str(ctx.exception))
        self.assertIn("foo_bar", str(ctx.exception))

    def test_incompatible_format_collision(self) -> None:
        """Two specs targeting the same physical file with conflicting formats must raise ConfigCollisionError (INV-CFG-03)."""
        spec1 = AgentSpec(
            agent="agent1",
            server="srv1",
            config_path=self.home / "shared.json",
            config_format="claude_json",
            target_name="srv1",
            desired={"url": "https://example.com/1"},
        )
        spec2 = AgentSpec(
            agent="agent2",
            server="srv2",
            config_path=self.home / "shared.json",
            config_format="toml",
            target_name="srv2",
            desired={"url": "https://example.com/2"},
        )

        with self.assertRaises(ConfigCollisionError) as ctx:
            build_mcp_plan(self.ws, self.home, specs=[spec1, spec2])
        self.assertIn("Conflicting formats declared", str(ctx.exception))

    def test_stale_precondition_runtime_file_mutation(self) -> None:
        """Mutating the runtime config after planning raises StaleConfigPlanError (INV-MCP-04)."""
        (self.mcps_dir / "server-a.toml").write_text(
            """transport = "remote"
url = "https://example.com/a"
agents = ["claude"]
""",
            encoding="utf-8",
        )
        # Pre-create .claude.json
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {}}), encoding="utf-8"
        )

        plan = build_mcp_plan(self.ws, self.home)
        # Before modification, preconditions are valid
        plan.validate_preconditions(self.home)

        # Mutate .claude.json externally
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {}, "other": 123}), encoding="utf-8"
        )

        with self.assertRaises(StaleConfigPlanError):
            plan.validate_preconditions(self.home)

    def test_stale_precondition_state_store_mutation(self) -> None:
        """Mutating the state store after planning raises StaleConfigPlanError (INV-MCP-04)."""
        (self.mcps_dir / "server-a.toml").write_text(
            """transport = "remote"
url = "https://example.com/a"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        # Sync once to create state file
        sync_mcp_configs(aikito_dir=self.ws, home=self.home)

        plan = build_mcp_plan(self.ws, self.home)
        plan.validate_preconditions(self.home)

        # External state modification
        state_path = self.home / STATE_FILE
        state_data = json.loads(state_path.read_text(encoding="utf-8"))
        state_data["external_field"] = "tampered"
        state_path.write_text(json.dumps(state_data), encoding="utf-8")

        with self.assertRaises(StaleConfigPlanError):
            plan.validate_preconditions(self.home)

    def test_display_redaction_secrets(self) -> None:
        """Secret headers and tokens are redacted in repr, desired view, and asdict (INV-MCP-05)."""
        spec = AgentSpec(
            agent="claude",
            server="secret-srv",
            config_path=self.home / ".claude.json",
            config_format="claude_json",
            target_name="secret_srv",
            desired={
                "url": "https://api.example.com?api_key=SUPER_SECRET_TOKEN",
                "headers": {"Authorization": "Basic c2VjcmV0LXBhc3N3b3Jk"},
            },
            contains_secret=True,
        )

        plan = build_mcp_plan(self.ws, self.home, specs=[spec])
        op = plan.operations[0]

        # 1. repr(op) must not contain the raw secret
        op_repr = repr(op)
        self.assertNotIn("SUPER_SECRET_TOKEN", op_repr)
        self.assertNotIn("c2VjcmV0LXBhc3N3b3Jk", op_repr)

        # 2. op.desired.desired view must have redacted tokens
        self.assertIsNotNone(op.desired)
        desired_view = op.desired.desired
        self.assertEqual(desired_view["headers"]["Authorization"], "<redacted>")
        self.assertIn("<redacted>", desired_view["url"])
        self.assertNotIn("SUPER_SECRET_TOKEN", desired_view["url"])

        # 3. dataclasses.asdict(op) must not expose raw secrets
        op_dict = dataclasses.asdict(op)
        dict_str = json.dumps(str(op_dict))
        self.assertNotIn("SUPER_SECRET_TOKEN", dict_str)
        self.assertNotIn("c2VjcmV0LXBhc3N3b3Jk", dict_str)

    def test_desired_absent_planning(self) -> None:
        """Desired absent server plans REMOVE operation (INV-MCP-07 preview)."""
        # Create server in claude.json
        (self.home / ".claude.json").write_text(
            json.dumps({"mcpServers": {"old_server": {"url": "https://old.com"}}}),
            encoding="utf-8",
        )
        spec = AgentSpec(
            agent="claude",
            server="old-server",
            config_path=self.home / ".claude.json",
            config_format="claude_json",
            target_name="old_server",
            desired={},
        )

        # Plan as desired absent
        plan = build_mcp_plan(
            self.ws,
            self.home,
            specs=[spec],
            desired_absent_servers={"old-server"},
            force=True,
        )
        self.assertEqual(len(plan.operations), 1)
        op = plan.operations[0]
        self.assertEqual(op.action, "REMOVE")
        self.assertTrue(op.is_authorized)

        # In-memory chained content should have old_server removed
        fp = plan.file_plans[0]
        self.assertTrue(fp.will_mutate)
        data = json.loads(fp.final_content)
        self.assertNotIn("old_server", data.get("mcpServers", {}))

    def test_execute_mcp_plan_success_single_file_write_and_state_commit(self) -> None:
        """Applying plan writes file once and atomically records state (INV-MCP-02, INV-MCP-03)."""
        (self.mcps_dir / "srv-1.toml").write_text(
            """transport = "remote"
url = "https://example.com/1"
agents = ["claude"]
""",
            encoding="utf-8",
        )
        (self.mcps_dir / "srv-2.toml").write_text(
            """transport = "remote"
url = "https://example.com/2"
agents = ["claude"]
""",
            encoding="utf-8",
        )

        plan = build_mcp_plan(self.ws, self.home)
        self.assertEqual(len(plan.operations), 2)
        self.assertEqual(len(plan.file_plans), 1)

        result = execute_mcp_plan(plan, self.home)
        self.assertTrue(result.success)
        self.assertEqual(result.applied_count, 2)
        self.assertEqual(result.failed_count, 0)

        # File written once with both servers
        claude_json = self.home / ".claude.json"
        self.assertTrue(claude_json.exists())
        data = json.loads(claude_json.read_text(encoding="utf-8"))
        self.assertIn("srv_1", data["mcpServers"])
        self.assertIn("srv_2", data["mcpServers"])

        # State file committed
        state_file = self.home / STATE_FILE
        self.assertTrue(state_file.exists())
        state = json.loads(state_file.read_text(encoding="utf-8"))
        self.assertIn("claude:srv-1", state["entries"])
        self.assertIn("claude:srv-2", state["entries"])

        # Second plan execution is complete NOOP
        second_plan = build_mcp_plan(self.ws, self.home)
        self.assertTrue(all(op.action == "NOOP" for op in second_plan.operations))

    def test_execute_backup_suppression_and_secure_permissions_for_sensitive(self) -> None:
        """Sensitive configs suppress backups and enforce 0o600 permissions (INV-MCP-06)."""
        (self.mcps_dir / "secret-srv.toml").write_text(
            """transport = "remote"
url = "https://example.com/sec"
agents = ["claude"]
""",
            encoding="utf-8",
        )
        # Pre-create claude.json
        claude_json = self.home / ".claude.json"
        claude_json.write_text(json.dumps({"mcpServers": {}}), encoding="utf-8")

        plan = build_mcp_plan(self.ws, self.home)
        self.assertFalse(plan.file_plans[0].should_backup)

        result = execute_mcp_plan(plan, self.home)
        self.assertTrue(result.success)
        # No backups should be created
        self.assertEqual(len(result.backups_created), 0)

        # On POSIX, file permissions must be 0o600
        if os.name == "posix":
            file_mode = stat.S_IMODE(claude_json.stat().st_mode)
            self.assertEqual(file_mode, 0o600)

    def test_execute_backup_created_for_eligible_targets(self) -> None:
        """Eligible non-sensitive existing configs are backed up before write (INV-MCP-06)."""
        # Create a spec for an agent with standard toml config format
        toml_path = self.home / "agent_config.toml"
        toml_path.write_text("[mcpServers]\n", encoding="utf-8")

        spec = AgentSpec(
            agent="standard-agent",
            server="my-server",
            config_path=toml_path,
            config_format="toml",
            target_name="my_server",
            desired={"url": "https://example.com"},
            contains_secret=False,
        )

        plan = build_mcp_plan(self.ws, self.home, specs=[spec])
        self.assertTrue(plan.file_plans[0].should_backup)

        result = execute_mcp_plan(plan, self.home)
        self.assertTrue(result.success)
        self.assertEqual(len(result.backups_created), 1)
        backup_path = result.backups_created[0]
        self.assertTrue(backup_path.exists())
        self.assertIn("agent_config.toml", backup_path.name)

    def test_execute_abort_on_backup_failure(self) -> None:
        """If backup fails, abort immediately before modifying any runtime file (INV-MCP-03)."""
        toml_path = self.home / "agent_config.toml"
        toml_path.write_text("[mcpServers]\n", encoding="utf-8")

        spec = AgentSpec(
            agent="test-agent",
            server="srv",
            config_path=toml_path,
            config_format="toml",
            target_name="srv",
            desired={"url": "https://example.com"},
            contains_secret=False,
        )
        plan = build_mcp_plan(self.ws, self.home, specs=[spec])

        with patch("aikito.mcp._backup_config", side_effect=OSError("Disk full")):
            result = execute_mcp_plan(plan, self.home)

        self.assertFalse(result.success)
        self.assertIn("Backup failed", result.error_message or "")
        # Original runtime file MUST NOT be touched
        self.assertEqual(toml_path.read_text(encoding="utf-8"), "[mcpServers]\n")
        # State file MUST NOT be created
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_execute_rollback_on_write_failure(self) -> None:
        """If write fails, committed files are rolled back to pre-mutation content (INV-MCP-03)."""
        file1 = self.home / "file1.json"
        file2 = self.home / "file2.json"
        file1.write_text("orig 1", encoding="utf-8")
        file2.write_text("orig 2", encoding="utf-8")

        spec1 = AgentSpec(
            agent="ag1",
            server="s1",
            config_path=file1,
            config_format="claude_json",
            target_name="s1",
            desired={"url": "https://1"},
        )
        spec2 = AgentSpec(
            agent="ag2",
            server="s2",
            config_path=file2,
            config_format="claude_json",
            target_name="s2",
            desired={"url": "https://2"},
        )

        plan = build_mcp_plan(self.ws, self.home, specs=[spec1, spec2])
        self.assertEqual(len(plan.file_plans), 2)

        original_atomic_write = __import__("aikito.mcp", fromlist=["_atomic_write"])._atomic_write

        call_count = [0]
        def failing_write(path: Path, content: str, secure_permissions: bool = False) -> None:
            call_count[0] += 1
            if call_count[0] == 2:
                raise OSError("Write error on file 2")
            original_atomic_write(path, content, secure_permissions=secure_permissions)

        with patch("aikito.mcp._atomic_write", side_effect=failing_write):
            result = execute_mcp_plan(plan, self.home)

        self.assertFalse(result.success)
        # file1 was written on call 1, then rolled back when call 2 failed
        self.assertEqual(file1.read_text(encoding="utf-8"), "orig 1")
        self.assertEqual(file2.read_text(encoding="utf-8"), "orig 2")
        # State file must not exist
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_execute_rollback_on_state_promotion_failure(self) -> None:
        """If state promotion fails, runtime files are rolled back (INV-MCP-03)."""
        file1 = self.home / "file1.json"
        file1.write_text("orig 1", encoding="utf-8")

        spec1 = AgentSpec(
            agent="ag1",
            server="s1",
            config_path=file1,
            config_format="claude_json",
            target_name="s1",
            desired={"url": "https://1"},
        )

        plan = build_mcp_plan(self.ws, self.home, specs=[spec1])

        with patch("os.replace", side_effect=OSError("State replace failed")):
            result = execute_mcp_plan(plan, self.home)

        self.assertFalse(result.success)
        # file1 must be restored to original
        self.assertEqual(file1.read_text(encoding="utf-8"), "orig 1")
        # State file must not exist
        self.assertFalse((self.home / STATE_FILE).exists())

    def test_execute_rollback_failure_retains_backup_and_sets_recovery_required(self) -> None:
        """If rollback fails, backups are strictly retained and recovery_required=True (INV-MCP-08)."""
        file1 = self.home / "agent_config.toml"
        file2 = self.home / "fail_write.toml"
        file1.write_text("[mcpServers]\n", encoding="utf-8")
        file2.write_text("[mcpServers]\n", encoding="utf-8")

        spec1 = AgentSpec(
            agent="ag1",
            server="s1",
            config_path=file1,
            config_format="toml",
            target_name="s1",
            desired={"url": "https://1"},
        )
        spec2 = AgentSpec(
            agent="ag2",
            server="s2",
            config_path=file2,
            config_format="toml",
            target_name="s2",
            desired={"url": "https://2"},
        )

        plan = build_mcp_plan(self.ws, self.home, specs=[spec1, spec2])

        original_atomic_write = __import__("aikito.mcp", fromlist=["_atomic_write"])._atomic_write

        write_calls = [0]
        def simulate_write_and_rollback_failure(path: Path, content: str, secure_permissions: bool = False) -> None:
            write_calls[0] += 1
            if write_calls[0] == 2:
                # file2 write fails
                raise OSError("Simulated disk error on file2")
            if write_calls[0] == 3:
                # rollback of file1 fails!
                raise OSError("Simulated disk error during rollback")
            original_atomic_write(path, content, secure_permissions=secure_permissions)

        with patch("aikito.mcp._atomic_write", side_effect=simulate_write_and_rollback_failure):
            result = execute_mcp_plan(plan, self.home)

        self.assertFalse(result.success)
        self.assertTrue(result.recovery_required)
        self.assertIsNotNone(result.recovery_guidance)
        self.assertIn("rollback failed", result.recovery_guidance or "")
        # The backup for file1 MUST be retained on disk
        self.assertEqual(len(result.backups_created), 1)
        backup_path = result.backups_created[0]
        self.assertTrue(backup_path.exists())


if __name__ == "__main__":
    unittest.main()
