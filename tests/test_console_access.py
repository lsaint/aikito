"""Online choices require a fresh, exact local confirmation before key access."""

import copy
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito import console_access as access
from aikito.workspace.encrypted_remote import EncryptedRemoteError
from aikito.workspace.remote_binding import RemoteAuth, RemoteBinding


@pytest.fixture
def console(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    project = ws / "projects/demo"
    project.mkdir(parents=True)
    (project / "agent.toml").write_text("")
    home = tmp_path / "home"
    home.mkdir()
    binding = RemoteBinding(
        1,
        "https://remote.example/v1/stores/main/remote",
        "a" * 32,
        RemoteAuth("bearer_env", "TEST_TOKEN"),
    )
    monkeypatch.setattr(
        access, "target", lambda _: (binding, "https://remote.example", "main")
    )
    monkeypatch.setattr(access, "publish_catalog", Mock())
    monkeypatch.setattr(access, "_open_remote", lambda *a, **kw: object())
    monkeypatch.setattr(
        access, "BoundRemoteReader", lambda *a: SimpleNamespace(read=lambda: object())
    )
    monkeypatch.setattr(access, "DeviceKeyStore", lambda **kw: object())
    monkeypatch.setattr(
        access,
        "unlock_encrypted_snapshot",
        lambda *a: SimpleNamespace(vault_id="b" * 32),
    )
    auth = {
        "version": 2,
        "account_id": "account-1",
        "store_id": "main",
        "sync_id": "a" * 32,
        "vault_id": "b" * 32,
        "collection_id": "project/demo/memory",
        "permissions": ["read"],
        "recipient_key_id": "recipient",
        "grant_id": "c" * 64,
        "nonce": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "expires_at": int(time.time()) + 86400,
    }
    request = {
        "request_id": "d" * 64,
        "account_id": "account-1",
        "username": "alice",
        "store_id": "main",
        "sync_id": "a" * 32,
        "vault_id": "b" * 32,
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 900,
        "cancelled": False,
        "items": [{"authorization": auth, "status": "pending"}],
    }
    monkeypatch.setattr(
        access, "exchange", lambda *a, **kw: {"requests": [copy.deepcopy(request)]}
    )
    fetch, export, submit = Mock(), Mock(), Mock(return_value={"status": "active"})
    monkeypatch.setattr(access, "fetch_recipient", fetch)
    monkeypatch.setattr(access, "export_bound_scoped_keys", export)
    monkeypatch.setattr(access, "submit_bound_export", submit)
    return access.ConsoleAccess(ws, home), request, fetch, export, submit


def confirm(client, request):
    shown = client.pending()["requests"][0]
    return client.perform(
        request["request_id"], "confirm", {"confirmation": shown["confirmation"]}
    )


def test_polling_does_not_export_keys(console):
    client, request, fetch, export, submit = console
    shown = client.pending()["requests"][0]
    assert shown["recipient"] == "https://remote.example"
    assert shown["account_id"] == request["account_id"]
    fetch.assert_not_called()
    export.assert_not_called()
    submit.assert_not_called()


def test_confirm_binds_exact_local_request(console):
    client, request, _fetch, export, submit = console
    shown = client.pending()["requests"][0]
    with pytest.raises(access.ConsoleAccessError):
        client.perform(request["request_id"], "confirm", {"confirmation": "wrong"})
    export.assert_not_called()
    result = client.perform(
        request["request_id"], "confirm", {"confirmation": shown["confirmation"]}
    )
    assert result["status"] == "active"
    export.assert_called_once()
    submit.assert_called_once()
    assert export.call_args.kwargs["collection_id"] == "project/demo/memory"
    assert export.call_args.kwargs["permission"] == "read"
    assert client.pending()["requests"] == []


def test_changed_or_cancelled_request_is_not_exported(console):
    client, request, fetch, export, _submit = console
    shown = client.pending()["requests"][0]
    request["items"][0]["authorization"]["permissions"] = ["read", "write"]
    with pytest.raises(access.ConsoleAccessError):
        client.perform(
            request["request_id"], "confirm", {"confirmation": shown["confirmation"]}
        )
    fetch.assert_not_called()
    export.assert_not_called()


def test_unknown_submission_cannot_replay_after_restart(console):
    client, request, _fetch, _export, submit = console
    submit.side_effect = RuntimeError("fixture unknown commit")
    with pytest.raises(RuntimeError):
        confirm(client, request)
    restarted = access.ConsoleAccess(client.workspace, client.home)
    assert restarted.pending()["requests"] == []
    assert submit.call_count == 1


def test_reject_never_reads_keys(console, monkeypatch):
    client, request, fetch, export, submit = console
    shown = client.pending()["requests"][0]
    exchange = Mock(return_value={"requests": [copy.deepcopy(request)]})
    monkeypatch.setattr(access, "exchange", exchange)
    client.perform(
        request["request_id"], "reject", {"confirmation": shown["confirmation"]}
    )
    assert exchange.call_args.args[2].endswith("/reject")
    assert exchange.call_args.kwargs["post"] is True
    fetch.assert_not_called()
    export.assert_not_called()
    submit.assert_not_called()


@pytest.mark.parametrize(
    "collection", ["private", "mcps", "agents", "project/demo/skills"]
)
def test_private_collections_never_reach_confirmation(console, collection):
    client, request, _fetch, export, _submit = console
    request["items"][0]["authorization"]["collection_id"] = collection
    with pytest.raises((ValueError, EncryptedRemoteError)):
        client.pending()
    export.assert_not_called()


def test_partially_processed_groups_are_not_automatically_resumed(console):
    client, request, _fetch, export, _submit = console
    request["items"][0]["status"] = "processing"
    assert client.pending()["requests"] == []
    export.assert_not_called()


def test_active_grants_are_returned_and_validated(console, monkeypatch):
    client, _request, _fetch, _export, _submit = console
    grant = {
        "grant_id": "a" * 64,
        "account_id": "account-1",
        "username": "tester",
        "store_id": "main",
        "sync_id": "a" * 32,
        "collection_id": "global/memory",
        "permissions": ["read", "write"],
        "created_at": int(time.time()),
        "expires_at": int(time.time()) + 86400,
        "status": "active",
        "clients": ["aikito-chatgpt"],
    }
    monkeypatch.setattr(
        access,
        "exchange",
        lambda *a, **kw: {"requests": [], "grants": [grant]},
    )
    res = client.pending()
    assert res["requests"] == []
    assert len(res["grants"]) == 1
    assert res["grants"][0]["grant_id"] == "a" * 64
    assert res["grants"][0]["clients"] == ["aikito-chatgpt"]
    assert res["grants"][0]["permissions"] == ["read", "write"]
