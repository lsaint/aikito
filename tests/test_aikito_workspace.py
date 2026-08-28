import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aikito_workspace import (
    persist_workspace,
    resolve_workspace,
    resolve_workspace_with_source,
)

ROOT = Path(__file__).resolve().parents[1]


class AikitoWorkspaceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary_directory.name)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_default_workspace(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(
                resolve_workspace(self.home), (self.home / "aikito").resolve()
            )
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "default")

    def test_persisted_workspace(self) -> None:
        workspace = self.home / "custom-workspace"
        with patch.dict(os.environ, {}, clear=True):
            persist_workspace(workspace, self.home)
            self.assertEqual(resolve_workspace(self.home), workspace.resolve())
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "configured")

    def test_environment_overrides_persisted_workspace(self) -> None:
        persisted = self.home / "persisted"
        environment = self.home / "environment"
        with patch.dict(os.environ, {}, clear=True):
            persist_workspace(persisted, self.home)
        with patch.dict(os.environ, {"AIKITO_DIR": str(environment)}, clear=True):
            self.assertEqual(resolve_workspace(self.home), environment.resolve())
            self.assertEqual(resolve_workspace_with_source(self.home)[1], "AIKITO_DIR")


if __name__ == "__main__":
    unittest.main()
