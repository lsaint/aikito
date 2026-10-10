"""Secure credential storage, resolution, and OS keychain integration.

Provides secure credential resolution for background services operating
without ambient shell environment variables. Credential values are stored
outside the workspace and Git repository in user-controlled configuration
directories with strict owner-only permissions (0600 on POSIX).
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path

from ..compat import get_workspace_config_dir, is_windows
from .remote_access import RemoteCredentialError
from .remote_binding import RemoteAuth


def get_credentials_path(home: Path | None = None) -> Path:
    """Return the secure credentials file path in user configuration directory."""
    home = Path.home() if home is None else home.expanduser().resolve()
    return get_workspace_config_dir(home) / "credentials"


def _check_credentials_file_permissions(path: Path) -> None:
    """Ensure the credentials file is readable only by the owner on POSIX systems."""
    if is_windows() or not path.exists():
        return
    st = path.stat()
    if st.st_mode & 0o077 != 0:
        raise RemoteCredentialError(
            f"Insecure permissions on credentials file '{path}': "
            "file must be accessible only by owner (mode 0600)."
        )


def read_credentials_file(path: Path) -> dict[str, str]:
    """Read key-value pairs from a secure credentials file.

    Format: KEY=VALUE (supports comments starting with # and optional quotes).
    """
    if not path.exists():
        return {}
    _check_credentials_file_permissions(path)

    credentials: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:
        raise RemoteCredentialError(
            f"Failed to read credentials file '{path}': {exc}"
        ) from exc

    for line_idx, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip()
        if (val.startswith('"') and val.endswith('"')) or (
            val.startswith("'") and val.endswith("'")
        ):
            val = val[1:-1]
        if key:
            credentials[key] = val

    return credentials


def write_credentials_file(path: Path, credentials: Mapping[str, str]) -> None:
    """Atomically write credentials to file with strict owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if not is_windows():
        try:
            os.chmod(path.parent, 0o700)
        except OSError:
            pass

    content = [
        "# Aikito secure credentials storage",
        "# Keep this file private; permissions must be 0600",
        "",
    ]
    for k in sorted(credentials.keys()):
        content.append(f"{k}={credentials[k]}")
    content.append("")
    text_data = "\n".join(content)

    tmp_dir = path.parent
    tmp_file = tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=str(tmp_dir),
        delete=False,
        prefix=".tmp-creds-",
    )
    try:
        tmp_path = Path(tmp_file.name)
        tmp_file.write(text_data)
        tmp_file.flush()
        tmp_file.close()

        if not is_windows():
            os.chmod(tmp_path, 0o600)

        os.replace(tmp_path, path)
    finally:
        if tmp_file is not None and not tmp_file.closed:
            tmp_file.close()
        tmp_path = Path(tmp_file.name)
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def save_credential(
    key: str,
    value: str,
    *,
    home: Path | None = None,
    path: Path | None = None,
) -> Path:
    """Save or update a credential in the secure credentials file."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise RemoteCredentialError(f"Invalid credential key name: '{key}'")
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise RemoteCredentialError("Invalid credential value; must be ASCII printable")

    target = path if path is not None else get_credentials_path(home)
    creds = read_credentials_file(target) if target.exists() else {}
    creds[key] = value
    write_credentials_file(target, creds)
    return target


def query_keychain(key: str) -> str | None:
    """Query the operating system keychain for a stored credential."""
    if sys.platform == "darwin":
        cmd = [
            "/usr/bin/security",
            "find-generic-password",
            "-s",
            "aikito",
            "-a",
            key,
            "-w",
        ]
        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=3.0,
                check=False,
            )
            if res.returncode == 0:
                secret = res.stdout.strip()
                if secret and all(33 <= ord(c) <= 126 for c in secret):
                    return secret
        except Exception:
            return None
    elif sys.platform.startswith("linux"):
        secret_tool = shutil.which("secret-tool")
        if secret_tool:
            cmd = [secret_tool, "lookup", "service", "aikito", "key", key]
            try:
                res = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=3.0,
                    check=False,
                )
                if res.returncode == 0:
                    secret = res.stdout.strip()
                    if secret and all(33 <= ord(c) <= 126 for c in secret):
                        return secret
            except Exception:
                return None
    return None


def save_keychain(key: str, value: str) -> None:
    """Store credential in the operating system keychain."""
    if not re.fullmatch(r"[A-Z_][A-Z0-9_]*", key):
        raise RemoteCredentialError(f"Invalid credential key name: '{key}'")
    if not value or any(not 33 <= ord(c) <= 126 for c in value):
        raise RemoteCredentialError("Invalid credential value; must be ASCII printable")

    if is_windows():
        raise RemoteCredentialError("OS keychain is not supported on Windows")

    if sys.platform == "darwin":
        cmd = [
            "/usr/bin/security",
            "add-generic-password",
            "-U",
            "-s",
            "aikito",
            "-a",
            key,
            "-w",
            value,
        ]
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0:
            raise RemoteCredentialError(
                f"Failed to save credential to macOS keychain: {res.stderr.strip() or res.stdout.strip()}"
            )
    elif sys.platform.startswith("linux"):
        secret_tool = shutil.which("secret-tool")
        if not secret_tool:
            raise RemoteCredentialError(
                "Cannot save to keychain: 'secret-tool' is not installed or not in PATH."
            )
        cmd = [secret_tool, "store", "--label=aikito", "service", "aikito", "key", key]
        res = subprocess.run(
            cmd, input=value, text=True, capture_output=True, check=False
        )
        if res.returncode != 0:
            raise RemoteCredentialError(
                f"Failed to save credential to Linux keyring: {res.stderr.strip() or res.stdout.strip()}"
            )
    else:
        raise RemoteCredentialError(f"OS keychain is not supported on {sys.platform}")


def resolve_credential_with_source(
    auth: RemoteAuth,
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
    credentials_path: Path | None = None,
) -> tuple[str, str]:
    """Resolve credential value and report source of resolution.

    Resolution order:
    1. Caller-supplied explicit environ mapping
    2. Ambient os.environ
    3. OS native keychain (macOS / Linux)
    4. Secure credentials file (~/.config/aikito/credentials)
    """
    key = auth.env

    # 1. Explicit environ mapping (takes precedence and does not fall back to ambient env)
    if environ is not None:
        if key in environ:
            val = environ[key]
            if type(val) is str and val and all(33 <= ord(c) <= 126 for c in val):
                return val, "explicit_environ"
        raise RemoteCredentialError("Remote credential is missing or invalid")

    # 2. Ambient environment
    if key in os.environ:
        val = os.environ[key]
        if type(val) is str and val and all(33 <= ord(c) <= 126 for c in val):
            return val, "environment"

    # 3. OS keychain
    val = query_keychain(key)
    if val is not None:
        return val, "keychain"

    # 4. Secure credentials file (checks configured path then standard ~/.config/aikito/credentials)
    creds_file = credentials_path or get_credentials_path(home)
    if not creds_file.exists():
        std_path = (
            (Path.home() if home is None else home)
            / ".config"
            / "aikito"
            / "credentials"
        )
        if std_path.exists():
            creds_file = std_path

    if creds_file.exists():
        file_creds = read_credentials_file(creds_file)
        if key in file_creds:
            val = file_creds[key]
            if type(val) is str and val and all(33 <= ord(c) <= 126 for c in val):
                return val, "credentials_file"

    raise RemoteCredentialError(
        f"Remote credential '{key}' could not be resolved from environment, "
        "keychain, or secure credentials file."
    )


def resolve_credential(
    auth: RemoteAuth,
    environ: Mapping[str, str] | None = None,
    *,
    home: Path | None = None,
) -> str:
    """Resolve credential value for remote store authentication."""
    val, _ = resolve_credential_with_source(auth, environ, home=home)
    return val
