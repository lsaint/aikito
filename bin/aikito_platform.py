"""Platform abstraction and compatibility layer for Aikito.

Encapsulates OS-specific behavior for Windows, macOS, and Linux:
- Symlink creation with Windows Developer Mode detection and WinError 1314 guidance
- Credential file permission inspection (POSIX mode 0600 vs Windows ACL via icacls)
- Windows CLI executable resolution (.cmd/.bat) for subprocess invocations
- Cross-platform configuration directory and path display normalization
- Safe editor selection and console encoding setup
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
import sys
import webbrowser
from pathlib import Path
from typing import Sequence


def is_windows() -> bool:
    """Return True if running on Windows."""
    return sys.platform == "win32"


def init_console_encoding() -> None:
    """Ensure stdout and stderr use UTF-8 on Windows consoles to prevent encoding errors."""
    if is_windows():
        try:
            if hasattr(sys.stdout, "reconfigure"):
                sys.stdout.reconfigure(encoding="utf-8")
            if hasattr(sys.stderr, "reconfigure"):
                sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass


def is_developer_mode_enabled() -> bool | None:
    """Check if Windows Developer Mode is enabled in the registry.

    Returns:
        True: Developer Mode is enabled in registry.
        False: Developer Mode is disabled in registry.
        None: Not running on Windows or unable to query registry.
    """
    if not is_windows():
        return None
    try:
        import winreg

        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\AppModelUnlock"
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            val, _ = winreg.QueryValueEx(key, "AllowDevelopmentWithoutDevLicense")
            return val == 1
    except Exception:
        return None


def can_symlink() -> bool:
    """Return True if symlinks can be created in the current environment.

    Probes actual symlink creation in a temporary directory on Windows, which
    reliably detects both Developer Mode and unprivileged/Administrator capabilities.
    """
    if not is_windows():
        return True
    try:
        import tempfile

        with tempfile.TemporaryDirectory(prefix="aikito-symlink-probe-") as temp_dir:
            temp_path = Path(temp_dir)
            probe_src = temp_path / "probe_src"
            probe_src.write_text("probe", encoding="utf-8")
            probe_dst = temp_path / "probe_dst"
            probe_dst.symlink_to(probe_src)
            return True
    except OSError:
        return False


def safe_symlink(source: Path, target: Path, quiet: bool = False) -> bool:
    """Create a symbolic link from target to source.

    Handles Windows directory symlinks explicitly and catches WinError 1314
    (privilege not held) to output actionable guidance for enabling Developer Mode.
    """
    try:
        if is_windows():
            # In Windows, directory symlinks require target_is_directory=True
            is_dir = source.is_dir() if source.exists() else False
            target.symlink_to(source, target_is_directory=is_dir)
        else:
            target.symlink_to(source)
        return True
    except OSError as exc:
        winerror = getattr(exc, "winerror", None)
        if winerror == 1314 or (is_windows() and exc.errno in (1, 13)):  # EPERM / EACCES
            if not quiet:
                print(
                    "[ERROR] Windows requires Developer Mode or Administrator privileges to create symlinks.\n"
                    "  - To enable Developer Mode: Settings -> System -> For developers -> Developer Mode (On)\n"
                    "  - Or run PowerShell as Administrator: reg add \"HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AppModelUnlock\" /t REG_DWORD /f /v \"AllowDevelopmentWithoutDevLicense\" /d \"1\"\n"
                    "  - Or use copy-based synchronization.",
                    file=sys.stderr,
                )
        else:
            if not quiet:
                print(
                    f"[ERROR] Failed to create symlink {target} -> {source}: {exc}",
                    file=sys.stderr,
                )
        return False


def secure_file_permissions(path: Path) -> bool:
    """Harden file permissions for sensitive/credential files.

    On POSIX: chmod 0600 (owner read/write only).
    On Windows: applies icacls to disable inheritance and grant only the
    current user read/write access.
    """
    if not path.exists():
        return False
    if not is_windows():
        try:
            path.chmod(0o600)
            return True
        except OSError:
            return False

    try:
        username = os.environ.get("USERNAME") or os.environ.get("USER")
        principal = f"{username}:(R,W)" if username else "*S-1-3-4:(R,W)"
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", principal],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_credential_permissions(path: Path) -> tuple[bool, str]:
    """Check that a secret-bearing configuration file has secure permissions.

    On POSIX: checks stat mode is 0600.
    On Windows: queries icacls to verify Everyone or standard Users cannot read.

    Returns:
        (is_secure, description)
    """
    if not path.exists():
        return True, "missing"

    if not is_windows():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode == 0o600:
            return True, oct(mode)
        return False, oct(mode)

    # Windows NTFS permissions check via icacls
    try:
        result = subprocess.run(
            ["icacls", str(path)],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode != 0:
            return False, f"ACL unchecked (icacls exit {result.returncode})"
        output = result.stdout.upper()
        # Insecure if Everyone, Users (BUILTIN\Users), or Authenticated Users have access
        insecure_principals = [
            "\\EVERYONE:",
            "BUILTIN\\USERS:",
            "NT AUTHORITY\\AUTHENTICATED USERS:",
        ]
        for principal in insecure_principals:
            if principal in output:
                return False, "ACL allows group access"
        return True, "ACL restricted"
    except Exception as exc:
        return False, f"ACL unchecked ({exc})"


def get_permission_fix_cmd(display_path: str) -> str:
    """Return platform-specific remediation command to secure a credential file."""
    if is_windows():
        return f'icacls {display_path} /inheritance:r /grant:r "%USERNAME%:(R,W)"'
    return f"chmod 600 {display_path}"


def resolve_executable(command: Sequence[str]) -> list[str]:
    """Resolve the command binary to its absolute path if needed.

    On Windows, subprocess.run(["claude", ...]) fails when claude is a .cmd/.bat
    script unless resolved via shutil.which().
    """
    if not command:
        return list(command)
    cmd_list = list(command)
    if is_windows():
        binary = cmd_list[0]
        resolved = shutil.which(binary)
        if resolved:
            cmd_list[0] = resolved
    return cmd_list


def split_command(cmd: str) -> list[str]:
    """Split a command line string into arguments handling platform escaping.

    On POSIX: uses shlex.split(cmd, posix=True).
    On Windows: uses shlex.split(cmd, posix=False) so backslashes in paths are preserved.
    """
    if not cmd.strip():
        return []
    import shlex

    try:
        return shlex.split(cmd, posix=(not is_windows()))
    except ValueError:
        return cmd.split()


def get_workspace_config_dir(home: Path) -> Path:
    """Return the configuration base directory (~/.config or %APPDATA%)."""
    if is_windows():
        app_data = os.environ.get("APPDATA")
        base_dir = Path(app_data) if app_data else home / ".config"
    else:
        config_home = os.environ.get("XDG_CONFIG_HOME")
        base_dir = Path(config_home).expanduser() if config_home else home / ".config"
    return base_dir / "aikito"


def get_default_editor() -> str:
    """Return the default editor name when $VISUAL and $EDITOR are unset."""
    env_editor = (
        (os.environ.get("VISUAL") or "").strip()
        or (os.environ.get("EDITOR") or "").strip()
    )
    if env_editor:
        return env_editor
    return "notepad" if is_windows() else "vi"


def safe_relative_path(path: Path, base: Path) -> str:
    """Return a display string relative to base with ~/ prefix, or absolute path.

    Gracefully handles cross-drive paths on Windows where relative_to raises ValueError.
    Always uses forward slashes so paths can safely be embedded in TOML or rendered consistently.
    """
    try:
        rel = path.resolve().relative_to(base.resolve())
        return f"~/{rel.as_posix()}"
    except ValueError:
        try:
            rel = path.relative_to(base)
            return f"~/{rel.as_posix()}"
        except ValueError:
            return path.as_posix()


def launch_browser(url: str) -> None:
    """Open a URL in the user's default browser."""
    if is_windows():
        try:
            os.startfile(url)  # type: ignore[attr-defined]
            return
        except Exception:
            pass
    webbrowser.open(url)

