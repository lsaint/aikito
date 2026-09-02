"""Load the extensionless ``bin/aikito`` entry script as the module ``aikito_cli``.

Completions and tests need the entry's ``build_parser`` and command handlers as
importable attributes. This loader performs the importlib machinery once and
registers the module in ``sys.modules`` so ``from aikito_cli import ...`` works.
"""

import importlib.machinery
import importlib.util
import sys
from pathlib import Path
from types import ModuleType


ENTRY_PATH = Path(__file__).resolve().parent / "aikito"


def load_cli() -> ModuleType:
    """Load and return the CLI entry module without import-order side effects."""
    existing = sys.modules.get("aikito_cli")
    if existing is not None:
        return existing
    loader = importlib.machinery.SourceFileLoader("aikito_cli", str(ENTRY_PATH))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["aikito_cli"] = module
    try:
        loader.exec_module(module)
    except Exception:
        sys.modules.pop("aikito_cli", None)
        raise
    return module


def main() -> None:
    """Entry point for installed console script."""
    load_cli().main()
