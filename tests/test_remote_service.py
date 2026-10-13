"""Unit tests for background service management, status, and secure credentials."""

from __future__ import annotations

import io
import json
import os
import plistlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from aikito.cli_parser import build_parser
from aikito.workspace.auto_sync import (
    AutoSyncLifecycleLock,
    is_worker_running,
    load_worker_status,
    save_worker_status,
)
from aikito.workspace.paths import persist_workspace
from aikito.workspace.remote_access import RemoteCredentialError
from aikito.workspace.remote_binding import RemoteAuth, RemoteBinding
from aikito.workspace.remote_credentials import (
    get_credentials_path,
    read_credentials_file,
    resolve_credential_with_source,
    save_credential,
)
from aikito.workspace.service import (
    LaunchdServiceManager,
    ProcessServiceManager,
    SystemdServiceManager,
    TaskSchedulerServiceManager,
    get_aikito_command,
    get_workspace_slug,
    is_windows,
)


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return home


@pytest.fixture
def fake_workspace(tmp_path: Path, fake_home: Path) -> Path:
    ws = tmp_path / "workspace"
    for directory in (
        "agents",
        "subagents",
        "mcps",
        "memory/notes",
        "projects/demo/memory/notes",
        "skills",
        "global",
    ):
        (ws / directory).mkdir(parents=True, exist_ok=True)
    (ws / "layout.toml").write_text("version = 2\n", encoding="utf-8")
    (ws / "skills.toml").write_text("skills = []\n", encoding="utf-8")
    (ws / "config.toml").write_text("[workspace]\nversion = 1\n", encoding="utf-8")
    persist_workspace(ws, fake_home)
    return ws


def test_credentials_storage_and_permissions(fake_home: Path) -> None:
    creds_path = get_credentials_path(fake_home)
    assert creds_path.name == "credentials"

    save_credential("MY_TEST_TOKEN", "secret-token-12345", home=fake_home)
    assert creds_path.exists()

    if sys.platform != "win32":
        st = creds_path.stat()
        assert st.st_mode & 0o077 == 0, f"Expected 0600 mode, got {oct(st.st_mode)}"

    data = read_credentials_file(creds_path)
    assert data.get("MY_TEST_TOKEN") == "secret-token-12345"

    # Insecure permission rejection on POSIX
    if sys.platform != "win32":
        os.chmod(creds_path, 0o644)
        with pytest.raises(RemoteCredentialError, match="Insecure permissions"):
            read_credentials_file(creds_path)
        # Restore secure permissions
        os.chmod(creds_path, 0o600)


def test_resolve_credential_priority(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth = RemoteAuth("bearer_env", "RESOLVE_TEST_TOKEN")

    # 1. Credentials file
    save_credential("RESOLVE_TEST_TOKEN", "from-file-token", home=fake_home)
    val, source = resolve_credential_with_source(auth, home=fake_home)
    assert val == "from-file-token"
    assert source == "credentials_file"

    # 2. Environment override
    monkeypatch.setenv("RESOLVE_TEST_TOKEN", "from-env-token")
    val, source = resolve_credential_with_source(auth, home=fake_home)
    assert val == "from-env-token"
    assert source == "environment"

    # 3. Explicit environ mapping override
    val, source = resolve_credential_with_source(
        auth,
        environ={"RESOLVE_TEST_TOKEN": "from-explicit-environ"},
        home=fake_home,
    )
    assert val == "from-explicit-environ"
    assert source == "explicit_environ"

    # 4. Missing credential
    auth_missing = RemoteAuth("bearer_env", "NON_EXISTENT_VAR")
    with pytest.raises(RemoteCredentialError, match="could not be resolved"):
        resolve_credential_with_source(auth_missing, home=fake_home)


def test_service_manager_slug_generation(tmp_path: Path) -> None:
    ws1 = tmp_path / "my_project"
    ws2 = tmp_path / "other" / "my_project"
    slug1 = get_workspace_slug(ws1)
    slug2 = get_workspace_slug(ws2)
    assert slug1.startswith("my_project-")
    assert slug2.startswith("my_project-")
    assert slug1 != slug2, "Distinct paths should yield distinct slugs"


def test_launchd_service_manager_generation(
    fake_workspace: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    mgr = LaunchdServiceManager(fake_workspace, home=fake_home)
    plist_path = mgr._plist_path()
    assert plist_path.parent == fake_home / "Library" / "LaunchAgents"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mgr.install()

    assert plist_path.exists()
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)

    assert data["Label"] == mgr.get_service_name()
    assert "--workspace" in data["ProgramArguments"]
    assert str(fake_workspace) in data["ProgramArguments"]
    assert data["WorkingDirectory"] == str(fake_workspace)
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] == {"Crashed": True}
    assert data["ThrottleInterval"] == 30
    # Ensure no token values or env vars are embedded when XDG_CONFIG_HOME is unset
    assert "EnvironmentVariables" not in data


def test_launchd_service_manager_with_xdg_config_home(
    fake_workspace: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    mgr = LaunchdServiceManager(fake_workspace, home=fake_home)
    plist_path = mgr._plist_path()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mgr.install()

    assert plist_path.exists()
    with open(plist_path, "rb") as f:
        data = plistlib.load(f)

    assert data["EnvironmentVariables"] == {"XDG_CONFIG_HOME": "/custom/xdg"}


def test_systemd_service_manager_generation(
    fake_workspace: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    mgr = SystemdServiceManager(fake_workspace, home=fake_home)
    unit_path = mgr._unit_path()
    assert unit_path.parent == fake_home / ".config" / "systemd" / "user"

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mgr.install()

    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert f"WorkingDirectory={fake_workspace}" in content
    assert f"--workspace {fake_workspace}" in content
    assert "Restart=on-failure" in content
    assert "RestartSec=10" in content
    assert "RestartPreventExitStatus=75 78" in content
    # Ensure no token values or env vars are embedded when XDG_CONFIG_HOME is unset
    assert "Environment=" not in content


def test_systemd_service_manager_with_xdg_config_home(
    fake_workspace: Path, fake_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", "/custom/xdg")
    mgr = SystemdServiceManager(fake_workspace, home=fake_home)
    unit_path = mgr._unit_path()

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mgr.install()

    assert unit_path.exists()
    content = unit_path.read_text(encoding="utf-8")
    assert "Environment=XDG_CONFIG_HOME=/custom/xdg" in content


def test_task_scheduler_service_manager_generation(
    fake_workspace: Path, fake_home: Path
) -> None:
    mgr = TaskSchedulerServiceManager(fake_workspace, home=fake_home)
    assert mgr.get_service_name().startswith("AikitoRemoteSync_")

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        mgr.install()
        schtasks_calls = [
            call for call in mock_run.call_args_list if "schtasks" in call[0][0]
        ]
        assert len(schtasks_calls) == 1
        args = schtasks_calls[0][0][0]
        assert "schtasks" in args
        assert "/create" in args
        assert mgr.get_service_name() in args
        tr_cmd = args[args.index("/tr") + 1]
        assert "cmd.exe" in tr_cmd
        assert "worker.log" in tr_cmd


def test_worker_status_persistence(fake_workspace: Path) -> None:
    status_data = {
        "state": "syncing",
        "pid": 99999,
        "last_checked": "2026-10-10T06:00:00Z",
        "last_success": "2026-10-10T05:59:00Z",
        "last_revision": 12,
        "last_error": None,
        "conflicts": 0,
        "pending": 0,
    }
    save_worker_status(fake_workspace, status_data)
    loaded = load_worker_status(fake_workspace)
    assert loaded == status_data


def test_is_worker_running_detection(fake_workspace: Path) -> None:
    running, pid = is_worker_running(fake_workspace)
    assert not running
    assert pid is None

    lock = AutoSyncLifecycleLock(fake_workspace)
    lock.acquire()
    try:
        running, pid = is_worker_running(fake_workspace)
        assert running
        assert pid == os.getpid()
    finally:
        lock.release()

    running, pid = is_worker_running(fake_workspace)
    assert not running


def test_cmd_remote_start_already_running(
    fake_workspace: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    # Set up remote binding
    from aikito.workspace.remote_binding import save_remote_binding

    binding = RemoteBinding(
        version=1,
        endpoint="https://127.0.0.1:8443/test",
        sync_id="a" * 32,
        auth=RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN"),
    )
    save_remote_binding(fake_workspace, binding, home=fake_home)

    lock = AutoSyncLifecycleLock(fake_workspace)
    lock.acquire()
    try:
        parser = build_parser()
        args = parser.parse_args(
            ["remote", "start", "--workspace", str(fake_workspace)]
        )
        args.func(args)
        captured = capsys.readouterr()
        assert "[INFO] Auto-sync service is already running" in captured.out
    finally:
        lock.release()


def test_cmd_remote_status_json_and_text(
    fake_workspace: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from aikito.workspace.remote_binding import save_remote_binding

    binding = RemoteBinding(
        version=1,
        endpoint="https://127.0.0.1:8443/test",
        sync_id="a" * 32,
        auth=RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN"),
    )
    save_remote_binding(fake_workspace, binding, home=fake_home)

    save_worker_status(
        fake_workspace,
        {
            "state": "idle",
            "last_checked": "2026-10-10T12:00:00Z",
            "last_success": "2026-10-10T11:59:00Z",
            "last_revision": 7,
            "last_error": None,
            "conflicts": 0,
            "pending": 0,
        },
    )

    parser = build_parser()

    # 1. Human-readable text status
    args_text = parser.parse_args(
        ["remote", "status", "--workspace", str(fake_workspace)]
    )
    args_text.func(args_text)
    captured = capsys.readouterr()
    assert "Aikito Remote Status:" in captured.out
    assert "https://127.0.0.1:8443/test" in captured.out
    assert "Last revision:   7" in captured.out
    assert "Sync ID:         aaaaaaaa...aaaa" in captured.out
    assert "Log file:" in captured.out
    assert "Recent activity:" in captured.out
    assert "(no recent activity logged)" in captured.out

    # 2. JSON status
    args_json = parser.parse_args(
        ["remote", "status", "--workspace", str(fake_workspace), "--json"]
    )
    args_json.func(args_json)
    captured = capsys.readouterr()
    data = json.loads(captured.out)
    assert data["binding"]["endpoint"] == "https://127.0.0.1:8443/test"
    assert data["binding"]["sync_id"] == "a" * 32
    assert data["last_revision"] == 7
    assert data["service"]["running"] is False


def test_cmd_remote_status_recent_activity_and_tail(
    fake_workspace: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from aikito.workspace.remote_binding import save_remote_binding
    from aikito.workspace.resource_state import WORKER_LOG_STATE, state_path

    binding = RemoteBinding(
        version=1,
        endpoint="https://127.0.0.1:8443/test",
        sync_id="a" * 32,
        auth=RemoteAuth("bearer_env", "AIKITO_REMOTE_TOKEN"),
    )
    save_remote_binding(fake_workspace, binding, home=fake_home)

    log_path = state_path(fake_workspace, WORKER_LOG_STATE, create=True)
    log_path.write_text(
        "2026-10-12 20:00:00 [SYNC] (rev 1) pull memory/note1.md\n"
        "2026-10-12 20:05:00 [SYNC] (rev 2) push memory/note2.md\n"
        "2026-10-12 20:10:00 [SYNC] (rev 3) push memory/note3.md\n",
        encoding="utf-8",
    )

    parser = build_parser()

    # Default tail shows recent activity and Log file
    args = parser.parse_args(["remote", "status", "--workspace", str(fake_workspace)])
    args.func(args)
    captured = capsys.readouterr()
    assert f"Log file: {log_path}" in captured.out
    assert "Recent activity:" in captured.out
    assert "memory/note1.md" in captured.out
    assert "memory/note2.md" in captured.out

    # Specific tail count (--tail 2) shows only last 2 lines
    args_tail2 = parser.parse_args(
        ["remote", "status", "--workspace", str(fake_workspace), "--tail", "2"]
    )
    args_tail2.func(args_tail2)
    captured2 = capsys.readouterr()
    assert "memory/note1.md" not in captured2.out
    assert "memory/note2.md" in captured2.out

    # --tail 0 hides Recent activity section
    args_tail0 = parser.parse_args(
        ["remote", "status", "--workspace", str(fake_workspace), "--tail", "0"]
    )
    args_tail0.func(args_tail0)
    captured0 = capsys.readouterr()
    assert f"Log file: {log_path}" in captured0.out
    assert "Recent activity:" not in captured0.out


def test_cmd_remote_credential_cli(
    fake_home: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("HOME", str(fake_home))
    parser = build_parser()

    # 1. path
    args_path = parser.parse_args(["remote", "credential", "path"])
    args_path.func(args_path)
    captured = capsys.readouterr()
    assert str(get_credentials_path(fake_home)) in captured.out

    # 2. set via --stdin
    monkeypatch.setattr("sys.stdin", io.StringIO("cli-secret-999\n"))
    args_set = parser.parse_args(
        ["remote", "credential", "set", "MY_CLI_TOKEN", "--stdin"]
    )
    args_set.func(args_set)
    captured = capsys.readouterr()
    assert "[SUCCESS] Saved credential 'MY_CLI_TOKEN'" in captured.out

    # 3. set via interactive getpass
    import getpass

    monkeypatch.setattr(getpass, "getpass", lambda prompt="": "interactive-secret-888")
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    args_set_prompt = parser.parse_args(
        ["remote", "credential", "set", "INTERACTIVE_TOKEN"]
    )
    args_set_prompt.func(args_set_prompt)
    captured = capsys.readouterr()
    assert "[SUCCESS] Saved credential 'INTERACTIVE_TOKEN'" in captured.out

    # 4. set with empty value fails
    monkeypatch.setattr("sys.stdin", io.StringIO("   \n"))
    args_empty = parser.parse_args(
        ["remote", "credential", "set", "EMPTY_TOKEN", "--stdin"]
    )
    with pytest.raises(SystemExit) as exc_info:
        args_empty.func(args_empty)
    assert exc_info.value.code == 1
    captured = capsys.readouterr()
    assert "Credential value cannot be empty" in captured.err

    # 5. list
    args_list = parser.parse_args(["remote", "credential", "list"])
    args_list.func(args_list)
    captured = capsys.readouterr()
    assert "MY_CLI_TOKEN: [configured]" in captured.out
    assert "INTERACTIVE_TOKEN: [configured]" in captured.out
    # Token value must not be displayed in list
    assert "cli-secret-999" not in captured.out
    assert "interactive-secret-888" not in captured.out

    # 6. set via --keychain
    with patch("aikito.remote_cli.save_keychain") as mock_save_kc:
        monkeypatch.setattr("sys.stdin", io.StringIO("keychain-secret-777\n"))
        args_kc = parser.parse_args(
            ["remote", "credential", "set", "KEYCHAIN_TOKEN", "--stdin", "--keychain"]
        )
        args_kc.func(args_kc)
        captured = capsys.readouterr()
        mock_save_kc.assert_called_once_with("KEYCHAIN_TOKEN", "keychain-secret-777")
        assert (
            "[SUCCESS] Saved credential 'KEYCHAIN_TOKEN' to operating system keychain"
            in captured.out
        )


def test_launchd_service_manager_load_and_start(
    fake_workspace: Path, fake_home: Path
) -> None:
    mgr = LaunchdServiceManager(fake_workspace, home=fake_home)
    plist_path = mgr._plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_data = {
        "Label": mgr.get_service_name(),
        "ProgramArguments": mgr.get_command(),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(plist_data, f)

    recorded_commands: list[list[str]] = []

    def mock_run(cmd, *args, **kwargs):
        recorded_commands.append(cmd)
        if cmd[:2] == ["launchctl", "list"]:
            # Initially not loaded
            return subprocess.CompletedProcess(args=cmd, returncode=1)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=mock_run):
        # 1. start_service when not loaded: must run load -w then start
        mgr.start_service()
        assert recorded_commands[0][:2] == ["launchctl", "list"]
        assert recorded_commands[1] == ["launchctl", "load", "-w", str(plist_path)]
        assert recorded_commands[2] == ["launchctl", "start", mgr.get_service_name()]

    # 2. start_service when already loaded: only runs start
    recorded_commands.clear()

    def mock_run_loaded(cmd, *args, **kwargs):
        recorded_commands.append(cmd)
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=mock_run_loaded):
        mgr.start_service()
        assert recorded_commands[0][:2] == ["launchctl", "list"]
        assert recorded_commands[1] == ["launchctl", "start", mgr.get_service_name()]

    # 3. stop_service unloads plist if loaded
    recorded_commands.clear()
    with patch("subprocess.run", side_effect=mock_run_loaded):
        mgr.stop_service()
        assert recorded_commands[0][:2] == ["launchctl", "list"]
        assert recorded_commands[1] == ["launchctl", "stop", mgr.get_service_name()]
        assert recorded_commands[2] == ["launchctl", "unload", "-w", str(plist_path)]


def test_launchd_is_stale_detection(fake_workspace: Path, fake_home: Path) -> None:
    mgr = LaunchdServiceManager(fake_workspace, home=fake_home)
    plist_path = mgr._plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)

    # 1. Stale when plist points to non-existent executable
    stale_data = {
        "Label": mgr.get_service_name(),
        "ProgramArguments": ["/non/existent/path/aikito", "remote", "watch"],
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(stale_data, f)
    assert mgr.is_stale() is True

    # 2. Fresh when plist points to current executable and matching args
    fresh_data = {
        "Label": mgr.get_service_name(),
        "ProgramArguments": mgr.get_command(),
    }
    with open(plist_path, "wb") as f:
        plistlib.dump(fresh_data, f)
    assert mgr.is_stale() is False


def test_service_manager_start_timeout_raises_with_log(
    fake_workspace: Path, fake_home: Path
) -> None:
    mgr = ProcessServiceManager(fake_workspace, home=fake_home)
    log_path = mgr.get_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text(
        "[ERROR] Remote credential in environment variable 'TOKEN' is missing or invalid.\n"
        "Failed to authenticate with remote store.",
        encoding="utf-8",
    )

    with patch.object(mgr, "start_service"):
        # Worker lock is never acquired, timeout should raise with log tail
        with pytest.raises(RuntimeError) as exc_info:
            mgr.start(timeout=0.2)
        err = str(exc_info.value)
        assert "Background auto-sync service failed to acquire worker lock" in err
        assert str(log_path) in err
        assert "Remote credential in environment variable" in err


def test_get_aikito_command_resolution(
    fake_workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from aikito import __version__

    bin_name = "aikito.cmd" if is_windows() else "aikito"
    fake_bin = fake_workspace / "bin" / bin_name
    fake_bin.parent.mkdir(parents=True, exist_ok=True)
    if is_windows():
        fake_bin.write_text(f"@echo off\necho aikito {__version__}\nexit /b 0\n")
    else:
        fake_bin.write_text(f'#!/bin/sh\necho "aikito {__version__}"\nexit 0\n')
        fake_bin.chmod(0o755)

    # 1. When sys.argv[0] is matching aikito binary
    monkeypatch.setattr("sys.argv", [str(fake_bin)])
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    cmd_argv0 = get_aikito_command(fake_workspace)
    assert cmd_argv0[0] == str(fake_bin)
    assert cmd_argv0[1:] == ["remote", "watch", "--workspace", str(fake_workspace)]

    # 2. When shutil.which finds matching aikito binary
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr(
        "shutil.which", lambda cmd: str(fake_bin) if cmd == "aikito" else None
    )
    cmd_which = get_aikito_command(fake_workspace)
    assert cmd_which[0] == str(fake_bin)
    assert cmd_which[1:] == ["remote", "watch", "--workspace", str(fake_workspace)]

    # 3. When which finds an outdated aikito binary
    old_bin_dir = fake_workspace / "old_bin"
    old_bin_dir.mkdir(parents=True, exist_ok=True)
    old_bin = old_bin_dir / bin_name
    if is_windows():
        old_bin.write_text("@echo off\necho aikito 0.1.0\nexit /b 0\n")
    else:
        old_bin.write_text('#!/bin/sh\necho "aikito 0.1.0"\nexit 0\n')
        old_bin.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr(
        "shutil.which", lambda cmd: str(old_bin) if cmd == "aikito" else None
    )
    cmd_outdated = get_aikito_command(fake_workspace)
    assert cmd_outdated[0] == sys.executable
    assert cmd_outdated[1:] == [
        "-m",
        "aikito",
        "remote",
        "watch",
        "--workspace",
        str(fake_workspace),
    ]

    # 4. When which finds a binary with substring version (e.g. 1.58.00 should not match 1.58.0)
    sub_bin_dir = fake_workspace / "sub_bin"
    sub_bin_dir.mkdir(parents=True, exist_ok=True)
    sub_bin = sub_bin_dir / bin_name
    if is_windows():
        sub_bin.write_text(f"@echo off\necho aikito {__version__}0\nexit /b 0\n")
    else:
        sub_bin.write_text(f'#!/bin/sh\necho "aikito {__version__}0"\nexit 0\n')
        sub_bin.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr(
        "shutil.which", lambda cmd: str(sub_bin) if cmd == "aikito" else None
    )
    cmd_sub = get_aikito_command(fake_workspace)
    assert cmd_sub[0] == sys.executable
    assert cmd_sub[1:] == [
        "-m",
        "aikito",
        "remote",
        "watch",
        "--workspace",
        str(fake_workspace),
    ]

    # 5. When which finds a hanging binary (timeout kill & reap)
    hang_dir = fake_workspace / "hang_dir"
    hang_dir.mkdir(parents=True, exist_ok=True)
    hang_bin = hang_dir / bin_name
    if is_windows():
        hang_bin.write_text("@echo off\ncall :loop\nexit /b 0\n:loop\ngoto loop\n")
    else:
        hang_bin.write_text("#!/bin/sh\nsleep 10\nexit 0\n")
        hang_bin.chmod(0o755)
    monkeypatch.setattr("sys.argv", ["pytest"])
    from aikito.workspace import service as svc_mod

    with patch(
        "subprocess.Popen.communicate",
        side_effect=subprocess.TimeoutExpired(cmd="aikito", timeout=0.01),
    ) as mock_comm, patch("subprocess.Popen.kill") as mock_kill:
        assert svc_mod._is_matching_aikito_bin(hang_bin) is False
        mock_kill.assert_called_once()
        assert mock_comm.call_count == 2

    # 6. When shutil.which returns None
    monkeypatch.setattr("sys.argv", ["pytest"])
    monkeypatch.setattr("shutil.which", lambda cmd: None)
    cmd_fallback = get_aikito_command(fake_workspace)
    assert cmd_fallback[0] == sys.executable
    assert cmd_fallback[1:] == [
        "-m",
        "aikito",
        "remote",
        "watch",
        "--workspace",
        str(fake_workspace),
    ]


def test_save_keychain_implementations(monkeypatch: pytest.MonkeyPatch) -> None:
    from aikito.workspace.remote_access import RemoteCredentialError
    from aikito.workspace.remote_credentials import save_keychain

    # Test darwin
    monkeypatch.setattr(sys, "platform", "darwin")
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        save_keychain("MY_TEST_KEY", "secret-value-123")
        cmd = mock_run.call_args[0][0]
        assert "security" in cmd[0]
        assert "add-generic-password" in cmd
        assert "MY_TEST_KEY" in cmd
        assert "secret-value-123" in cmd

    # Test linux
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr("shutil.which", lambda cmd: "/usr/bin/secret-tool")
    with patch.object(subprocess, "run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        save_keychain("MY_TEST_KEY", "secret-value-123")
        cmd = mock_run.call_args[0][0]
        assert "secret-tool" in cmd[0]
        assert "store" in cmd

    # Test windows raises unsupported error
    monkeypatch.setattr("aikito.workspace.remote_credentials.is_windows", lambda: True)
    with pytest.raises(
        RemoteCredentialError, match="OS keychain is not supported on Windows"
    ):
        save_keychain("MY_TEST_KEY", "secret-value-123")


def test_watch_workspace_remote_missing_credentials(
    fake_workspace: Path, fake_home: Path
) -> None:
    from aikito.remote_service import watch_workspace_remote
    from aikito.workspace.remote_access import RemoteCredentialError
    from aikito.workspace.remote_binding import (
        RemoteAuth,
        RemoteBinding,
        save_remote_binding,
    )

    auth = RemoteAuth("bearer_env", "MISSING_ENV_VAR")
    save_remote_binding(
        fake_workspace,
        RemoteBinding(1, "https://example.com/v1/remote", "a" * 32, auth),
    )

    # once=True raises RemoteCredentialError
    with pytest.raises(RemoteCredentialError):
        watch_workspace_remote(fake_workspace, home=fake_home, once=True)

    # once=False passes remote=None to AutoSyncScheduler without NameError
    with patch("aikito.remote_service.AutoSyncScheduler.run") as mock_run:
        watch_workspace_remote(fake_workspace, home=fake_home, once=False)
        mock_run.assert_called_once_with(once=False)


def test_resolve_credential_fallback_to_standard_config(fake_home: Path) -> None:
    auth = RemoteAuth("bearer_env", "FALLBACK_TEST_TOKEN")
    # Save credential in standard ~/.config/aikito/credentials
    std_creds_file = fake_home / ".config" / "aikito" / "credentials"
    save_credential("FALLBACK_TEST_TOKEN", "fallback-secret-999", path=std_creds_file)

    # Resolve with a custom credentials_path that does not exist
    custom_missing = fake_home / "custom_config" / "credentials"
    val, source = resolve_credential_with_source(
        auth, home=fake_home, credentials_path=custom_missing
    )
    assert val == "fallback-secret-999"
    assert source == "credentials_file"


def test_stop_workspace_remote_service_uninstalls_autostart(
    fake_workspace: Path, fake_home: Path
) -> None:
    from aikito.remote_service import stop_workspace_remote_service

    mgr = LaunchdServiceManager(fake_workspace, home=fake_home)
    plist_path = mgr._plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_bytes(b"<plist></plist>")
    assert mgr.is_installed() is True

    with (
        patch("aikito.remote_service.get_service_manager", return_value=mgr),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0)
        stop_workspace_remote_service(fake_workspace, home=fake_home, mode="auto")
        assert not mgr.is_installed()


def test_cmd_remote_disable_alias(fake_workspace: Path) -> None:
    parser = build_parser()
    args = parser.parse_args(["remote", "disable", "--workspace", str(fake_workspace)])
    from aikito.remote_cli import cmd_remote_stop

    assert args.func == cmd_remote_stop


def test_worker_log_rotation(fake_workspace: Path, fake_home: Path) -> None:
    from aikito.workspace.auto_sync import AutoSyncScheduler
    from aikito.workspace.resource_state import WORKER_LOG_STATE, state_path

    scheduler = AutoSyncScheduler(fake_workspace, None, fake_home)
    log_path = state_path(fake_workspace, WORKER_LOG_STATE, create=True)
    log_path.write_text("x" * 100, encoding="utf-8")

    # Rotate when max_bytes is exceeded
    scheduler._rotate_log(max_bytes=50)
    backup_path = log_path.with_name(log_path.name + ".1")
    assert backup_path.exists()
    assert backup_path.read_text(encoding="utf-8") == "x" * 100


def test_restart_workspace_remote_service_lifecycle(
    fake_workspace: Path, fake_home: Path
) -> None:
    from aikito.remote_service import restart_workspace_remote_service
    from aikito.workspace.remote_binding import (
        RemoteAuth,
        RemoteBinding,
        save_remote_binding,
    )

    binding = RemoteBinding(
        version=1,
        endpoint="https://example.com/sync",
        sync_id="a" * 32,
        auth=RemoteAuth("bearer_env", "RESTART_TEST_TOKEN"),
    )
    save_remote_binding(fake_workspace, binding, home=fake_home)

    class MockManager:
        def __init__(self) -> None:
            self.stop_called = False
            self.start_called = False
            self.install_called = False
            self._running = False

        def manager_name(self) -> str:
            return "mock"

        def get_service_name(self) -> str:
            return "mock-service"

        def get_log_path(self) -> Path:
            return fake_workspace / "worker.log"

        def is_running(self) -> tuple[bool, int | None]:
            return (True, 99999) if self.start_called else (False, None)

        def is_installed(self) -> bool:
            return True

        def is_stale(self) -> bool:
            return True

        def install(self) -> None:
            self.install_called = True

        def stop(self, timeout: float = 35.0) -> None:
            self.stop_called = True

        def start(self, timeout: float = 5.0) -> None:
            self.start_called = True

    mgr = MockManager()
    with patch("aikito.remote_service.get_service_manager", return_value=mgr):
        res = restart_workspace_remote_service(fake_workspace, home=fake_home)
        assert mgr.stop_called is True
        assert mgr.install_called is True
        assert mgr.start_called is True
        assert res["pid"] == 99999
        assert res["service_name"] == "mock-service"
        assert res["is_running"] is True


def test_cmd_remote_restart_cli(
    fake_workspace: Path, fake_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from aikito.remote_cli import cmd_remote_restart
    from aikito.workspace.remote_binding import (
        RemoteAuth,
        RemoteBinding,
        save_remote_binding,
    )

    binding = RemoteBinding(
        version=1,
        endpoint="https://example.com/sync",
        sync_id="a" * 32,
        auth=RemoteAuth("bearer_env", "RESTART_CLI_TOKEN"),
    )
    save_remote_binding(fake_workspace, binding, home=fake_home)

    parser = build_parser()
    args = parser.parse_args(["remote", "restart", "--workspace", str(fake_workspace)])
    assert args.func == cmd_remote_restart

    mock_res = {
        "was_running": True,
        "old_pid": 1111,
        "pid": 2222,
        "service_name": "com.aikito.remote.test",
        "manager_name": "launchd",
        "log_path": "/path/to/worker.log",
        "is_running": True,
    }

    with patch(
        "aikito.remote_cli.restart_workspace_remote_service", return_value=mock_res
    ):
        cmd_remote_restart(args)
        out = capsys.readouterr().out
        assert "[SUCCESS] Restarted background auto-sync service" in out
        assert "PID: 2222" in out
        assert "/path/to/worker.log" in out
