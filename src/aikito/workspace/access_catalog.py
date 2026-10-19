"""Public collection directory and device-authenticated approval transport."""

from __future__ import annotations

import http.client
import json
import re
import ssl

from .device_keys import DeviceKeyStore
from .encrypted_discovery import unlock_encrypted_snapshot
from .http_transport import validate_endpoint
from .recipient_keys import require_pinned_recipient
from .remote_binding import load_remote_binding
from .remote_credentials import resolve_credential
from .remote_factory import BoundRemoteReader, _open_remote
from .remote_wire import _canonical
from .scoped_export import _collection


class AccessRequestError(ValueError):
    pass


def collections(workspace):
    result = ["global/instructions", "global/memory", "skills", "subagents", "inbox"]
    for config in sorted((workspace / "projects").glob("*/agent.toml")):
        if (
            config.is_file()
            and not config.is_symlink()
            and not config.parent.is_symlink()
        ):
            for kind in ("instructions", "memory"):
                identity = f"project/{config.parent.name}/{kind}"
                _collection(identity)
                result.append(identity)
    if len(result) > 512:
        raise AccessRequestError("Collection directory exceeds the supported limit")
    return result


def target(workspace):
    binding = load_remote_binding(workspace)
    if binding is None:
        raise AccessRequestError("Bind an encrypted Remote first")
    endpoint = validate_endpoint(binding.endpoint)
    match = re.fullmatch(r"/v1/stores/([a-z0-9][a-z0-9_-]{0,62})/remote", endpoint.path)
    if endpoint.scheme != "https" or "?" in binding.endpoint or not match:
        raise AccessRequestError("A bound HTTPS Store is required")
    return binding, f"https://{endpoint.netloc}", match[1]


def exchange(workspace, home, suffix, *, body=None, post=False):
    binding, origin, store = target(workspace)
    endpoint = validate_endpoint(origin)
    token = resolve_credential(binding.auth, None, home=home)
    raw = (b"" if post else None) if body is None else _canonical(body)
    if raw is not None and len(raw) > 65536:
        raise AccessRequestError("Approval request exceeds the supported limit")
    connection = http.client.HTTPSConnection(
        endpoint.hostname,
        endpoint.port or 443,
        timeout=15,
        context=ssl.create_default_context(),
    )
    try:
        connection.request(
            "GET" if raw is None else "POST",
            f"/v1/stores/{store}/access/{suffix}",
            body=raw,
            headers={
                "Authorization": "Bearer " + token,
                "Content-Type": "application/json",
                "Connection": "close",
                "Accept-Encoding": "identity",
            },
        )
        with connection.getresponse() as response:
            if (
                response.status != 200
                or response.headers.get("Content-Type") != "application/json"
                or response.headers.get("Content-Encoding") not in (None, "identity")
            ):
                raise AccessRequestError("Authorization service is unavailable")
            data = response.read(4 * 1024 * 1024 + 1)
            if len(data) > 4 * 1024 * 1024:
                raise AccessRequestError(
                    "Approval response exceeds the supported limit"
                )
            result = json.loads(data)
            if type(result) is not dict:
                raise AccessRequestError("Invalid approval response")
            return result
    except AccessRequestError:
        raise
    except Exception:  # noqa: BLE001 - Sanitize untrusted transport and provider errors.
        raise AccessRequestError("Authorization service is unavailable") from None
    finally:
        connection.close()


def publish_catalog(workspace, home):
    binding, origin, _ = target(workspace)
    require_pinned_recipient(workspace, home, origin)
    reader = BoundRemoteReader(
        _open_remote(binding.endpoint, binding.auth, None, home=home), binding.sync_id
    )
    vault = unlock_encrypted_snapshot(
        reader, reader.read(), DeviceKeyStore(home=home, workspace=workspace)
    )
    return exchange(
        workspace,
        home,
        "catalog",
        body={
            "sync_id": binding.sync_id,
            "vault_id": vault.vault_id,
            "collections": collections(workspace),
        },
    )
