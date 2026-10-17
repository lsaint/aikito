"""Consent choices cannot be replaced by cross-window authorization data."""

import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito import console_access as access
from aikito.workspace.remote_binding import RemoteAuth, RemoteBinding


@pytest.fixture
def console(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    project = ws / "projects" / "demo"
    project.mkdir(parents=True)
    (project / "agent.toml").write_text("")
    binding = RemoteBinding(
        1,
        "https://remote.example/v1/stores/main/remote",
        "a" * 32,
        RemoteAuth("bearer_env", "TEST_TOKEN"),
    )
    monkeypatch.setattr(access, "load_remote_binding", lambda _: binding)
    monkeypatch.setattr(access, "_open_remote", lambda *args, **kwargs: object())
    monkeypatch.setattr(
        access,
        "BoundRemoteReader",
        lambda *args: SimpleNamespace(read=lambda: object()),
    )
    monkeypatch.setattr(access, "DeviceKeyStore", lambda **kwargs: object())
    monkeypatch.setattr(
        access,
        "unlock_encrypted_snapshot",
        lambda *args: SimpleNamespace(vault_id="b" * 32),
    )
    return access.ConsoleAccess(ws, tmp_path / "home")


def request(result):
    context = result["context"]
    return {
        "version": 1,
        "account_id": "account-1",
        "store_id": context["store_id"],
        "sync_id": context["sync_id"],
        "vault_id": context["vault_id"],
        "collection_id": context["collection_id"],
        "permissions": context["permissions"],
        "recipient_key_id": "recipient",
        "grant_id": "c" * 64,
        "nonce": "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        "expires_at": int(time.time()) + 86400,
    }


@pytest.mark.parametrize("permission", ["read", "write"])
def test_confirmed_scope_reuses_existing_export_and_submit(
    console, monkeypatch, permission
):
    result = console.begin("demo", "https://recipient.example", permission)
    identity = result["intent_id"]
    assert "token" not in result and "key" not in result["context"]
    console.perform(identity, "approve", {"account_id": "account-1"})
    fetch, export = Mock(), Mock()
    submit = Mock(return_value={"status": "active"})
    monkeypatch.setattr(access, "fetch_recipient", fetch)
    monkeypatch.setattr(access, "export_bound_scoped_keys", export)
    monkeypatch.setattr(access, "submit_bound_export", submit)
    assert console.perform(identity, "submit", request(result)) == {"status": "active"}
    saved = access.ConsoleAccess(console.workspace, console.home).history("demo")[
        "records"
    ]
    assert saved[0]["grants"][0]["permissions"] == result["context"]["permissions"]
    console.perform(identity, "observe", {"account_id": "account-1", "grants": []})
    assert console.history("demo")["records"][0]["grants"] == []
    assert export.call_args.kwargs["collection_id"] == "project/demo/memory"
    assert export.call_args.kwargs["permission"] == permission
    assert submit.call_args.kwargs["expected_account"] == "account-1"
    assert submit.call_args.kwargs["origin"] == "https://recipient.example"
    assert not export.call_args.kwargs["output"].parent.exists()
    with pytest.raises(access.ConsoleAccessError):
        console.perform(identity, "submit", request(result))
    assert submit.call_count == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "other"),
        ("collection_id", "project/other/memory"),
        ("sync_id", "d" * 32),
        ("vault_id", "d" * 32),
        ("store_id", "other"),
        ("permissions", ["read", "write"]),
    ],
)
def test_request_cannot_change_confirmed_selection(console, monkeypatch, field, value):
    result = console.begin("demo", "https://recipient.example", "read")
    console.perform(result["intent_id"], "approve", {"account_id": "account-1"})
    body = request(result)
    body[field] = value
    export = Mock()
    monkeypatch.setattr(access, "fetch_recipient", export)
    with pytest.raises(Exception):
        console.perform(result["intent_id"], "submit", body)
    export.assert_not_called()


def test_claim_needs_account_confirmation(console, monkeypatch):
    result = console.begin("demo", "https://recipient.example", "read")
    identity = result["intent_id"]
    claim = Mock(return_value={"account_id": "account-1"})
    monkeypatch.setattr(access, "claim_bound_store", claim)
    with pytest.raises(access.ConsoleAccessError):
        console.perform(identity, "claim", {})
    console.perform(identity, "approve", {"account_id": "account-1"})
    console.perform(identity, "claim", {"challenge": "public"})
    assert claim.call_args.args[2] == "account-1"


def test_expired_and_changed_binding_are_rejected(console, monkeypatch):
    result = console.begin("demo", "https://recipient.example", "read")
    identity = result["intent_id"]
    monkeypatch.setattr(access, "load_remote_binding", lambda _: None)
    with pytest.raises(access.ConsoleAccessError, match="binding changed"):
        console.perform(identity, "approve", {"account_id": "account-1"})
    console._intents[identity].expires = 0
    with pytest.raises(access.ConsoleAccessError, match="expired"):
        console.perform(identity, "approve", {"account_id": "account-1"})


def test_uncertain_submission_is_not_repeated(console, monkeypatch):
    result = console.begin("demo", "https://recipient.example", "read")
    identity = result["intent_id"]
    console.perform(identity, "approve", {"account_id": "account-1"})
    monkeypatch.setattr(access, "fetch_recipient", Mock())
    monkeypatch.setattr(access, "export_bound_scoped_keys", Mock())
    submit = Mock(side_effect=RuntimeError("uncertain"))
    monkeypatch.setattr(access, "submit_bound_export", submit)
    with pytest.raises(RuntimeError):
        console.perform(identity, "submit", request(result))
    with pytest.raises(access.ConsoleAccessError):
        console.perform(identity, "submit", request(result))
    assert submit.call_count == 1


def test_history_survives_restart_and_never_approves_access(console):
    result = console.begin("demo", "https://recipient.example", "write")
    grant = {
        **request(result),
        "status": "active",
        "access_token": "never-save-this-token",
    }
    console.perform(
        result["intent_id"], "observe", {"account_id": "account-1", "grants": [grant]}
    )
    restarted = access.ConsoleAccess(console.workspace, console.home)
    saved = restarted.history("demo")["records"]
    assert len(saved) == 1
    assert saved[0]["origin"] == "https://recipient.example"
    assert saved[0]["grants"][0]["permissions"] == ["read", "write"]
    assert saved[0]["grants"][0]["grant_id"] == "c" * 64
    state = console.workspace / access.console_access_history.HISTORY_STATE
    text = state.read_text()
    assert "access_token" not in text and "never-save-this-token" not in text
    assert "nonce" not in text and "csrf" not in text
    assert console._intents[result["intent_id"]].account is None
    with pytest.raises(access.ConsoleAccessError, match="Confirm"):
        console.perform(result["intent_id"], "submit", request(result))


def test_history_clears_revoked_status_and_isolates_bindings(console, monkeypatch):
    result = console.begin("demo", "https://recipient.example", "read")
    observe = lambda grants: console.perform(
        result["intent_id"], "observe", {"account_id": "account-1", "grants": grants}
    )
    observe([{**request(result), "status": "active"}])
    observe([])
    assert console.history("demo")["records"][0]["grants"] == []
    assert console.history("other")["records"] == []
    old = access.load_remote_binding(console.workspace)
    changed = RemoteBinding(1, old.endpoint, "d" * 32, old.auth)
    monkeypatch.setattr(access, "load_remote_binding", lambda _: changed)
    assert console.history("demo")["records"] == []
    with pytest.raises(access.ConsoleAccessError, match="binding changed"):
        observe([])


def test_history_rejects_other_projects_and_account_changes(console):
    result = console.begin("demo", "https://recipient.example", "read")
    grant = {
        **request(result),
        "status": "active",
        "collection_id": "project/other/memory",
    }
    with pytest.raises(access.ConsoleAccessError, match="does not match"):
        console.perform(
            result["intent_id"],
            "observe",
            {"account_id": "account-1", "grants": [grant]},
        )
    console.perform(result["intent_id"], "approve", {"account_id": "account-1"})
    with pytest.raises(access.ConsoleAccessError, match="account changed"):
        console.perform(
            result["intent_id"], "observe", {"account_id": "other", "grants": []}
        )
    assert console.history("demo")["records"] == []


def test_history_failure_does_not_reclassify_a_successful_grant(console, monkeypatch):
    result = console.begin("demo", "https://recipient.example", "read")
    identity = result["intent_id"]
    console.perform(identity, "approve", {"account_id": "account-1"})
    monkeypatch.setattr(access, "fetch_recipient", Mock())
    monkeypatch.setattr(access, "export_bound_scoped_keys", Mock())
    submit = Mock(return_value={"status": "active"})
    monkeypatch.setattr(access, "submit_bound_export", submit)
    monkeypatch.setattr(
        access.console_access_history, "record", Mock(side_effect=OSError("fixture"))
    )
    assert console.perform(identity, "submit", request(result)) == {
        "status": "active",
        "history_saved": False,
    }
    with pytest.raises(access.ConsoleAccessError):
        console.perform(identity, "submit", request(result))
    assert submit.call_count == 1
