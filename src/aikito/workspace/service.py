"""Operating system native background service registration and management.

Supports:
- macOS: launchd LaunchAgent (~/Library/LaunchAgents/com.aikito.remote.<slug>.plist)
- Linux: systemd --user service (~/.config/systemd/user/aikito-remote-<slug>.service)
- Windows: Task Scheduler (schtasks.exe)
- Process-based fallback: detached daemon process
"""

from __future__ import annotations

import abc
import hashlib
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from ..compat import is_windows
from .auto_sync import is_worker_running
from .resource_state import WORKER_LOG_STATE, WORKER_STOP_STATE, state_path


def get_workspace_slug(workspace: Path) -> str:
    """Generate a stable, unique, alphanumeric slug for a workspace path."""
    canonical = str(workspace.expanduser().resolve())
    name_clean = re.sub(r"[^a-zA-Z0-9_.-]", "_", workspace.name) or "workspace"
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:8]
    return f"{name_clean}-{digest}"


def is_matching_aikito_command(
    cmd: list[str] | str | Path,
    target_version: str | None = None,
) -> bool:
    """Check if command arguments or binary execute and match target version."""
    try:
        cmd_list = [str(cmd)] if isinstance(cmd, (str, Path)) else list(cmd)
        if not cmd_list:
            return False

        bin_path = Path(cmd_list[0])
        if not bin_path.exists() and shutil.which(cmd_list[0]) is None:
            return False

        if target_version is None:
            from .. import __version__

            target_version = __version__

        proc = subprocess.Popen(
            [*cmd_list, "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            stdout, _ = proc.communicate(timeout=1.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            return False
        except Exception:
            try:
                proc.kill()
                proc.communicate()
            except Exception:
                pass
            return False

        if proc.returncode == 0:
            tokens = [t.strip("(),;\"'").lstrip("v") for t in stdout.split()]
            return target_version in tokens
    except Exception:
        pass
    return False


def _is_matching_aikito_bin(
    bin_path: Path | str, target_version: str | None = None
) -> bool:
    """Check if binary path exists, has aikito name, and matches package version."""
    try:
        p = Path(bin_path)
        if not p.is_file():
            return False
        if p.name.lower() not in ("aikito", "aikito.exe", "aikito.cmd", "aikito.bat"):
            return False
        return is_matching_aikito_command([str(p)], target_version=target_version)
    except Exception:
        return False


def get_aikito_command(
    workspace: Path,
    target_version: str | None = None,
) -> list[str]:
    """Resolve stable CLI command arguments for running remote watch.

    Priority:
    1. sys.argv[0] if it is a stable console script matching target version.
    2. shutil.which('aikito') on PATH, verified to match target version.
    3. Fallback to sys.executable -m aikito.
    """
    if sys.argv and sys.argv[0]:
        argv0 = Path(sys.argv[0])
        if not argv0.is_absolute():
            resolved = shutil.which(sys.argv[0])
            if resolved:
                argv0 = Path(resolved)
        if _is_matching_aikito_bin(argv0, target_version=target_version):
            return [str(argv0), "remote", "watch", "--workspace", str(workspace)]

    aikito_bin = shutil.which("aikito")
    if aikito_bin and _is_matching_aikito_bin(
        aikito_bin, target_version=target_version
    ):
        return [aikito_bin, "remote", "watch", "--workspace", str(workspace)]

    return [
        sys.executable,
        "-m",
        "aikito",
        "remote",
        "watch",
        "--workspace",
        str(workspace),
    ]


def tail_log(log_path: Path, max_lines: int = 15) -> str:
    """Read last N non-empty lines from worker log file."""
    if max_lines <= 0 or not log_path.exists():
        return ""
    try:
        content = log_path.read_text(encoding="utf-8", errors="replace")
        lines = [line for line in content.splitlines() if line.strip()]
        if not lines:
            return ""
        return "\n".join(lines[-max_lines:])
    except Exception:
        return ""


class BaseServiceManager(abc.ABC):
    """Abstract interface for background service management."""

    def __init__(self, workspace: Path, *, home: Path | None = None) -> None:
        self.workspace = workspace.expanduser().resolve()
        self.home = Path.home() if home is None else home.expanduser().resolve()
        self.slug = get_workspace_slug(self.workspace)
        self._command: list[str] | None = None

    @abc.abstractmethod
    def manager_name(self) -> str:
        """Name of the service manager (e.g. 'launchd', 'systemd', 'process')."""
        ...

    @abc.abstractmethod
    def get_service_name(self) -> str:
        """Identifier or filename of the registered service."""
        ...

    def get_command(self) -> list[str]:
        """Return executable command list for background worker."""
        if self._command is None:
            self._command = get_aikito_command(self.workspace)
        return list(self._command)

    @abc.abstractmethod
    def is_installed(self) -> bool:
        """Return True if service definition is currently installed."""
        ...

    @abc.abstractmethod
    def is_stale(self) -> bool:
        """Return True if installed service definition references a stale/invalid executable."""
        ...

    def get_log_path(self) -> Path:
        """Return path to background service stdout/stderr log."""
        return state_path(
            self.workspace,
            WORKER_LOG_STATE,
            create=True,
        )

    def is_running(self) -> tuple[bool, int | None]:
        """Check if worker is currently running using the worker lifecycle lock."""
        return is_worker_running(self.workspace)

    @abc.abstractmethod
    def install(self) -> None:
        """Install service definition."""
        ...

    @abc.abstractmethod
    def uninstall(self) -> None:
        """Uninstall service definition."""
        ...

    @abc.abstractmethod
    def start_service(self) -> None:
        """Start or trigger the registered service."""
        ...

    @abc.abstractmethod
    def stop_service(self) -> None:
        """Stop the registered service."""
        ...

    def start(self, timeout: float = 3.0) -> None:
        """Start the background service and verify worker acquisition."""
        running, _ = self.is_running()
        if running:
            return
        if not self.is_installed() or self.is_stale():
            self.install()
        self.start_service()

        deadline = time.monotonic() + timeout
        running, pid = self.is_running()
        while not running and time.monotonic() < deadline:
            time.sleep(0.15)
            running, pid = self.is_running()

        if not running:
            log_path = self.get_log_path()
            log_tail = tail_log(log_path)
            msg = (
                f"Background auto-sync service failed to acquire worker lock within {timeout:.1f} seconds.\n"
                f"  Service type: {self.manager_name()} ({self.get_service_name()})\n"
                f"  Log file:     {log_path}"
            )
            if log_tail:
                msg += f"\nRecent log output:\n{log_tail}"
            raise RuntimeError(msg)

    def stop(self, timeout: float = 35.0) -> None:
        """Stop the background service and wait for worker to terminate cleanly."""
        stop_path = state_path(self.workspace, WORKER_STOP_STATE, create=True)
        try:
            stop_path.touch()
        except Exception:
            pass

        try:
            self.stop_service()
        except Exception:
            pass

        # Gracefully wait for worker lock to be released (greater than HTTP request timeout)
        deadline = time.monotonic() + timeout
        running, pid = self.is_running()
        while running and time.monotonic() < deadline:
            time.sleep(0.2)
            running, pid = self.is_running()

        # If worker is still running after timeout, send SIGTERM or taskkill /f
        if running and pid is not None:
            if not is_windows():
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
            else:
                try:
                    subprocess.run(
                        ["taskkill", "/f", "/pid", str(pid)],
                        check=False,
                        capture_output=True,
                    )
                except Exception:
                    pass

            kill_deadline = time.monotonic() + 5.0
            while running and time.monotonic() < kill_deadline:
                time.sleep(0.2)
                running, pid = self.is_running()

            if running and pid is not None and not is_windows():
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass

        try:
            if stop_path.exists():
                stop_path.unlink()
        except OSError:
            pass


class LaunchdServiceManager(BaseServiceManager):
    """macOS launchd LaunchAgent service manager."""

    def manager_name(self) -> str:
        return "launchd"

    def get_service_name(self) -> str:
        return f"com.aikito.remote.{self.slug}"

    def _plist_path(self) -> Path:
        agents_dir = self.home / "Library" / "LaunchAgents"
        return agents_dir / f"{self.get_service_name()}.plist"

    def is_installed(self) -> bool:
        return self._plist_path().exists()

    def is_loaded(self) -> bool:
        res = subprocess.run(
            ["launchctl", "list", self.get_service_name()],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    def is_stale(self) -> bool:
        plist_path = self._plist_path()
        if not plist_path.exists():
            return False
        try:
            with open(plist_path, "rb") as f:
                data = plistlib.load(f)
            args = data.get("ProgramArguments", [])
            if not args or not Path(args[0]).exists():
                return True
            if args != self.get_command():
                return True
        except Exception:
            return True
        return False

    def install(self) -> None:
        plist_path = self._plist_path()
        plist_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.get_log_path()

        if self.is_loaded():
            subprocess.run(
                ["launchctl", "unload", "-w", str(plist_path)],
                capture_output=True,
                check=False,
            )

        plist_data = {
            "Label": self.get_service_name(),
            "ProgramArguments": self.get_command(),
            "WorkingDirectory": str(self.workspace),
            "RunAtLoad": True,
            "KeepAlive": {"Crashed": True},
            "ThrottleInterval": 30,
            "StandardOutPath": str(log_path),
            "StandardErrorPath": str(log_path),
            "ProcessType": "Background",
        }
        if "XDG_CONFIG_HOME" in os.environ:
            plist_data["EnvironmentVariables"] = {
                "XDG_CONFIG_HOME": os.environ["XDG_CONFIG_HOME"]
            }

        with open(plist_path, "wb") as f:
            plistlib.dump(plist_data, f)

        # Register with launchctl
        res = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to load launchd service {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
            )

    def uninstall(self) -> None:
        plist_path = self._plist_path()
        if plist_path.exists():
            if self.is_loaded():
                subprocess.run(
                    ["launchctl", "unload", "-w", str(plist_path)],
                    capture_output=True,
                    check=False,
                )
            try:
                plist_path.unlink()
            except OSError:
                pass

    def start_service(self) -> None:
        plist_path = self._plist_path()
        if not plist_path.exists() or self.is_stale():
            self.install()
        elif not self.is_loaded():
            res = subprocess.run(
                ["launchctl", "load", "-w", str(plist_path)],
                capture_output=True,
                text=True,
                check=False,
            )
            if res.returncode != 0:
                raise RuntimeError(
                    f"Failed to load launchd service {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
                )
        res = subprocess.run(
            ["launchctl", "start", self.get_service_name()],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to start launchd service {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
            )

    def stop_service(self) -> None:
        if self.is_loaded():
            subprocess.run(
                ["launchctl", "stop", self.get_service_name()],
                capture_output=True,
                check=False,
            )
            plist_path = self._plist_path()
            if plist_path.exists():
                subprocess.run(
                    ["launchctl", "unload", "-w", str(plist_path)],
                    capture_output=True,
                    check=False,
                )


class SystemdServiceManager(BaseServiceManager):
    """Linux systemd --user service manager."""

    def manager_name(self) -> str:
        return "systemd"

    def get_service_name(self) -> str:
        return f"aikito-remote-{self.slug}.service"

    def _unit_path(self) -> Path:
        unit_dir = self.home / ".config" / "systemd" / "user"
        return unit_dir / self.get_service_name()

    def is_installed(self) -> bool:
        return self._unit_path().exists()

    def is_stale(self) -> bool:
        unit_path = self._unit_path()
        if not unit_path.exists():
            return False
        try:
            content = unit_path.read_text(encoding="utf-8")
            cmd = self.get_command()
            bin_path = Path(cmd[0])
            if not bin_path.exists():
                return True
            cmd_str = subprocess.list2cmdline(cmd)
            if (
                f"ExecStart={cmd_str}" not in content
                and f"ExecStart={' '.join(cmd)}" not in content
            ):
                return True
        except Exception:
            return True
        return False

    def install(self) -> None:
        unit_path = self._unit_path()
        unit_path.parent.mkdir(parents=True, exist_ok=True)
        log_path = self.get_log_path()
        cmd_str = subprocess.list2cmdline(self.get_command())
        env_lines = ""
        if "XDG_CONFIG_HOME" in os.environ:
            env_lines = f"Environment=XDG_CONFIG_HOME={os.environ['XDG_CONFIG_HOME']}\n"

        content = f"""[Unit]
Description=Aikito Remote Sync for {self.workspace}
After=network.target

[Service]
Type=simple
WorkingDirectory={self.workspace}
ExecStart={cmd_str}
{env_lines}Restart=on-failure
RestartSec=10
RestartPreventExitStatus=75 78
StandardOutput=append:{log_path}
StandardError=append:{log_path}

[Install]
WantedBy=default.target
"""
        unit_path.write_text(content, encoding="utf-8")
        res_reload = subprocess.run(
            ["systemctl", "--user", "daemon-reload"],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_reload.returncode != 0:
            raise RuntimeError(
                f"Failed to reload systemd user daemon: {res_reload.stderr.strip() or res_reload.stdout.strip()}"
            )
        res_enable = subprocess.run(
            ["systemctl", "--user", "enable", self.get_service_name()],
            capture_output=True,
            text=True,
            check=False,
        )
        if res_enable.returncode != 0:
            raise RuntimeError(
                f"Failed to enable systemd service {self.get_service_name()}: {res_enable.stderr.strip() or res_enable.stdout.strip()}"
            )

    def uninstall(self) -> None:
        subprocess.run(
            ["systemctl", "--user", "disable", "--now", self.get_service_name()],
            capture_output=True,
            check=False,
        )
        unit_path = self._unit_path()
        if unit_path.exists():
            try:
                unit_path.unlink()
            except OSError:
                pass
            subprocess.run(
                ["systemctl", "--user", "daemon-reload"],
                capture_output=True,
                check=False,
            )

    def start_service(self) -> None:
        if not self.is_installed() or self.is_stale():
            self.install()
        res = subprocess.run(
            ["systemctl", "--user", "start", self.get_service_name()],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to start systemd service {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
            )

    def stop_service(self) -> None:
        subprocess.run(
            ["systemctl", "--user", "stop", self.get_service_name()],
            capture_output=True,
            check=False,
        )


class TaskSchedulerServiceManager(BaseServiceManager):
    """Windows Task Scheduler service manager via schtasks."""

    def manager_name(self) -> str:
        return "task_scheduler"

    def get_service_name(self) -> str:
        return f"AikitoRemoteSync_{self.slug}"

    def is_installed(self) -> bool:
        res = subprocess.run(
            ["schtasks", "/query", "/tn", self.get_service_name()],
            capture_output=True,
            check=False,
        )
        return res.returncode == 0

    def is_stale(self) -> bool:
        cmd = self.get_command()
        bin_path = Path(cmd[0])
        return not bin_path.exists()

    def install(self) -> None:
        log_path = self.get_log_path()
        aikito_cmd = subprocess.list2cmdline(self.get_command())
        cmd = f'cmd.exe /c "{aikito_cmd} >> "{log_path}" 2>&1"'
        res = subprocess.run(
            [
                "schtasks",
                "/create",
                "/tn",
                self.get_service_name(),
                "/tr",
                cmd,
                "/sc",
                "onlogon",
                "/f",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to create scheduled task {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
            )

    def uninstall(self) -> None:
        subprocess.run(
            ["schtasks", "/delete", "/tn", self.get_service_name(), "/f"],
            capture_output=True,
            check=False,
        )

    def start_service(self) -> None:
        if not self.is_installed() or self.is_stale():
            self.install()
        res = subprocess.run(
            ["schtasks", "/run", "/tn", self.get_service_name()],
            capture_output=True,
            text=True,
            check=False,
        )
        if res.returncode != 0:
            raise RuntimeError(
                f"Failed to run scheduled task {self.get_service_name()}: {res.stderr.strip() or res.stdout.strip()}"
            )

    def stop_service(self) -> None:
        # BaseServiceManager.stop signals graceful termination via WORKER_STOP_STATE.
        pass


class ProcessServiceManager(BaseServiceManager):
    """Universal cross-platform detached process manager."""

    def manager_name(self) -> str:
        return "process"

    def get_service_name(self) -> str:
        return f"process-{self.slug}"

    def is_installed(self) -> bool:
        return True

    def is_stale(self) -> bool:
        return False

    def install(self) -> None:
        pass

    def uninstall(self) -> None:
        self.stop()

    def start_service(self) -> None:
        log_path = self.get_log_path()
        log_file = open(log_path, "a", encoding="utf-8")

        args = self.get_command()

        if not is_windows():
            subprocess.Popen(
                args,
                stdout=log_file,
                stderr=log_file,
                cwd=str(self.workspace),
                start_new_session=True,
            )
        else:
            detached_flags = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
            ) | getattr(subprocess, "DETACHED_PROCESS", 0x8)
            subprocess.Popen(
                args,
                stdout=log_file,
                stderr=log_file,
                cwd=str(self.workspace),
                creationflags=detached_flags,
            )

    def stop_service(self) -> None:
        # stop() in BaseServiceManager signals the worker PID directly
        pass


def get_service_manager(
    workspace: Path,
    *,
    home: Path | None = None,
    mode: str = "auto",
) -> BaseServiceManager:
    """Return appropriate service manager for workspace and operating system."""
    if mode == "process":
        return ProcessServiceManager(workspace, home=home)

    if mode in ("auto", "system"):
        if sys.platform == "darwin":
            if shutil.which("launchctl") is not None:
                return LaunchdServiceManager(workspace, home=home)
        elif sys.platform.startswith("linux"):
            if shutil.which("systemctl") is not None:
                # Verify systemd user session is available
                try:
                    res = subprocess.run(
                        ["systemctl", "--user", "is-system-running"],
                        capture_output=True,
                        check=False,
                    )
                    if res.returncode in (0, 1):  # running or degraded
                        return SystemdServiceManager(workspace, home=home)
                except Exception:
                    pass
        elif is_windows():
            if shutil.which("schtasks") is not None:
                return TaskSchedulerServiceManager(workspace, home=home)

        if mode == "system":
            raise RuntimeError(
                f"Native system service manager is unavailable on {sys.platform}"
            )

    return ProcessServiceManager(workspace, home=home)
