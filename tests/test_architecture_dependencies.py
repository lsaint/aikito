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
        tree = ast.parse((SRC / "mcp.py").read_text(encoding="utf-8"))
        public_reexports = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module == "agents"
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


if __name__ == "__main__":
    unittest.main()
