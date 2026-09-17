"""
Aikito Update Notifier

Provides non-blocking, cached version checks and upgrade notifications
for the Aikito CLI across different distribution channels (Homebrew, uv, pipx).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, TextIO

from . import __version__

PYPI_URL = "https://pypi.org/pypi/aikito/json"
GITHUB_LATEST_URL = "https://github.com/lsaint/aikito/releases/latest"
DEFAULT_CHECK_INTERVAL = 86400  # 24 hours


def parse_semver(version_str: str) -> tuple[int, ...] | None:
    """Parse a SemVer string like '1.39.0' or 'v1.39.0' into a tuple of ints."""
    if not version_str or not isinstance(version_str, str):
        return None
    cleaned = version_str.strip().lstrip("v")
    main_part = cleaned.split("-")[0].split("+")[0]
    parts = main_part.split(".")
    if not parts:
        return None
    try:
        return tuple(int(p) for p in parts)
    except ValueError:
        return None


def is_newer_version(latest: str, current: str) -> bool:
    """Return True if latest version is strictly greater than current version."""
    v_latest = parse_semver(latest)
    v_current = parse_semver(current)
    if v_latest is None or v_current is None:
        return False
    return v_latest > v_current


def fetch_latest_version_pypi(timeout: float = 2.0) -> str | None:
    """Fetch latest published version from PyPI JSON API."""
    try:
        req = urllib.request.Request(
            PYPI_URL,
            headers={"User-Agent": f"aikito/{__version__}"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status == 200:
                data = json.loads(resp.read().decode("utf-8"))
                version = data.get("info", {}).get("version")
                if isinstance(version, str) and parse_semver(version) is not None:
                    return version.strip()
    except Exception:
        pass
    return None


def fetch_latest_version_github(timeout: float = 2.0) -> str | None:
    """Fetch latest release version via GitHub latest release redirect."""
    try:
        req = urllib.request.Request(
            GITHUB_LATEST_URL,
            headers={"User-Agent": f"aikito/{__version__}"},
            method="HEAD",
        )
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler)
        with opener.open(req, timeout=timeout) as resp:
            final_url = resp.geturl()
            if "/releases/tag/" in final_url:
                tag = final_url.split("/releases/tag/")[-1].strip()
                version = tag.lstrip("v")
                if parse_semver(version) is not None:
                    return version
    except Exception:
        pass
    return None


def fetch_latest_version(timeout: float = 2.0) -> str | None:
    """Attempt to fetch latest version from PyPI, falling back to GitHub."""
    ver = fetch_latest_version_pypi(timeout=timeout)
    if ver:
        return ver
    return fetch_latest_version_github(timeout=timeout)


def get_cache_path() -> Path:
    """Return the path to the update check cache file."""
    override = os.environ.get("AIKITO_UPDATE_CACHE_FILE")
    if override:
        return Path(override)

    if sys.platform == "win32":
        local_app_data = os.environ.get("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "aikito" / "cache" / "version_check.json"

    xdg_cache = os.environ.get("XDG_CACHE_HOME")
    if xdg_cache:
        base = Path(xdg_cache)
    else:
        base = Path.home() / ".cache"
    return base / "aikito" / "version_check.json"


def read_cache(cache_path: Path | None = None) -> dict[str, Any] | None:
    """Read cached update check data."""
    path = cache_path or get_cache_path()
    if not path.is_file():
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return None


def write_cache(data: dict[str, Any], cache_path: Path | None = None) -> None:
    """Write update check data to cache."""
    path = cache_path or get_cache_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = path.with_suffix(".tmp")
        with open(temp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        temp_path.replace(path)
    except Exception:
        pass


def is_cache_fresh(
    cache: dict[str, Any] | None,
    check_interval: float = DEFAULT_CHECK_INTERVAL,
    now: float | None = None,
) -> bool:
    """Return True if cache exists, has latest_version, and was checked within interval."""
    if not cache or "latest_version" not in cache:
        return False
    last_checked = cache.get("last_checked")
    if not isinstance(last_checked, (int, float)):
        return False
    current_time = time.time() if now is None else now
    return (current_time - last_checked) <= check_interval


def get_check_interval() -> float:
    """Return the configured update-check interval or the default."""
    try:
        return float(
            os.environ.get("AIKITO_UPDATE_CHECK_INTERVAL", str(DEFAULT_CHECK_INTERVAL))
        )
    except ValueError:
        return float(DEFAULT_CHECK_INTERVAL)


def get_upgrade_command() -> str:
    """Determine the recommended upgrade command based on current execution environment."""
    exe = sys.executable.lower()
    module_path = str(Path(__file__).resolve()).lower()

    # Homebrew detection
    if (
        "cellar" in exe
        or "homebrew" in exe
        or "cellar" in module_path
        or "homebrew" in module_path
    ):
        return "brew upgrade lsaint/tap/aikito"

    # uv tool detection
    if "uv/tools" in exe or "uv/tools" in module_path or "uv" in exe:
        return "uv tool upgrade aikito"

    # pipx detection
    if "pipx" in exe or "pipx" in module_path:
        return "pipx upgrade aikito"

    # Windows installer script
    if sys.platform == "win32" and (
        "programs\\aikito" in exe or "programs\\aikito" in module_path
    ):
        return (
            "irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1 | iex"
        )

    # Default fallback
    return "uv tool upgrade aikito"


def is_development_mode() -> bool:
    """Check if Aikito is running directly from a source git checkout."""
    if os.environ.get("AIKITO_FORCE_UPDATE_CHECK") == "1":
        return False
    try:
        root = Path(__file__).resolve().parent.parent.parent
        if (root / ".git").is_dir() and (root / "pyproject.toml").is_file():
            return True
    except Exception:
        pass
    return False


def should_check_update(
    *,
    aikito_dir: Path | None = None,
    command: str | None = None,
) -> bool:
    """Check whether update check and notification should proceed."""
    # Explicit bypass for testing
    if os.environ.get("AIKITO_FORCE_UPDATE_CHECK") == "1":
        return True

    # Disable if not attached to interactive terminal
    if not sys.stderr.isatty():
        return False

    # Explicit opt-out via environment variable
    if (
        os.environ.get("AIKITO_NO_UPDATE_NOTIFIER") == "1"
        or os.environ.get("NO_UPDATE_NOTIFIER") == "1"
    ):
        return False

    # Disable in CI/CD pipelines
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return False

    # Skip on machine-readable or exempt commands
    exempt_commands = {
        "completion",
        "path",
    }
    if command in exempt_commands:
        return False

    # Disable when running from development checkout
    if is_development_mode():
        return False

    # Check workspace config if available
    if aikito_dir:
        try:
            from .config import load_workspace_config

            cfg = load_workspace_config(aikito_dir)
            if not cfg.update.check:
                return False
        except Exception:
            pass

    return True


def spawn_background_check() -> None:
    """Spawn a detached background process to refresh the update cache."""
    try:
        kwargs: dict[str, Any] = {
            "stdin": subprocess.DEVNULL,
            "stdout": subprocess.DEVNULL,
            "stderr": subprocess.DEVNULL,
        }
        if sys.platform == "win32":
            flags = 0
            if hasattr(subprocess, "DETACHED_PROCESS"):
                flags |= subprocess.DETACHED_PROCESS
            if hasattr(subprocess, "CREATE_NO_WINDOW"):
                flags |= subprocess.CREATE_NO_WINDOW
            if flags:
                kwargs["creationflags"] = flags
        else:
            kwargs["start_new_session"] = True

        cmd = [sys.executable, "-m", "aikito.update_notifier", "--worker"]
        subprocess.Popen(cmd, **kwargs)
    except Exception:
        pass


def run_worker() -> None:
    """Worker process logic: fetch latest version and persist to cache."""
    version = fetch_latest_version(timeout=2.0)
    now = time.time()
    cache = read_cache() or {}
    cache["last_checked"] = now
    if version:
        cache["latest_version"] = version
    write_cache(cache)


def check_and_notify_update(
    current_version: str = __version__,
    aikito_dir: Path | None = None,
    command: str | None = None,
    args: Any = None,
    output: TextIO | None = None,
) -> None:
    """Check cache and notify user if an update is available; trigger background refresh if needed."""
    if command == "version" and (
        getattr(args, "check", False) or getattr(args, "json", False)
    ):
        return

    if not should_check_update(aikito_dir=aikito_dir, command=command):
        return

    out = output or sys.stderr
    cache = read_cache()
    now = time.time()
    check_interval = get_check_interval()

    # 1. Print notice only if cache is fresh and a newer version is cached
    fresh = is_cache_fresh(cache, check_interval, now=now)
    if fresh and cache and "latest_version" in cache:
        latest = str(cache["latest_version"])
        if is_newer_version(latest, current_version):
            upgrade_cmd = get_upgrade_command()
            print(
                f"\n[NOTICE] A new version of Aikito is available: {current_version} -> {latest}\n"
                f"To upgrade, run: {upgrade_cmd}\n",
                file=out,
            )

    # 2. Check if cache is stale or missing; spawn background refresh if so
    if not fresh:
        cached_data = cache or {}
        cached_data["last_checked"] = now
        write_cache(cached_data)
        spawn_background_check()


def cmd_version(args: Any) -> None:
    """Handler for 'aikito version [-c|--check] [--force] [--json]'."""
    check = getattr(args, "check", False)
    force = getattr(args, "force", False)
    json_output = getattr(args, "json", False)
    current = __version__
    latest: str | None = None
    now = time.time()
    cache = read_cache()
    fresh = is_cache_fresh(cache, get_check_interval(), now=now)

    if force:
        latest = fetch_latest_version(timeout=3.0)
        if latest:
            write_cache({"last_checked": now, "latest_version": latest})
    elif check or json_output:
        if fresh and cache:
            latest = str(cache["latest_version"])
        else:
            latest = fetch_latest_version(timeout=3.0)
            if latest:
                write_cache({"last_checked": now, "latest_version": latest})
            elif cache and "latest_version" in cache:
                latest = str(cache["latest_version"])
    else:
        if fresh and cache:
            latest = str(cache["latest_version"])

    has_update = is_newer_version(latest, current) if latest else False
    upgrade_cmd = get_upgrade_command() if has_update else None

    if json_output:
        data = {
            "version": current,
            "latest_version": latest,
            "update_available": has_update,
            "upgrade_command": upgrade_cmd,
        }
        print(json.dumps(data, indent=2))
        return

    print(f"aikito {current}")
    if check or force:
        if latest is None:
            print(
                "[WARNING] Could not check for updates (network request failed).",
                file=sys.stderr,
            )
        elif has_update:
            print(
                f"\n[NOTICE] A new version of Aikito is available: {current} -> {latest}\n"
                f"To upgrade, run: {upgrade_cmd}",
                file=sys.stderr,
            )
        else:
            print(f"Aikito is up to date (version {current}).", file=sys.stderr)


if __name__ == "__main__":
    if "--worker" in sys.argv:
        run_worker()
    else:

        class _Args:
            check = "-c" in sys.argv or "--check" in sys.argv
            force = "--force" in sys.argv
            json = "--json" in sys.argv

        cmd_version(_Args())
