"""AST guards for INV-AGENT-01: MCP must not act as an Agent-domain facade."""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "aikito"

AGENT_DOMAIN_SYMBOLS = {
    "AGENT_INSTALL_MARKERS",
    "Agent",
    "AgentDefinition",
    "AgentRegistry",
    "AgentRegistryError",
    "check_agent_availability",
    "is_agent_installed",
    "load_agent_definitions",
    "load_agents",
}


def _mcp_imports(path: Path) -> list[tuple[int, str]]:
    """Return (lineno, name) for every symbol imported from aikito.mcp."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        is_mcp = (node.level == 1 and node.module == "mcp") or (
            node.level == 0 and node.module == "aikito.mcp"
        )
        if is_mcp:
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


def _imports_mcp_module(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level == 1 and node.module == "mcp":
                return True
            if node.level == 1 and node.module is None:
                if any(alias.name == "mcp" for alias in node.names):
                    return True
            if node.level == 0 and node.module in ("aikito.mcp", "aikito"):
                if node.module == "aikito.mcp" or any(
                    alias.name == "mcp" for alias in node.names
                ):
                    return True
        elif isinstance(node, ast.Import):
            if any(alias.name == "aikito.mcp" for alias in node.names):
                return True
    return False


class ArchitectureDependencyTests(unittest.TestCase):
    def test_agents_module_does_not_import_mcp(self) -> None:
        self.assertFalse(_imports_mcp_module(SRC / "agents.py"))

    def test_no_agent_symbols_imported_from_mcp(self) -> None:
        violations = [
            f"{path.relative_to(SRC)}:{lineno} imports {name} from mcp"
            for path in sorted(SRC.rglob("*.py"))
            for lineno, name in _mcp_imports(path)
            if name in AGENT_DOMAIN_SYMBOLS
        ]
        self.assertEqual(violations, [])

    def test_mcp_does_not_reexport_agent_symbols(self) -> None:
        mcp_dir = SRC / "mcp"
        mcp_files = (
            list(mcp_dir.rglob("*.py")) if mcp_dir.is_dir() else [SRC / "mcp.py"]
        )
        for mcp_file in mcp_files:
            tree = ast.parse(mcp_file.read_text(encoding="utf-8"))
            public_reexports = [
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)
                and (node.module or "").endswith("agents")
                for alias in node.names
                if alias.asname is not None and alias.asname == alias.name
            ]
            self.assertEqual(public_reexports, [])
            defined = {
                node.name
                for node in tree.body
                if isinstance(node, (ast.ClassDef, ast.FunctionDef))
            }
            self.assertEqual(defined & AGENT_DOMAIN_SYMBOLS, set())

    def test_mcp_reexport_surface_locked(self) -> None:
        """Gate P1-A / P2-D: Lock public re-export surface of aikito.mcp."""
        import aikito.mcp

        expected_exports = {
            "AgentSpec",
            "BasicTokenAuth",
            "LiveMCPResult",
            "MCPConfigError",
            "MCPConfigTarget",
            "MCPDesiredEntry",
            "MCPExecutionResult",
            "MCPFilePlan",
            "MCPObservedEntry",
            "MCPOperation",
            "MCPPlan",
            "MCPToolProbeResult",
            "Token",
            "BACKUP_DIR",
            "BROWSER_HELPER",
            "DEFAULT_AGENTS_CONFIG",
            "DEFAULT_MCPS_DIR",
            "LEGACY_PLACEHOLDER_TOKEN",
            "SENSITIVE_URL_PARAMETERS",
            "STATE_FILE",
            "STATE_VERSION",
            "URL_PATTERN",
            "load_agent_specs",
            "get_agy_json_server",
            "get_claude_json_server",
            "get_copilot_json_server",
            "get_dsh_cordis_server",
            "get_jsonc_server",
            "get_toml_server",
            "parse_jsonc",
            "read_all_entries",
            "read_entry",
            "remove_claude_json_server",
            "remove_dsh_cordis_server",
            "remove_jsonc_server",
            "remove_toml_server",
            "update_agy_json_server",
            "update_claude_json_server",
            "update_copilot_json_server",
            "update_dsh_cordis_server",
            "update_jsonc_server",
            "update_toml_server",
            "environment_reference",
            "is_credential_header",
            "is_sensitive_url_parameter",
            "redact_mcp_entry",
            "build_mcp_plan",
            "evaluate_spec_status",
            "mcp_operation_effect",
            "mcp_operation_finding",
            "observe_mcp_operation",
            "execute_mcp_plan",
            "sync_mcp_configs",
            "sync_remove_mcp_from_agents",
            "authenticate_mcp",
            "describe_mcp_auth",
            "probe_mcp_tools",
            "probe_mcp_tools_for_specs",
            "run_live_mcp_commands",
            "_LiveLoadingIndicator",
            "_MCPProbeError",
            "_RejectRedirects",
            "_agent_detected",
            "_atomic_write",
            "_list_remote_mcp_tools",
            "_load_basic_token_auth",
            "_load_document",
            "_load_state",
            "_parse_jsonc",
            "_post_mcp_message",
            "_read_entry",
            "_redact_probe_error",
            "_response_message",
        }
        self.assertTrue(hasattr(aikito.mcp, "__all__"))
        self.assertEqual(set(aikito.mcp.__all__), expected_exports)

    def test_no_cross_module_private_project_imports(self) -> None:
        """Gate P1-B: No cross-module imports of private symbols from project modules."""
        project_modules = {
            "project",
            "project_config",
            "project_runtime",
            "project_sync",
        }
        violations: list[str] = []
        for path in sorted(SRC.rglob("*.py")):
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not isinstance(node, ast.ImportFrom):
                    continue
                mod = (node.module or "").removeprefix("aikito.")
                if mod in project_modules:
                    for alias in node.names:
                        if alias.name.startswith("_"):
                            violations.append(
                                f"{path.relative_to(SRC)}:{node.lineno} imports private {alias.name} from {node.module}"
                            )
        self.assertEqual(violations, [])


if __name__ == "__main__":
    unittest.main()
