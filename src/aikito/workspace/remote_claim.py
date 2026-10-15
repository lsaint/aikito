"""Prove Store ownership only to the configured HTTPS origin and chosen account.

An out-of-band challenge cannot choose a destination or credential source. Claims
never retry: a lost response may follow a completed ownership write.
"""

from __future__ import annotations

import http.client
import json
import re
import ssl
import time
from pathlib import Path

from .http_transport import validate_endpoint
from .remote_binding import load_remote_binding
from .remote_credentials import resolve_credential
from .remote_wire import decode_state_json

_ACCOUNT = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}")
_STORE_PATH = re.compile(r"/v1/stores/([a-z0-9][a-z0-9_-]{0,62})/remote")
_LIMIT = 4096


class RemoteClaimError(Exception):
    pass


def _validate_challenge(raw, binding, expected_account, now):
    if len(raw) > _LIMIT or not _ACCOUNT.fullmatch(expected_account):
        raise RemoteClaimError("claim_request_invalid")
    try:
        value = decode_state_json(raw)
        if type(value) is not dict or set(value) != {
            "version",
            "challenge_id",
            "account_id",
            "store_id",
            "sync_id",
            "expires_at",
        }:
            raise ValueError()
        endpoint = validate_endpoint(binding.endpoint)
        path = _STORE_PATH.fullmatch(endpoint.path)
        if (
            endpoint.scheme != "https"
            or endpoint.query
            or "?" in binding.endpoint
            or path is None
        ):
            raise ValueError()
        if (
            type(value["version"]) is not int
            or value["version"] != 1
            or value["account_id"] != expected_account
            or value["store_id"] != path[1]
            or value["sync_id"] != binding.sync_id
            or type(value["challenge_id"]) is not str
            or re.fullmatch(r"[0-9a-f]{64}", value["challenge_id"]) is None
            or type(value["expires_at"]) is not int
            or not now < value["expires_at"] <= now + 300
        ):
            raise ValueError()
        return endpoint, value
    except (ValueError, TypeError, KeyError):
        raise RemoteClaimError("claim_request_invalid") from None


def _read_response(response):
    lengths = response.headers.get_all("Content-Length", [])
    transfers = response.headers.get_all("Transfer-Encoding", [])
    if len(lengths) > 1 or (lengths and transfers):
        raise ValueError()
    declared = None
    if lengths:
        value = lengths[0]
        if not value.isascii() or not value.isdecimal():
            raise ValueError()
        declared = int(value)
        if declared > _LIMIT:
            raise ValueError()
    if transfers and (len(transfers) != 1 or transfers[0].lower() != "chunked"):
        raise ValueError()
    raw = response.read(_LIMIT + 1)
    if len(raw) > _LIMIT or (declared is not None and len(raw) != declared):
        raise ValueError()
    return decode_state_json(raw)


def claim_bound_store(
    workspace: Path, challenge: Path, expected_account: str, *, home: Path
):
    """Use an explicit independent account expectation; make no local state writes."""
    try:
        binding = load_remote_binding(workspace)
        if binding is None:
            raise RemoteClaimError("claim_binding_required")
        with challenge.open("rb") as stream:
            raw = stream.read(_LIMIT + 1)
        endpoint, value = _validate_challenge(
            raw, binding, expected_account, time.time()
        )
        token = resolve_credential(binding.auth, None, home=home)
        body = json.dumps(
            {
                "challenge_id": value["challenge_id"],
                "sync_id": binding.sync_id,
                "expected_account_id": expected_account,
            },
            separators=(",", ":"),
        ).encode()
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
            "Connection": "close",
        }
    except RemoteClaimError:
        raise
    except Exception:
        raise RemoteClaimError("claim_request_invalid") from None
    connection = None
    attempted = False
    try:
        connection = http.client.HTTPSConnection(
            endpoint.hostname,
            endpoint.port or 443,
            timeout=10,
            context=ssl.create_default_context(),
        )
        connection.set_debuglevel(0)
        connection.connect()
        if time.time() >= value["expires_at"]:
            raise RemoteClaimError("claim_challenge_expired")
        attempted = True
        connection.request(
            "POST",
            "/v1/stores/" + value["store_id"] + "/claim",
            body=body,
            headers=headers,
        )
        with connection.getresponse() as response:
            if response.status != 201:
                # Never follow redirects or print untrusted response bodies.
                raise RemoteClaimError("claim_rejected")
            record = _read_response(response)
        if (
            type(record) is not dict
            or set(record) != {"account_id", "store_id", "sync_id", "created_at"}
            or any(
                record[key] != value[key]
                for key in ("account_id", "store_id", "sync_id")
            )
            or type(record["created_at"]) is not int
            or record["created_at"] <= 0
        ):
            raise ValueError()
        return record
    except RemoteClaimError:
        raise
    except Exception:
        raise RemoteClaimError(
            "claim_outcome_unknown" if attempted else "claim_connection_failed"
        ) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
