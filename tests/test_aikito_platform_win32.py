import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aikito_platform import (
    check_credential_permissions,
    get_default_editor,
    get_permission_fix_cmd,
    get_workspace_config_dir,
    is_windows,
    resolve_executable,
    safe_relative_path,
    safe_symlink,
)



class AikitoPlatformWin32Test(unittest.TestCase):
    def test_is_windows(self) -> None:
        expected = sys.platform == "win32"
        self.assertEqual(is_windows(), expected)

    def test_safe_symlink_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.txt"
            source.write_text("hello")
            target = tmp / "target.txt"

            success = safe_symlink(source, target)
            self.assertTrue(success)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.read_text(), "hello")

    def test_safe_symlink_windows_privilege_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source = tmp / "source.txt"
            source.write_text("hello")
            target = tmp / "target.txt"

            win_err = OSError("A required privilege is not held by the client")
            win_err.winerror = 1314

            with patch("aikito_platform.is_windows", return_value=True):
                with patch.object(Path, "symlink_to", side_effect=win_err):
                    with patch("sys.stderr"):
                        success = safe_symlink(source, target)
                        self.assertFalse(success)

    def test_safe_symlink_windows_directory_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            source_dir = tmp / "source_dir"
            source_dir.mkdir()
            target_dir = tmp / "target_dir"

            with patch("aikito_platform.is_windows", return_value=True):
                with patch.object(Path, "symlink_to") as mock_symlink:
                    safe_symlink(source_dir, target_dir)
                    mock_symlink.assert_called_once_with(
                        source_dir, target_is_directory=True
                    )

    def test_check_credential_permissions_posix(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "cred.json"
            f.write_text("{}")

            mock_stat_600 = MagicMock()
            mock_stat_600.st_mode = 0o100600
            mock_stat_644 = MagicMock()
            mock_stat_644.st_mode = 0o100644

            with patch("aikito_platform.is_windows", return_value=False):
                with patch.object(Path, "stat", return_value=mock_stat_600):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertTrue(is_secure)
                    self.assertEqual(desc, "0o600")

                with patch.object(Path, "stat", return_value=mock_stat_644):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)


    def test_check_credential_permissions_windows_icacls(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "cred.json"
            f.write_text("{}")

            with patch("aikito_platform.is_windows", return_value=True):
                # Mock secure icacls output
                mock_res = MagicMock()
                mock_res.stdout = "NT AUTHORITY\\SYSTEM:(F)\nDESKTOP-TEST\\user:(R)\n"
                with patch("subprocess.run", return_value=mock_res):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertTrue(is_secure)

                # Mock insecure icacls output containing Everyone
                mock_res.stdout = "NT AUTHORITY\\SYSTEM:(F)\n\\EVERYONE:(R)\n"
                with patch("subprocess.run", return_value=mock_res):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)

    def test_get_permission_fix_cmd(self) -> None:
        with patch("aikito_platform.is_windows", return_value=False):
            self.assertEqual(get_permission_fix_cmd("~/.claude.json"), "chmod 600 ~/.claude.json")

        with patch("aikito_platform.is_windows", return_value=True):
            self.assertIn("icacls", get_permission_fix_cmd("~/.claude.json"))

    def test_resolve_executable(self) -> None:
        cmd = ["claude", "mcp", "list"]
        with patch("aikito_platform.is_windows", return_value=True):
            with patch("shutil.which", return_value=r"C:\Users\test\AppData\Roaming\npm\claude.cmd"):
                resolved = resolve_executable(cmd)
                self.assertEqual(resolved[0], r"C:\Users\test\AppData\Roaming\npm\claude.cmd")
                self.assertEqual(resolved[1:], ["mcp", "list"])

        with patch("aikito_platform.is_windows", return_value=False):
            resolved = resolve_executable(cmd)
            self.assertEqual(resolved, cmd)

    def test_get_workspace_config_dir(self) -> None:
        home = Path("/Users/test")
        with patch("aikito_platform.is_windows", return_value=False):
            with patch.dict(os.environ, {"XDG_CONFIG_HOME": "/custom/config"}, clear=True):
                self.assertEqual(get_workspace_config_dir(home), Path("/custom/config/aikito"))

        with patch("aikito_platform.is_windows", return_value=True):
            appdata = "/Users/test/AppData/Roaming"
            with patch.dict(os.environ, {"APPDATA": appdata}, clear=True):
                self.assertEqual(
                    get_workspace_config_dir(home),
                    Path(appdata) / "aikito",
                )


    def test_get_default_editor(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with patch("aikito_platform.is_windows", return_value=False):
                self.assertEqual(get_default_editor(), "vi")

            with patch("aikito_platform.is_windows", return_value=True):
                self.assertEqual(get_default_editor(), "notepad")

        with patch.dict(os.environ, {"EDITOR": "code --wait"}, clear=True):
            self.assertEqual(get_default_editor(), "code --wait")

    def test_safe_relative_path(self) -> None:
        home = Path("/home/user")
        sub = Path("/home/user/projects/demo")
        self.assertEqual(safe_relative_path(sub, home), "~/projects/demo")

        other_drive = Path("/other/path")
        self.assertEqual(safe_relative_path(other_drive, home), str(other_drive))


if __name__ == "__main__":
    unittest.main()
