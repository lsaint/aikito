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
        can_symlink.cache_clear()
        result = can_symlink()
        self.assertIsInstance(result, bool)

    def test_can_symlink_force_copy_env(self) -> None:
        can_symlink.cache_clear()
        with patch.dict(os.environ, {"AIKITO_FORCE_NO_SYMLINK": "1"}):
            self.assertFalse(can_symlink())
        can_symlink.cache_clear()

    def test_require_symlink_support(self) -> None:
        from aikito_platform import require_symlink_support

        with patch("aikito_platform.can_symlink", return_value=True):
            require_symlink_support()

        with patch("aikito_platform.can_symlink", return_value=False):
            with patch("sys.stderr"):
                with self.assertRaises(SystemExit) as cm:
                    require_symlink_support()
                self.assertEqual(cm.exception.code, 1)

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

    def test_check_credential_permissions_windows_sid_based(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            f = tmp / "cred.json"
            f.write_text("{}")

            with patch("aikito_platform.is_windows", return_value=True):
                owner_sid = "S-1-5-21-1234-5678-9012-1001"

                def make_run(whoami_out: str, ps_out: str, ps_rc: int = 0):
                    whoami_res = MagicMock(returncode=0, stdout=whoami_out)
                    ps_res = MagicMock(returncode=ps_rc, stdout=ps_out)
                    return [whoami_res, ps_res]

                # Secure: only owner, SYSTEM, Administrators
                safe_ps_out = f"{owner_sid}\nS-1-5-18\nS-1-5-32-544\n"
                with patch(
                    "subprocess.run",
                    side_effect=make_run(
                        f'"DOMAIN\\\\user","{owner_sid}"', safe_ps_out
                    ),
                ):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertTrue(is_secure)

                # Insecure: EVERYONE SID (S-1-1-0) granted — locale-independent
                insecure_ps_out = f"{owner_sid}\nS-1-5-18\nS-1-1-0\n"
                with patch(
                    "subprocess.run",
                    side_effect=make_run(
                        f'"DOMAIN\\\\user","{owner_sid}"', insecure_ps_out
                    ),
                ):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)
                    self.assertIn("S-1-1-0", desc)

                # PowerShell failure → unchecked
                with patch(
                    "subprocess.run",
                    side_effect=make_run(
                        f'"DOMAIN\\\\user","{owner_sid}"', "", ps_rc=1
                    ),
                ):
                    is_secure, desc = check_credential_permissions(f)
                    self.assertFalse(is_secure)
                    self.assertIn("ACL unchecked", desc)

    def test_get_permission_fix_cmd(self) -> None:
        # POSIX: quoted path for spaces
        p = Path("/home/user/my files/.claude.json")
        with patch("aikito_platform.is_windows", return_value=False):
            cmd = get_permission_fix_cmd(p)
            self.assertIn("chmod 600", cmd)
            self.assertIn('"', cmd)  # path must be quoted

        # Windows: absolute path, no bare ~
        win_path = Path("C:/Users/user/.claude.json")
        with patch("aikito_platform.is_windows", return_value=True):
            cmd = get_permission_fix_cmd(win_path)
            self.assertIn("icacls", cmd)
            self.assertNotIn("icacls ~", cmd)  # ~ must not appear as first path char

    def test_check_credential_permissions_windows_ps_path_single_quote(self) -> None:
        """Paths containing single quotes must not break the PowerShell script."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "cred.json"
            f.write_text("{}")
            with patch("aikito_platform.is_windows", return_value=True):
                owner_sid = "S-1-5-21-9999"
                whoami_out = f'"DOMAIN\\\\user","{owner_sid}"'
                ps_out = f"{owner_sid}\nS-1-5-18\n"

                captured: list[list] = []

                def fake_run(args, **kw):
                    captured.append(list(args))
                    if "whoami" in args[0]:
                        return MagicMock(returncode=0, stdout=whoami_out)
                    return MagicMock(returncode=0, stdout=ps_out)

                with patch("subprocess.run", side_effect=fake_run):
                    # Patch path to include a single quote
                    tricky = Path("/home/user/o'brien/.claude.json")
                    with patch.object(Path, "exists", return_value=True):
                        with patch("aikito_platform.is_windows", return_value=True):
                            check_credential_permissions(tricky)
                # The PowerShell command arg must escape single quotes with ''
                ps_call = next(c for c in captured if "powershell" in c[0])
                ps_cmd = ps_call[-1]
                self.assertNotIn(
                    "o'brien", ps_cmd
                )  # raw unescaped quote must not appear
                self.assertIn("o''brien", ps_cmd)  # doubled quote must appear

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
        self.assertEqual(safe_relative_path(other_drive, home), other_drive.as_posix())

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
