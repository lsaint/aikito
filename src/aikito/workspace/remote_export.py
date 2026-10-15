"""Submit a scoped package only to an explicitly pinned HTTPS recipient.

Account and collection choices are independent of the downloaded request. A
verification receipt is not an active content grant. POSTs are never retried.
"""

from __future__ import annotations

import hashlib
import http.client
import re
import ssl
import time
from pathlib import Path

from .encrypted_remote import _parse, _unb64
from .http_transport import validate_endpoint
from .recipient_keys import require_pinned_recipient
from .remote_binding import load_remote_binding
from .remote_claim import _read_response
from .remote_credentials import resolve_credential
from .remote_wire import _canonical
from .scoped_export import (
    _authorization,
    _CONTEXT_FIELDS,
    EXPORT_SUITE,
    MAX_EXPORT_BYTES,
)


class ExportSubmissionError(Exception):
    pass


def _read(path):
    with Path(path).open("rb") as stream:
        raw = stream.read(MAX_EXPORT_BYTES + 1)
    if len(raw) > MAX_EXPORT_BYTES:
        raise ValueError()
    return raw


def submit_bound_export(
    workspace, home, *, origin, package, authorization, expected_account, collection
):
    connection = None
    attempted = False
    try:
        auth = _authorization(_read(authorization), int(time.time()))
        if (
            auth["account_id"] != expected_account
            or auth["collection_id"] != collection
        ):
            raise ValueError()
        binding = load_remote_binding(workspace)
        if binding is None:
            raise ValueError()
        endpoint = validate_endpoint(binding.endpoint)
        remote_path = re.fullmatch(
            r"/v1/stores/([a-z0-9][a-z0-9_-]{0,62})/remote", endpoint.path
        )
        if endpoint.scheme != "https" or "?" in binding.endpoint or remote_path is None:
            raise ValueError()
        if auth["store_id"] != remote_path[1] or auth["sync_id"] != binding.sync_id:
            raise ValueError()
        raw = _read(package)
        packet = _parse(raw, ("version", "context", "ciphertext"))
        context = packet["context"]
        if type(packet["version"]) is not int or packet["version"] != 1:
            raise ValueError()
        if type(context) is not dict or set(context) != set(_CONTEXT_FIELDS):
            raise ValueError()
        if any(
            context[k] != v or type(context[k]) is not type(v)
            for k, v in auth.items()
            if k != "collection_id"
        ):
            raise ValueError()
        if (
            context["algorithm"] != EXPORT_SUITE
            or type(context["key_version"]) is not int
            or context["key_version"] != 1
        ):
            raise ValueError()
        if len(_unb64(packet["ciphertext"])) < 48:
            raise ValueError()
        origin, recipient = require_pinned_recipient(workspace, home, origin)
        public = _unb64(recipient["public_key"], 32)
        if (
            context["recipient_key_id"] != recipient["key_id"]
            or context["recipient_public_key_sha256"]
            != hashlib.sha256(public).hexdigest()
        ):
            raise ValueError()
        token = resolve_credential(binding.auth, None, home=home)
        target = validate_endpoint(origin)
        connection = http.client.HTTPSConnection(
            target.hostname,
            target.port or 443,
            timeout=30,
            context=ssl.create_default_context(),
        )
        connection.set_debuglevel(0)
        connection.connect()
        if time.time() >= auth["expires_at"]:
            raise ExportSubmissionError("export_request_expired")
        attempted = True
        connection.request(
            "POST",
            "/v1/stores/" + auth["store_id"] + "/grants",
            body=raw,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            if response.status != 202:
                raise ExportSubmissionError("export_submission_rejected")
            receipt = _read_response(response)
        expected = {
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
        expected["status"] = "verified"
        if _canonical(receipt) != _canonical(expected):
            raise ValueError()
        return receipt
    except ExportSubmissionError:
        raise
    except Exception:
        raise ExportSubmissionError(
            "export_outcome_unknown" if attempted else "export_request_invalid"
        ) from None
    finally:
        if connection is not None:
            try:
                connection.close()
            except OSError:
                pass
