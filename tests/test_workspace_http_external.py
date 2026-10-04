"""External contract startup and teardown cannot hang the test process."""

import shlex
import sys
import os
from pathlib import Path

import pytest
import workspace_http_remote
from workspace_http_remote import external_store, external_endpoint
from test_workspace_remote_receipts import request

from aikito.workspace.http_transport import HTTPTransport
from aikito.workspace.remote_access import RemoteAccessDenied
from aikito.workspace.serialized_remote import SerializedRemoteStore


def command(*args):
    return shlex.join([sys.executable, *args])


def test_external_server_round_trip_with_spaced_root(tmp_path, monkeypatch):
    server = Path(__file__).resolve().with_name("http_remote_server.py")
    monkeypatch.setenv(
        "AIKITO_REMOTE_TEST_SERVER_CMD", command(str(server), "--root", "{root}")
    )
    with external_store(tmp_path / "root with spaces") as remote:
        assert remote.read().revision == 0
        assert remote.recover() is False


def test_external_auth_contract_rejects_without_mutation(tmp_path, monkeypatch):
    template = os.environ.get("AIKITO_REMOTE_TEST_SERVER_CMD")
    if template and "{token}" not in template:
        pytest.skip("External auth contract requires {token} in the command")
    if not template:
        server = Path(__file__).resolve().with_name("http_remote_server.py")
        monkeypatch.setenv(
            "AIKITO_REMOTE_TEST_SERVER_CMD",
            command(str(server), "--root", "{root}", "--token", "{token}"),
        )
    token = "external-auth-test-token"
    with external_endpoint(tmp_path / "root with spaces", token=token) as endpoint:
        valid = SerializedRemoteStore(
            HTTPTransport(endpoint, authorization="Bearer " + token).exchange
        )
        snapshot = valid.read()
        commit = request(valid)
        for authorization, status in [(None, 401), ("Bearer wrong", 403)]:
            invalid = SerializedRemoteStore(
                HTTPTransport(endpoint, authorization=authorization).exchange
            )
            with pytest.raises(RemoteAccessDenied) as caught:
                invalid.commit(commit)
            assert caught.value.status == status
            assert valid.read() == snapshot
            assert (
                valid.resolve_commit(
                    commit.expected.sync_id,
                    commit.client_id,
                    commit.request_id,
                    commit.mutation_digest,
                )
                is None
            )
        valid.commit(commit)
        assert valid.read().revision == snapshot.revision + 1


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
