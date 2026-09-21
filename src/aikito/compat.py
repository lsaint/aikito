"""Platform abstraction and compatibility layer for Aikito.

Encapsulates OS-specific behavior for Windows, macOS, and Linux:
- Strict symbolic link requirement and Windows Developer Mode detection
- Credential file permission inspection (POSIX mode 0600 vs Windows ACL via icacls)
- Windows CLI executable resolution (.cmd/.bat) for subprocess invocations
- Cross-platform configuration directory and path display normalization
- Safe editor selection and console encoding setup
"""

from __future__ import annotations

import ctypes
import functools
import importlib.resources
import os
import shlex
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import Sequence

SYMLINK_REQUIREMENT_GUIDANCE = (
    "[ERROR] Aikito requires symbolic link support to manage Agent resources.\n"
    "On Windows, symbolic links require enabling Developer Mode (no restart required):\n\n"
    "  Option 1 (Windows Settings GUI):\n"
    "    Settings -> System -> For developers -> Developer Mode -> Turn On\n\n"
    "  Option 2 (PowerShell / Command Prompt as Administrator):\n"
    '    reg add "HKLM\\SOFTWARE\\Microsoft\\Windows\\CurrentVersion\\AppModelUnlock" /t REG_DWORD /f /v "AllowDevelopmentWithoutDevLicense" /d "1"\n'
)


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


@functools.lru_cache(maxsize=1)
def can_symlink() -> bool:
    """Return True if symlinks can be created in the current environment.

    Probes actual symlink creation in a temporary directory on Windows, which
    reliably detects Developer Mode and Administrator capabilities.
    Can be forced to False via AIKITO_FORCE_NO_SYMLINK=1.
    """
    if os.environ.get("AIKITO_FORCE_NO_SYMLINK") == "1":
        return False
    if not is_windows():
        return True

    try:
        with tempfile.TemporaryDirectory(prefix="aikito-symlink-probe-") as temp_dir:
            temp_path = Path(temp_dir)
            probe_src = temp_path / "probe_src"
            probe_src.write_text("probe", encoding="utf-8")
            probe_dst = temp_path / "probe_dst"
            probe_dst.symlink_to(probe_src)
            return True
    except OSError:
        return False


def require_symlink_support() -> None:
    """Ensure the environment supports symlink creation; exit with actionable instructions if not."""
    if not can_symlink():
        print(SYMLINK_REQUIREMENT_GUIDANCE, file=sys.stderr)
        sys.exit(1)


def safe_symlink(source: Path, target: Path) -> bool:
    """Create a symbolic link from target to source.

    Handles Windows directory symlinks explicitly (target_is_directory=True).
    """
    try:
        if is_windows():
            is_dir = source.is_dir() if source.exists() else False
            target.symlink_to(source, target_is_directory=is_dir)
        else:
            target.symlink_to(source)
        return True
    except OSError as exc:
        print(
            f"[ERROR] Failed to create symlink {target} -> {source}: {exc}",
            file=sys.stderr,
        )
        return False


def resolve_symlink_target(path: Path) -> Path:
    """Resolve a symlink target handling raw readlink and Windows UNC/short names."""
    try:
        raw = os.readlink(path)
        if isinstance(raw, str):
            if raw.startswith("\\\\?\\UNC\\"):
                raw = "\\\\" + raw[8:]
            elif raw.startswith("\\\\?\\"):
                raw = raw[4:]
        target = path.parent / raw if not os.path.isabs(raw) else Path(raw)
    except OSError:
        target = path.resolve(strict=False)

    # For broken symlinks or non-existent targets, resolve the nearest existing ancestor
    # so short names (e.g. RUNNER~1 on Windows) and intermediate symlinks are expanded.
    parts: list[str] = []
    curr = target
    while not curr.exists() and curr != curr.parent:
        parts.append(curr.name)
        curr = curr.parent
    try:
        resolved_curr = curr.resolve()
    except OSError:
        resolved_curr = curr
    for part in reversed(parts):
        resolved_curr = resolved_curr / part
    return resolved_curr


_resolve_symlink_target = resolve_symlink_target


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
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def check_credential_permissions(path: Path) -> tuple[bool, str]:
    """Check that a secret-bearing configuration file has secure permissions.

    On POSIX: checks stat mode is 0600.
    On Windows: uses PowerShell Get-Acl with SID-based principal matching so
    that the result is locale-independent (icacls output is localized on
    non-English Windows).

    Safe SIDs (access is acceptable):
      - File owner (current user, detected via whoami /user)
      - S-1-5-18  SYSTEM
      - S-1-5-32-544  Administrators

    Any other SID with granted access is treated as insecure.

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

    # --- Windows: SID-based ACL check via PowerShell (locale-independent) ---
    # Step 1: get current user's SID
    try:
        sid_result = subprocess.run(
            ["whoami", "/user", "/fo", "csv", "/nh"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
        owner_sid: str | None = None
        if sid_result.returncode == 0:
            # output: "DOMAIN\\user","S-1-5-21-..."
            parts = [p.strip().strip('"') for p in sid_result.stdout.strip().split(",")]
            if len(parts) >= 2:
                owner_sid = parts[1]
    except Exception:
        owner_sid = None

    # Known-safe SIDs (locale-independent)
    safe_sids = {
        "S-1-5-18",  # SYSTEM
        "S-1-5-32-544",  # Administrators
        "S-1-3-4",  # Owner Rights
    }
    if owner_sid:
        safe_sids.add(owner_sid)

    # Step 2: query ACL via PowerShell Get-Acl or Get-Item.GetAccessControl
    # Escape single quotes in path for PowerShell string literal ('' = escaped ')
    ps_path = str(path).replace("'", "''")
    ps_script = (
        f"$item = Get-Item -LiteralPath '{ps_path}';"
        "$acl = $item.GetAccessControl();"
        "$acl.Access | ForEach-Object {"
        "$sid = $_.IdentityReference.Translate([System.Security.Principal.SecurityIdentifier]).Value;"
        "Write-Output $sid"
        "}"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=10,
        )
        if result.returncode != 0:
            return False, f"ACL unchecked (PowerShell exit {result.returncode})"

        granted_sids = {
            line.strip() for line in result.stdout.splitlines() if line.strip()
        }
        unknown = granted_sids - safe_sids
        if unknown:
            return (
                False,
                f"ACL allows access to unexpected SIDs: {', '.join(sorted(unknown))}",
            )
        return True, "ACL restricted"
    except Exception as exc:
        return False, f"ACL unchecked ({exc})"


def get_permission_fix_cmd(path: Path) -> str:
    """Return platform-specific remediation command to secure a credential file.

    Takes the absolute Path so the Windows icacls command never contains
    ~ (which cmd.exe and PowerShell do not expand for external programs).
    """
    if is_windows():
        abs_path = str(path.expanduser().resolve())
        return f'icacls "{abs_path}" /inheritance:r /grant:r "%USERNAME%:(R,W)"'
    # Quote path to handle spaces
    return f'chmod 600 "{path}"'


def secure_directory_permissions(path: Path) -> bool:
    """Harden directory permissions for state and transaction stores.

    On POSIX: chmod 0700 (owner read/write/exec only).
    On Windows: applies icacls to disable inheritance and grant only the current user full access.
    """
    if not path.exists():
        return False
    if not is_windows():
        try:
            path.chmod(0o700)
            return True
        except OSError:
            return False

    try:
        username = os.environ.get("USERNAME") or os.environ.get("USER")
        principal = f"{username}:(OI)(CI)(F)" if username else "*S-1-3-4:(OI)(CI)(F)"
        result = subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", principal],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _atomic_write_text(
    target_path: Path, content: str, encoding: str = "utf-8"
) -> None:
    """
    Atomically write text content to target_path using a temporary file in the same
    directory and replacing target_path with os.replace.
    Preserves file mode, permissions, and metadata (ACLs/xattrs) of existing files,
    and applies standard umask permissions to newly created files.
    """
    target_path = target_path.resolve()
    parent = target_path.parent
    parent.mkdir(parents=True, exist_ok=True)
    temp_file = tempfile.NamedTemporaryFile(
        mode="w",
        dir=parent,
        prefix=f".{target_path.name}.tmp.",
        delete=False,
        encoding=encoding,
        newline="",
    )
    temp_path = Path(temp_file.name)
    try:
        temp_file.write(content)
        temp_file.flush()
        os.fsync(temp_file.fileno())
        temp_file.close()

        if target_path.exists():
            try:
                shutil.copystat(target_path, temp_path)
                try:
                    os.utime(temp_path, None)
                except OSError:
                    pass
            except OSError:
                try:
                    st = target_path.stat()
                    os.chmod(temp_path, stat.S_IMODE(st.st_mode))
                except OSError:
                    pass
            if hasattr(os, "chown"):
                try:
                    st = target_path.stat()
                    os.chown(temp_path, -1, st.st_gid)
                except OSError:
                    pass
        else:
            try:
                current_umask = os.umask(0)
                os.umask(current_umask)
                os.chmod(temp_path, 0o666 & ~current_umask)
            except OSError:
                pass

        os.replace(temp_path, target_path)
    except Exception:
        temp_file.close()
        if temp_path.exists():
            try:
                temp_path.unlink()
            except OSError:
                pass
        raise


def check_directory_permissions(path: Path) -> tuple[bool, str]:
    """Check that a state or transaction directory has secure permissions.

    On POSIX: checks mode does not allow group or other write permissions.
    On Windows: uses SID-based ACL matching.
    """
    if not path.exists():
        return True, "missing"
    if not is_windows():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o022 != 0:
            return False, oct(mode)
        return True, oct(mode)

    return check_credential_permissions(path)


def is_reparse_point(path: Path) -> bool:
    """Check if a path is a symbolic link, junction, or other reparse point.

    On POSIX: checks path.is_symlink().
    On Windows: checks GetFileAttributesW for FILE_ATTRIBUTE_REPARSE_POINT (0x400).
    """
    if not is_windows():
        return path.is_symlink()
    if not path.is_symlink() and not path.exists():
        return False
    try:
        file_attribute_reparse_point = 0x00000400
        invalid_file_attributes = 0xFFFFFFFF
        attrs = ctypes.windll.kernel32.GetFileAttributesW(str(path))  # type: ignore[attr-defined]
        if (
            attrs in (-1, invalid_file_attributes)
            or (attrs & invalid_file_attributes) == invalid_file_attributes
        ):
            return path.is_symlink()
        return bool(attrs & file_attribute_reparse_point)
    except Exception:
        return path.is_symlink()


def get_physical_path(path: Path) -> Path:
    """Resolve the true physical path on the filesystem, resolving case aliases and symlinks.

    On Windows: uses GetFinalPathNameByHandleW to normalize drive letter casing and volume roots.
    On POSIX: uses Path.resolve(strict=False).
    """
    if not is_windows():
        return path.resolve(strict=False)
    try:
        file_share_read = 0x00000001
        file_share_write = 0x00000002
        file_share_delete = 0x00000004
        open_existing = 3
        file_flag_backup_semantics = 0x02000000
        file_name_normalized = 0x0
        volume_name_dos = 0x0

        handle = ctypes.windll.kernel32.CreateFileW(  # type: ignore[attr-defined]
            str(path),
            0,
            file_share_read | file_share_write | file_share_delete,
            None,
            open_existing,
            file_flag_backup_semantics,
            None,
        )
        if handle and handle != -1:
            try:
                buf = ctypes.create_unicode_buffer(1024)
                ret = ctypes.windll.kernel32.GetFinalPathNameByHandleW(  # type: ignore[attr-defined]
                    handle, buf, 1024, file_name_normalized | volume_name_dos
                )
                if ret > 0:
                    raw = buf.value
                    if raw.startswith("\\\\?\\UNC\\"):
                        raw = "\\\\" + raw[8:]
                    elif raw.startswith("\\\\?\\"):
                        raw = raw[4:]
                    return Path(raw)
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
    except Exception:
        pass
    res = path.resolve(strict=False)
    s = str(res)
    if s.startswith("\\\\?\\UNC\\"):
        return Path("\\\\" + s[8:])
    elif s.startswith("\\\\?\\"):
        return Path(s[4:])
    return res


def normalize_file_bytes(data: bytes | None) -> bytes | None:
    """Normalize CRLF to LF for cross-platform byte comparison."""
    if data is None:
        return None
    return data.replace(b"\r\n", b"\n")


class _DarwinAttrList(ctypes.Structure):
    _fields_ = [
        ("bitmapcount", ctypes.c_ushort),
        ("reserved", ctypes.c_ushort),
        ("commonattr", ctypes.c_uint),
        ("volattr", ctypes.c_uint),
        ("dirattr", ctypes.c_uint),
        ("fileattr", ctypes.c_uint),
        ("forkattr", ctypes.c_uint),
    ]


def is_directory_case_sensitive(path: Path) -> bool:
    """Return True if the directory at *path* is case-sensitive, False otherwise.

    On Windows, NTFS volumes may have per-directory case-sensitivity enabled
    (Win10 1803+).  GetVolumeInformationW only exposes the volume-level flag and
    misreports per-directory state, so we probe with a temporary scratch file pair
    instead.  Falls back conservatively to False if the probe cannot be performed.
    On macOS, getattrlist is used against the containing volume.  On Linux and
    other POSIX systems the filesystem is assumed case-sensitive.
    """
    if is_windows():
        probe_dir = path if path.is_dir() else path.parent
        if not probe_dir.is_dir():
            return False
        try:
            import ctypes

            # 1. Query per-directory case sensitivity via NtQueryInformationFile (Win10 1803+)
            # Open handle with FILE_READ_ATTRIBUTES and FILE_FLAG_BACKUP_SEMANTICS (read-only query on directory)
            FILE_READ_ATTRIBUTES = 0x0080
            FILE_SHARE_READ = 1
            FILE_SHARE_WRITE = 2
            FILE_SHARE_DELETE = 4
            OPEN_EXISTING = 3
            FILE_FLAG_BACKUP_SEMANTICS = 0x02000000

            handle = ctypes.windll.kernel32.CreateFileW(  # type: ignore[attr-defined]
                str(probe_dir),
                FILE_READ_ATTRIBUTES,
                FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
                None,
                OPEN_EXISTING,
                FILE_FLAG_BACKUP_SEMANTICS,
                None,
            )
            INVALID_HANDLE_VALUE = -1
            if handle != INVALID_HANDLE_VALUE and handle != 0:
                try:

                    class IO_STATUS_BLOCK(ctypes.Structure):
                        _fields_ = [
                            ("Status", ctypes.c_void_p),
                            ("Information", ctypes.c_ulong),
                        ]

                    class FILE_CASE_SENSITIVE_INFORMATION(ctypes.Structure):
                        _fields_ = [("Flags", ctypes.c_ulong)]

                    io_status = IO_STATUS_BLOCK()
                    info = FILE_CASE_SENSITIVE_INFORMATION()
                    FileCaseSensitiveInformation = 64
                    status = ctypes.windll.ntdll.NtQueryInformationFile(  # type: ignore[attr-defined]
                        handle,
                        ctypes.byref(io_status),
                        ctypes.byref(info),
                        ctypes.sizeof(info),
                        FileCaseSensitiveInformation,
                    )
                    if status == 0:
                        FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x00000001
                        return bool(info.Flags & FILE_CS_FLAG_CASE_SENSITIVE_DIR)
                finally:
                    ctypes.windll.kernel32.CloseHandle(handle)  # type: ignore[attr-defined]
        except Exception:
            pass

        # 2. Fallback to volume-level query via GetVolumeInformationW
        try:
            root_path = str(probe_dir.anchor) if probe_dir.anchor else "C:\\"
            volume_flags = ctypes.c_uint32()
            ret = ctypes.windll.kernel32.GetVolumeInformationW(  # type: ignore[attr-defined]
                root_path, None, 0, None, None, ctypes.byref(volume_flags), None, 0
            )
            FILE_CASE_SENSITIVE_SEARCH = 0x00000001
            if ret:
                return bool(volume_flags.value & FILE_CASE_SENSITIVE_SEARCH)
        except Exception:
            pass
        return False

    if sys.platform == "darwin":
        try:
            attr_list = _DarwinAttrList(
                bitmapcount=5,
                reserved=0,
                commonattr=0,
                volattr=0x00020000,  # ATTR_VOL_CAPABILITIES
                dirattr=0,
                fileattr=0,
                forkattr=0,
            )
            libc = ctypes.cdll.LoadLibrary("libc.dylib")
            buf = ctypes.create_string_buffer(256)
            query_path = path if path.exists() else path.parent
            ret = libc.getattrlist(
                str(query_path).encode("utf-8"),
                ctypes.byref(attr_list),
                buf,
                ctypes.sizeof(buf),
                0,
            )
            if ret == 0:
                caps = struct.unpack_from("4I", buf.raw, 4)
                vol_cap_fmt_case_sensitive = 0x00000100
                return bool(caps[0] & vol_cap_fmt_case_sensitive)
        except Exception:
            pass
        return False

    # Linux / other POSIX: ext4 / btrfs / tmpfs are case-sensitive by default
    return True


def is_same_target_location(p1: Path, p2: Path) -> bool:
    """Check if two target paths refer to the same physical file location without resolving target's own symlink."""
    try:
        p1_dir = get_physical_path(p1.parent)
        p2_dir = get_physical_path(p2.parent)
        probe = p1_dir
        while not probe.exists() and probe != probe.parent:
            probe = probe.parent
        case_sensitive = (
            is_directory_case_sensitive(probe) if probe.exists() else not is_windows()
        )
        if not case_sensitive:
            return (
                str(p1_dir).casefold() == str(p2_dir).casefold()
                and p1.name.casefold() == p2.name.casefold()
            )
        return p1_dir == p2_dir and p1.name == p2.name
    except Exception:
        return str(p1).casefold() == str(p2).casefold()


def check_case_collision(
    names: Sequence[str], dir_path: Path
) -> tuple[str, str] | None:
    """Check for resource names that collide on case-insensitive filesystems."""
    if is_directory_case_sensitive(dir_path):
        return None
    seen: dict[str, str] = {}
    for name in names:
        lower = name.lower()
        if lower in seen and seen[lower] != name:
            return seen[lower], name
        seen[lower] = name
    return None


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
    On Windows: uses shlex.split(cmd, posix=False) so backslashes in paths are preserved,
    and strips outer enclosing quotes from tokens.
    """
    if not cmd.strip():
        return []

    if not is_windows():
        try:
            return shlex.split(cmd, posix=True)
        except ValueError:
            return cmd.split()

    try:
        raw_parts = shlex.split(cmd, posix=False)
    except ValueError:
        raw_parts = cmd.split()

    parts: list[str] = []
    for arg in raw_parts:
        if len(arg) >= 2 and (
            (arg.startswith('"') and arg.endswith('"'))
            or (arg.startswith("'") and arg.endswith("'"))
        ):
            parts.append(arg[1:-1])
        else:
            parts.append(arg)
    return parts


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
    env_editor = (os.environ.get("VISUAL") or "").strip() or (
        os.environ.get("EDITOR") or ""
    ).strip()
    if env_editor:
        return env_editor
    return "notepad" if is_windows() else "vi"


def safe_relative_path(path: Path, base: Path) -> str:
    """Return a display string relative to base with ~/ prefix, or fallback to posix string.

    Never resolves symbolic links to avoid side effects during path display.
    Cross-drive paths on Windows gracefully fallback to the posix path string.
    """
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


def _package_resource_dir(name: str) -> Path:
    """Resolve a top-level package data subdirectory by name.

    Raises RuntimeError with a clear installation hint if the directory is
    missing, so callers see an actionable message instead of a confusing
    AttributeError or FileNotFoundError deep in importlib.resources.
    """
    try:
        ref = importlib.resources.files("aikito").joinpath(name)
        path = Path(str(ref))
        if path.is_dir():
            return path
    except (ImportError, OSError):
        pass
    raise RuntimeError(
        f"Aikito {name} directory is missing. "
        "Ensure the package was installed correctly with package data."
    )
