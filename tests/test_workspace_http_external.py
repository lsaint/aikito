"""External contract startup and teardown cannot hang the test process."""

import shlex
import sys
from pathlib import Path

import pytest
import workspace_http_remote
from workspace_http_remote import external_store


def command(*args):
    return shlex.join([sys.executable, *args])


def test_external_server_round_trip_with_spaced_root(tmp_path, monkeypatch):
    server = Path(__file__).with_name("http_remote_server.py")
    monkeypatch.setenv(
        "AIKITO_REMOTE_TEST_SERVER_CMD", command(str(server), "--root", "{root}")
    )
    with external_store(tmp_path / "root with spaces") as remote:
        assert remote.read().revision == 0
        assert remote.recover() is False


@pytest.mark.parametrize("source", ["pass", "print('invalid endpoint', flush=True)"])
def test_bad_startup_is_reaped(tmp_path, monkeypatch, source):
    monkeypatch.setenv("AIKITO_REMOTE_TEST_SERVER_CMD", command("-c", source, "{root}"))
    with pytest.raises((ValueError, RuntimeError)):
        with external_store(tmp_path / "root", startup_timeout=0.5):
            raise AssertionError("Invalid external server started")


def test_startup_timeout_reaps_process(tmp_path, monkeypatch):
    started = []
    original = workspace_http_remote.subprocess.Popen

    def spawn(*args, **kwargs):
        process = original(*args, **kwargs)
        started.append(process)
        return process

    monkeypatch.setattr(workspace_http_remote.subprocess, "Popen", spawn)
    monkeypatch.setenv(
        "AIKITO_REMOTE_TEST_SERVER_CMD",
        command("-c", "import time; time.sleep(30)", "{root}"),
    )
    with pytest.raises(RuntimeError, match="announce"):
        with external_store(tmp_path / "root", startup_timeout=1):
            raise AssertionError("Silent server started")
    assert len(started) == 1
    assert started[0].poll() is not None
