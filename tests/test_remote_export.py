"""Independent account/scope choices, pin continuity and one-shot submission."""

import base64
import hashlib
import time
from email.message import Message
from unittest.mock import Mock

import pytest

from aikito.workspace import remote_export as submit
from aikito.workspace.remote_binding import RemoteAuth, RemoteBinding
from aikito.workspace.remote_wire import _canonical


@pytest.fixture
def export_input(tmp_path, monkeypatch):
    public = bytes(range(32))
    recipient = {
        "version": 1,
        "algorithm": submit.EXPORT_SUITE,
        "key_id": "recipient-1",
        "public_key": base64.b64encode(public).decode(),
    }
    auth = {
        "version": 1,
        "account_id": "oidc:" + "a" * 64,
        "store_id": "alpha",
        "sync_id": "b" * 32,
        "vault_id": "00112233445566778899aabbccddeeff",
        "collection_id": "project/demo/memory",
        "recipient_key_id": "recipient-1",
        "permissions": ["read"],
        "grant_id": "a" * 64,
        "nonce": base64.b64encode(b"n" * 32).decode(),
        "expires_at": int(time.time()) + 240,
    }
    context = {k: v for k, v in auth.items() if k != "collection_id"}
    context.update(
        algorithm=submit.EXPORT_SUITE,
        key_version=1,
        collection_tag="1" * 32,
        parent_project_id="e1:" + "2" * 32 + ":" + "3" * 64,
        recipient_public_key_sha256=hashlib.sha256(public).hexdigest(),
    )
    packet = {
        "version": 1,
        "context": context,
        "ciphertext": base64.b64encode(b"x" * 48).decode(),
    }
    authorization, package = tmp_path / "auth.json", tmp_path / "sealed.json"
    authorization.write_bytes(_canonical(auth))
    package.write_bytes(_canonical(packet))
    binding = RemoteBinding(
        1,
        "https://store.example/v1/stores/alpha/remote",
        "b" * 32,
        RemoteAuth("bearer_env", "TEST_TOKEN"),
    )
    monkeypatch.setattr(submit, "load_remote_binding", lambda _: binding)
    credential = Mock(return_value="sensitive-fixture-token")
    monkeypatch.setattr(submit, "resolve_credential", credential)
    pin = Mock(return_value=("https://recipient.example", recipient))
    monkeypatch.setattr(submit, "require_pinned_recipient", pin)
    receipt = {
        k: auth[k]
        for k in (
            "version",
            "grant_id",
            "account_id",
            "store_id",
            "sync_id",
            "collection_id",
            "permissions",
            "expires_at",
        )
    }
    receipt["status"] = "verified"

    class Response:
        status = 202
        headers = Message()

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self, _limit):
            return _canonical(receipt)

    response = Response()
    connection = Mock()
    connection.getresponse.return_value = response
    factory = Mock(return_value=connection)
    monkeypatch.setattr(submit.http.client, "HTTPSConnection", factory)

    def run():
        return submit.submit_bound_export(
            tmp_path,
            tmp_path,
            origin="https://recipient.example",
            package=package,
            authorization=authorization,
            expected_account="oidc:" + "a" * 64,
            collection="project/demo/memory",
        )

    return (
        auth,
        packet,
        authorization,
        package,
        credential,
        pin,
        connection,
        response,
        receipt,
        run,
    )


def test_oidc_account_and_successful_verification(export_input):
    *_, connection, response, receipt, run = export_input
    assert run() == receipt
    connection.request.assert_called_once()
    args, kwargs = connection.request.call_args
    assert args == ("POST", "/v1/stores/alpha/grants")
    assert kwargs["headers"]["Authorization"] == "Bearer sensitive-fixture-token"
    assert "Cookie" not in kwargs["headers"]
    connection.set_debuglevel.assert_called_once_with(0)


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "attacker"),
        ("collection_id", "project/other/memory"),
        ("store_id", "beta"),
        ("sync_id", "other"),
        ("permissions", ["memory.write"]),
        ("expires_at", 0),
        ("expires_at", True),
        ("version", True),
    ],
)
def test_untrusted_request_rejected_before_credentials(export_input, field, value):
    auth, _, path, _, credential, _, connection, _, _, run = export_input
    auth[field] = value
    path.write_bytes(_canonical(auth))
    with pytest.raises(submit.ExportSubmissionError):
        run()
    credential.assert_not_called()
    connection.request.assert_not_called()


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "attacker"),
        ("recipient_public_key_sha256", "0" * 64),
        ("recipient_key_id", "other"),
        ("version", True),
        ("key_version", True),
        ("permissions", ["read", "write"]),
        ("algorithm", "other"),
    ],
)
def test_packet_mismatch_rejected_before_credentials(export_input, field, value):
    _, packet, _, path, credential, _, connection, _, _, run = export_input
    packet["context"][field] = value
    path.write_bytes(_canonical(packet))
    with pytest.raises(submit.ExportSubmissionError):
        run()
    credential.assert_not_called()
    connection.request.assert_not_called()


def test_changed_or_missing_pin_stops_before_credentials(export_input):
    _, _, _, _, credential, pin, connection, _, _, run = export_input
    pin.side_effect = ValueError("untrusted provider text")
    with pytest.raises(submit.ExportSubmissionError, match="export_request_invalid"):
        run()
    credential.assert_not_called()
    connection.request.assert_not_called()


@pytest.mark.parametrize("status", [201, 301, 307, 409, 500])
def test_rejection_never_retries_or_follows_redirect(export_input, status):
    *_, connection, response, _, run = export_input
    response.status = status
    with pytest.raises(
        submit.ExportSubmissionError, match="export_submission_rejected"
    ):
        run()
    connection.request.assert_called_once()
    connection.close.assert_called_once()


def test_invalid_receipt_is_unknown_outcome(export_input):
    *_, connection, _, receipt, run = export_input
    receipt["account_id"] = "attacker"
    with pytest.raises(submit.ExportSubmissionError, match="export_outcome_unknown"):
        run()
    connection.request.assert_called_once()


def test_active_receipt_is_distinct_from_verification(export_input):
    *_, receipt, run = export_input
    receipt["status"] = "active"
    assert run()["status"] == "active"


def test_write_requires_explicit_permission_before_credentials(export_input):
    auth, packet, path, package, credential, _, connection, _, _, run = export_input
    auth["permissions"] = ["read", "write"]
    packet["context"]["permissions"] = ["read", "write"]
    path.write_bytes(_canonical(auth))
    package.write_bytes(_canonical(packet))
    with pytest.raises(submit.ExportSubmissionError):
        run()
    credential.assert_not_called()
    connection.request.assert_not_called()
