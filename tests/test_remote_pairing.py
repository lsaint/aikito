"""Browser pairing validates the service boundary before persisting credentials."""

import hashlib
from unittest.mock import patch

import pytest

from aikito.cli_parser import build_parser
from aikito.workspace.remote_access import RemoteBindingError
from aikito.workspace.remote_factory import bind_remote

from aikito.workspace.remote_binding import (
    RemoteAuth,
    RemoteBinding,
    load_remote_binding,
    refresh_remote_binding_auth,
    save_remote_binding,
)
from aikito.workspace.resource_state import REMOTE_BINDING_STATE
from aikito.workspace.remote_store import StoreIdentityMismatch
from aikito.workspace.remote_pairing import _service_origin, pair_device

from workspace_memory_remote import InMemoryRemote


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://user:pass@example.com",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com#",
        "https://example.com:0",
        "https://[",
    ],
)
def test_invalid_service(origin):
    with pytest.raises(RemoteBindingError):
        _service_origin(origin)


def test_browser_pairing_persists_only_after_valid_approval(tmp_path):
    request = "a" * 64
    started = {
        "request": request,
        "verification_uri": "https://example.com/sync/connect?request=" + request,
        "code": request[:8],
        "expires_in": 300,
    }
    secret = "d" * 64
    with (
        patch(
            "aikito.workspace.remote_pairing._request",
            side_effect=[
                (201, started),
                (202, {"status": "pending"}),
                (
                    200,
                    {
                        "endpoint": "https://example.com/v1/stores/auto/remote",
                        "account_id": "alice",
                    },
                ),
            ],
        ) as call,
        patch("aikito.workspace.remote_pairing.secrets.token_hex", return_value=secret),
        patch("aikito.workspace.remote_pairing.time.sleep"),
        patch("aikito.workspace.remote_pairing.webbrowser.open") as browser,
        patch("aikito.workspace.remote_pairing.save_credential") as save,
    ):
        endpoint, key = pair_device("https://example.com/", home=tmp_path)
    assert endpoint == "https://example.com/v1/stores/auto/remote"
    assert key.startswith("AIKITO_REMOTE_DEVICE_")
    assert call.call_args_list[0].args[2] == {
        "challenge": hashlib.sha256(secret.encode()).hexdigest()
    }
    save.assert_called_once_with(key, secret, home=tmp_path)
    browser.assert_called_once_with(started["verification_uri"])


def test_cross_origin_response_never_opens_browser_or_saves_credentials(tmp_path):
    started = {
        "request": "a" * 64,
        "verification_uri": "https://evil.invalid/",
        "code": "aaaaaaaa",
        "expires_in": 300,
    }
    with (
        patch("aikito.workspace.remote_pairing._request", return_value=(201, started)),
        patch("aikito.workspace.remote_pairing.webbrowser.open") as browser,
        patch("aikito.workspace.remote_pairing.save_credential") as save,
        pytest.raises(RemoteBindingError),
    ):
        pair_device("https://example.com", home=tmp_path)
    browser.assert_not_called()
    save.assert_not_called()


def test_cross_origin_endpoint_never_saves_credentials(tmp_path):
    request = "a" * 64
    with (
        patch(
            "aikito.workspace.remote_pairing._request",
            side_effect=[
                (
                    201,
                    {
                        "request": request,
                        "verification_uri": "https://example.com/sync/connect?request="
                        + request,
                        "code": request[:8],
                        "expires_in": 300,
                    },
                ),
                (
                    200,
                    {
                        "endpoint": "https://evil.invalid/v1/stores/auto/remote",
                        "account_id": "alice",
                    },
                ),
            ],
        ),
        patch("aikito.workspace.remote_pairing.time.sleep"),
        patch("aikito.workspace.remote_pairing.webbrowser.open"),
        patch("aikito.workspace.remote_pairing.save_credential") as save,
        pytest.raises(RemoteBindingError),
    ):
        pair_device("https://example.com", home=tmp_path)
    save.assert_not_called()


def test_bind_parser_accepts_browser_and_custom_endpoint_forms():
    parser = build_parser()
    browser = parser.parse_args(["remote", "bind", "--service", "https://example.com"])
    assert browser.endpoint is None
    assert browser.service == "https://example.com"
    custom = parser.parse_args(
        ["remote", "bind", "https://example.com/v1/stores/a/remote"]
    )
    assert custom.endpoint.endswith("/remote")


def test_reauthentication_preserves_pending_journal(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    old = RemoteBinding(
        1,
        "https://example.com/v1/stores/a/remote",
        "a" * 32,
        RemoteAuth("bearer_env", "OLD_DEVICE"),
    )
    save_remote_binding(workspace, old, home=home)
    pending = (workspace / REMOTE_BINDING_STATE).with_name("pending.json")
    pending.write_bytes(b"opaque pending commit bytes")
    new = RemoteBinding(
        1, old.endpoint, old.sync_id, RemoteAuth("bearer_env", "NEW_DEVICE")
    )
    refresh_remote_binding_auth(workspace, new, home=home)
    assert load_remote_binding(workspace).auth.env == "NEW_DEVICE"
    assert pending.read_bytes() == b"opaque pending commit bytes"


def test_rebind_same_space_preserves_pending_and_rejects_identity_change(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    home = tmp_path / "home"
    old = RemoteBinding(
        1,
        "https://example.com/v1/stores/a/remote",
        "a" * 32,
        RemoteAuth("bearer_env", "OLD_DEVICE"),
    )
    save_remote_binding(workspace, old, home=home)
    pending = (workspace / REMOTE_BINDING_STATE).with_name("pending.json")
    pending.write_bytes(b"opaque pending commit bytes")
    backend = InMemoryRemote(old.sync_id)
    new_auth = RemoteAuth("bearer_env", "NEW_DEVICE")
    with patch("aikito.workspace.remote_factory._open_remote", return_value=backend):
        bind_remote(workspace, old.endpoint, new_auth, rebind=True, home=home)
    assert load_remote_binding(workspace).auth == new_auth
    assert pending.read_bytes() == b"opaque pending commit bytes"
    with (
        patch(
            "aikito.workspace.remote_factory._open_remote",
            return_value=InMemoryRemote("b" * 32),
        ),
        pytest.raises(StoreIdentityMismatch),
    ):
        bind_remote(workspace, old.endpoint, new_auth, rebind=True, home=home)
    assert load_remote_binding(workspace).sync_id == old.sync_id
    assert pending.read_bytes() == b"opaque pending commit bytes"
