import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aikito_platform import (
    can_symlink,
    check_credential_permissions,
    get_default_editor,
    get_permission_fix_cmd,
    get_workspace_config_dir,
    init_console_encoding,
    is_developer_mode_enabled,
    is_windows,
    launch_browser,
    resolve_executable,
    safe_relative_path,
    safe_symlink,
    secure_file_permissions,
    split_command,
)


class AikitoPlatformTest(unittest.TestCase):
    def test_is_windows(self) -> None:
        expected = sys.platform == "win32"
        self.assertEqual(is_windows(), expected)

    def test_init_console_encoding(self) -> None:
        init_console_encoding()

    def test_can_symlink(self) -> None:
        result = can_symlink()
        self.assertIsInstance(result, bool)

    def test_is_developer_mode_enabled(self) -> None:
        result = is_developer_mode_enabled()
        if not is_windows():
            self.assertIsNone(result)

    def test_secure_file_permissions(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "secret.toml"
            f.write_text("secret = 123", encoding="utf-8")
            with patch("aikito_platform.is_windows", return_value=False):
                self.assertTrue(secure_file_permissions(f))

            with patch("aikito_platform.is_windows", return_value=True):
                mock_proc = MagicMock(returncode=0)
                with patch("subprocess.run", return_value=mock_proc) as mock_run:
                    self.assertTrue(secure_file_permissions(f))
                    self.assertIn("icacls", mock_run.call_args.args[0][0])
                    self.assertIn("/inheritance:r", mock_run.call_args.args[0])

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
                mock_res = MagicMock(returncode=0)
                mock_res.stdout = "NT AUTHORITY\\SYSTEM:(F)\nDESKTOP-TEST\\user:(R)\n"
                with patch("subprocess.run", return_value=mock_res):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertTrue(is_secure)

                # Mock insecure icacls output containing Everyone
                mock_res.stdout = "NT AUTHORITY\\SYSTEM:(F)\n\\EVERYONE:(R)\n"
                with patch("subprocess.run", return_value=mock_res):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)

                # Mock icacls error returncode
                mock_res.returncode = 1
                with patch("subprocess.run", return_value=mock_res):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)
                    self.assertIn("ACL unchecked", desc)

    def test_get_permission_fix_cmd(self) -> None:
        with patch("aikito_platform.is_windows", return_value=False):
            self.assertEqual(
                get_permission_fix_cmd("~/.claude.json"), "chmod 600 ~/.claude.json"
            )

        with patch("aikito_platform.is_windows", return_value=True):
            self.assertIn("icacls", get_permission_fix_cmd("~/.claude.json"))

    def test_resolve_executable(self) -> None:
        cmd = ["claude", "mcp", "list"]
        with patch("aikito_platform.is_windows", return_value=True):
            with patch(
                "shutil.which",
                return_value=r"C:\Users\test\AppData\Roaming\npm\claude.cmd",
            ):
                resolved = resolve_executable(cmd)
                self.assertEqual(
                    resolved[0], r"C:\Users\test\AppData\Roaming\npm\claude.cmd"
                )
                self.assertEqual(resolved[1:], ["mcp", "list"])

        with patch("aikito_platform.is_windows", return_value=False):
            resolved = resolve_executable(cmd)
            self.assertEqual(resolved, cmd)

    def test_split_command(self) -> None:
        self.assertEqual(split_command(""), [])
        posix_cmd = 'code --wait --file "my file.txt"'
        self.assertEqual(
            split_command(posix_cmd), ["code", "--wait", "--file", "my file.txt"]
        )

        with patch("aikito_platform.is_windows", return_value=True):
            win_cmd = r'C:\Users\test\code.cmd --wait "C:\My Files\doc.txt"'
            res = split_command(win_cmd)
            self.assertEqual(
                res, [r"C:\Users\test\code.cmd", "--wait", r"C:\My Files\doc.txt"]
            )


    def test_launch_browser(self) -> None:
        with patch("aikito_platform.is_windows", return_value=True):
            with patch("os.startfile", create=True) as mock_startfile:
                launch_browser("http://127.0.0.1:8765")
                mock_startfile.assert_called_once_with("http://127.0.0.1:8765")

        with patch("aikito_platform.is_windows", return_value=False):
            with patch("webbrowser.open") as mock_open:
                launch_browser("http://127.0.0.1:8765")
                mock_open.assert_called_once_with("http://127.0.0.1:8765")

    def test_get_workspace_config_dir(self) -> None:
        home = Path("/Users/test")
        with patch("aikito_platform.is_windows", return_value=False):
            with patch.dict(
                os.environ, {"XDG_CONFIG_HOME": "/custom/config"}, clear=True
            ):
                self.assertEqual(
                    get_workspace_config_dir(home), Path("/custom/config/aikito")
                )

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
        self.assertEqual(
            safe_relative_path(other_drive, home), other_drive.as_posix()
        )

    def test_wrappers_content_and_structure(self) -> None:
        bin_dir = Path(__file__).resolve().parent.parent / "bin"
        cmd_file = bin_dir / "aikito.cmd"
        ps1_file = bin_dir / "aikito.ps1"

        self.assertTrue(cmd_file.exists())
        cmd_text = cmd_file.read_text(encoding="utf-8")
        self.assertIn("setlocal", cmd_text)
        self.assertIn("exit /b %ERRORLEVEL%", cmd_text)

        self.assertTrue(ps1_file.exists())
        ps1_text = ps1_file.read_text(encoding="utf-8")
        self.assertIn("exit $exitCode", ps1_text)


if __name__ == "__main__":
    unittest.main()

