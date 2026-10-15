"""Ownership intent and credential confinement for HTTPS Store proofs."""

import json
import time
from email.message import Message
from unittest.mock import Mock

import pytest

from aikito.cli_parser import build_parser
from aikito.workspace import remote_claim as claim
from aikito.workspace.remote_binding import RemoteAuth, RemoteBinding


@pytest.fixture
def claim_request(tmp_path, monkeypatch):
    binding = RemoteBinding(
        1,
        "https://store.example/v1/stores/alpha/remote",
        "00112233445566778899aabbccddeeff",
        RemoteAuth("bearer_env", "TEST_TOKEN"),
    )
    value = {
        "version": 1,
        "challenge_id": "a" * 64,
        "account_id": "account-1",
        "store_id": "alpha",
        "sync_id": "00112233445566778899aabbccddeeff",
        "expires_at": int(time.time()) + 240,
    }
    path = tmp_path / "challenge.json"
    path.write_text(json.dumps(value))
    monkeypatch.setattr(claim, "load_remote_binding", lambda _: binding)
    credential = Mock(return_value="sensitive-proof-token")
    monkeypatch.setattr(claim, "resolve_credential", credential)
    return binding, value, path, credential


def test_parser_requires_independent_account_intent():
    args = build_parser().parse_args(
        [
            "remote",
            "claim",
            "--workspace",
            "ws",
            "--challenge",
            "request.json",
            "--account",
            "account-1",
        ]
    )
    assert args.account == "account-1"
    with pytest.raises(SystemExit):
        build_parser().parse_args(
            ["remote", "claim", "--workspace", "ws", "--challenge", "request.json"]
        )


@pytest.mark.parametrize(
    "field,value",
    [
        ("account_id", "attacker"),
        ("store_id", "beta"),
        ("sync_id", "other"),
        ("expires_at", 0),
        ("expires_at", True),
        ("expires_at", 9000000000),
        ("version", True),
        ("challenge_id", "bad"),
        ("endpoint", "https://attacker.example"),
    ],
)
def test_malformed_or_wrong_scope_rejected_before_credentials(
    claim_request, tmp_path, field, value
):
    _, body, path, credential = claim_request
    body[field] = value
    path.write_text(json.dumps(body))
    with pytest.raises(claim.RemoteClaimError, match="claim_request_invalid"):
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
    credential.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://127.0.0.1/v1/stores/alpha/remote",
        "https://store.example/custom",
        "https://store.example/v1/stores/alpha/remote?route=proof",
        "https://store.example/v1/stores/alpha/remote?",
    ],
)
def test_only_exact_configured_https_route_is_usable(
    claim_request, tmp_path, monkeypatch, endpoint
):
    binding, _, path, credential = claim_request
    monkeypatch.setattr(
        claim,
        "load_remote_binding",
        lambda _: RemoteBinding(1, endpoint, binding.sync_id, binding.auth),
    )
    with pytest.raises(claim.RemoteClaimError, match="claim_request_invalid"):
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
    credential.assert_not_called()


def test_duplicate_fields_fail_without_reading_token(claim_request, tmp_path):
    _, _, path, credential = claim_request
    path.write_text('{"version":1,"version":1}')
    with pytest.raises(claim.RemoteClaimError):
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
    credential.assert_not_called()


class Response:
    status = 201

    def __init__(self, record):
        self.raw = json.dumps(record).encode()
        self.headers = Message()
        self.headers["Content-Length"] = str(len(self.raw))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, limit):
        return self.raw[:limit]


def connection(monkeypatch, value):
    response = Response(
        {key: value[key] for key in ("account_id", "store_id", "sync_id")}
        | {"created_at": 1}
    )
    transport = Mock()
    transport.getresponse.return_value = response
    create = Mock(return_value=transport)
    monkeypatch.setattr(claim.http.client, "HTTPSConnection", create)
    return create, transport, response


def test_only_bound_origin_receives_credentials_and_explicit_account(
    claim_request, tmp_path, monkeypatch
):
    _, value, path, _ = claim_request
    create, transport, _ = connection(monkeypatch, value)
    assert (
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)[
            "account_id"
        ]
        == "account-1"
    )
    assert create.call_args.args == ("store.example", 443)
    transport.set_debuglevel.assert_called_once_with(0)
    args, kwargs = transport.request.call_args
    assert args == ("POST", "/v1/stores/alpha/claim")
    assert kwargs["headers"]["Authorization"] == "Bearer sensitive-proof-token"
    assert json.loads(kwargs["body"])["expected_account_id"] == "account-1"
    transport.close.assert_called_once()


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 409, 500])
def test_rejections_and_redirects_never_retry(
    claim_request, tmp_path, monkeypatch, status
):
    _, value, path, _ = claim_request
    create, transport, response = connection(monkeypatch, value)
    response.status = status
    with pytest.raises(claim.RemoteClaimError, match="claim_rejected"):
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
    create.assert_called_once()
    transport.request.assert_called_once()


def test_response_loss_is_uncertain_and_secret_errors_are_hidden(
    claim_request, tmp_path, monkeypatch
):
    _, value, path, _ = claim_request
    create, transport, _ = connection(monkeypatch, value)
    transport.getresponse.side_effect = OSError("sensitive-proof-token")
    with pytest.raises(claim.RemoteClaimError) as error:
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
    assert str(error.value) == "claim_outcome_unknown"
    create.assert_called_once()


@pytest.mark.parametrize(
    "record", [{"account_id": "attacker"}, [], {"created_at": True}]
)
def test_unexpected_success_response_is_not_trusted(
    claim_request, tmp_path, monkeypatch, record
):
    _, value, path, _ = claim_request
    _, transport, _ = connection(monkeypatch, value)
    transport.getresponse.return_value = Response(record)
    with pytest.raises(claim.RemoteClaimError, match="claim_outcome_unknown"):
        claim.claim_bound_store(tmp_path, path, "account-1", home=tmp_path)
