"""Host-local, non-authoritative access observations; never credentials or consent."""

from __future__ import annotations

import json
import re
import time

from .workspace.recipient_keys import canonical_origin
from .workspace.resource_state import state_path
from .workspace.transactions import atomic_text

HISTORY_STATE = ".local/state/aikito/console-access.json"


def _grants(values):
    if not isinstance(values, list) or len(values) > 128:
        raise ValueError("Invalid access history")
    result = []
    for grant in values:
        if (
            not isinstance(grant, dict)
            or not isinstance(grant.get("grant_id"), str)
            or not re.fullmatch(r"[0-9a-f]{64}", grant["grant_id"])
            or grant.get("permissions") not in (["read"], ["read", "write"])
            or type(grant.get("expires_at")) is not int
            or not 0 < grant["expires_at"] < 2**53
            or grant.get("status") != "active"
        ):
            raise ValueError("Invalid access history")
        result.append(
            {
                key: grant[key]
                for key in ("grant_id", "permissions", "expires_at", "status")
            }
        )
    return sorted(result, key=lambda grant: grant["grant_id"])


def _load(workspace):
    path = state_path(workspace, HISTORY_STATE)
    if not path.exists():
        return []
    if path.stat().st_size > 1024 * 1024:
        raise ValueError("Invalid access history")
    document = json.loads(path.read_text(encoding="utf-8"))
    if (
        not isinstance(document, dict)
        or document.get("version") != 1
        or not isinstance(document.get("records"), list)
        or len(document["records"]) > 128
        or any(not isinstance(record, dict) for record in document["records"])
    ):
        raise ValueError("Invalid access history")
    return document["records"]


def history(workspace, binding):
    if binding is None:
        return []
    records = []
    for record in _load(workspace):
        if (
            record.get("endpoint") != binding.endpoint
            or record.get("sync_id") != binding.sync_id
        ):
            continue
        if (
            type(record.get("checked_at")) is not int
            or not isinstance(record.get("account_id"), str)
            or not re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", record["account_id"]
            )
        ):
            raise ValueError("Invalid access history")
        records.append(
            {
                "origin": canonical_origin(record["origin"]),
                "collection_id": record["collection_id"],
                "account_id": record["account_id"],
                "checked_at": record["checked_at"],
                "grants": _grants(record["grants"]),
            }
        )
    return sorted(records, key=lambda record: record["checked_at"], reverse=True)


def record(workspace, intent, account, grants, *, collection):
    """Project only public metadata; caller holds the workspace writer lock."""
    if not isinstance(account, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", account
    ):
        raise ValueError("Invalid account")
    observation = {
        "origin": intent.origin,
        "account_id": account,
        "collection_id": collection,
        "endpoint": intent.binding.endpoint,
        "sync_id": intent.sync_id,
        "checked_at": int(time.time()),
        "grants": _grants(grants),
    }
    records = _load(workspace)
    key = ("origin", "account_id", "collection_id", "endpoint", "sync_id")
    others = []
    for previous in records:
        if all(previous.get(field) == observation[field] for field in key):
            if (
                previous.get("grants") == observation["grants"]
                and 0 <= observation["checked_at"] - previous.get("checked_at", 0) < 60
            ):
                return
        else:
            others.append(previous)
    others.append(observation)
    others.sort(key=lambda row: row["checked_at"], reverse=True)
    path = state_path(workspace, HISTORY_STATE, create=True)
    others = others[:128]
    encoded = json.dumps({"version": 1, "records": others}, ensure_ascii=True) + "\n"
    while len(encoded.encode("utf-8")) > 1024 * 1024 and len(others) > 1:
        others.pop()
        encoded = (
            json.dumps({"version": 1, "records": others}, ensure_ascii=True) + "\n"
        )
    atomic_text(path, encoded)
