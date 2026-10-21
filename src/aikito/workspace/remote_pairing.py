"""Connect a device through browser approval without copying credentials.

The device generates the credential and sends only its hash until polling. The
browser sees a short comparison code, never the credential. Redirects and changes
of service origin are rejected before anything is persisted.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import re
import secrets
import ssl
import time
import webbrowser
from pathlib import Path
from urllib.parse import urlsplit

from .remote_access import RemoteBindingError
from .remote_credentials import save_credential


def _service_origin(service: str) -> str:
    try:
        parsed = urlsplit(service)
        valid = (
            parsed.scheme == "https"
            and parsed.hostname
            and not parsed.username
            and not parsed.password
            and parsed.path in ("", "/")
            and not parsed.query
            and not parsed.fragment
            and not any(c.isspace() for c in service)
            and parsed.port != 0
            and "?" not in service
            and "#" not in service
        )
    except ValueError:
        valid = False
    if not valid:
        raise RemoteBindingError("A service HTTPS origin is required.")
    return f"https://{parsed.netloc}"


def _request(origin: str, path: str, body: dict) -> tuple[int, dict]:
    parsed = urlsplit(origin)
    connection = http.client.HTTPSConnection(
        parsed.hostname,
        parsed.port or 443,
        timeout=10,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "POST",
            path,
            json.dumps(body).encode(),
            {
                "Content-Type": "application/json",
                "Connection": "close",
            },
        )
        with connection.getresponse() as response:
            raw = response.read(8193)
            if len(raw) > 8192 or response.status not in (200, 201, 202):
                raise ValueError()
            value = json.loads(raw)
            if type(value) is not dict:
                raise ValueError()
            return response.status, value
    except Exception:
        raise RemoteBindingError(
            "Device connection failed. Check the service and run bind again."
        ) from None
    finally:
        connection.close()


def pair_device(service: str, *, home: Path) -> tuple[str, str]:
    origin = _service_origin(service)
    secret = secrets.token_hex(32)
    challenge = hashlib.sha256(secret.encode()).hexdigest()
    status, started = _request(origin, "/v1/sync/pair", {"challenge": challenge})
    request = started.get("request")
    if (
        status != 201
        or type(request) is not str
        or re.fullmatch(r"[0-9a-f]{64}", request) is None
        or started.get("verification_uri")
        != origin + "/sync/connect?request=" + request
        or started.get("code") != request[:8]
        or type(started.get("expires_in")) is not int
        or not 1 <= started["expires_in"] <= 300
    ):
        raise RemoteBindingError("Invalid device connection response.")
    print(f"Open {started['verification_uri']}")
    print(f"Check code: {request[:4]}-{request[4:8]}. Approve only your own device.")
    try:
        webbrowser.open(started["verification_uri"])
    except webbrowser.Error:
        pass
    deadline = time.monotonic() + started["expires_in"]
    while time.monotonic() < deadline:
        time.sleep(2)
        status, result = _request(
            origin,
            "/v1/sync/poll",
            {
                "request": request,
                "secret": secret,
            },
        )
        if status == 202 and result == {"status": "pending"}:
            continue
        endpoint = result.get("endpoint")
        if (
            status != 200
            or type(endpoint) is not str
            or re.fullmatch(
                re.escape(origin) + r"/v1/stores/[a-z0-9][a-z0-9_-]{0,62}/remote",
                endpoint,
            )
            is None
            or type(result.get("account_id")) is not str
            or not result["account_id"]
        ):
            raise RemoteBindingError("Invalid device connection response.")
        key = "AIKITO_REMOTE_DEVICE_" + challenge[:24].upper()
        save_credential(key, secret, home=home)
        return endpoint, key
    raise RemoteBindingError("Device connection expired. Run bind again.")
