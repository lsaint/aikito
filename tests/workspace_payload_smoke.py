"""Round-trip every admitted resource through portable payloads and local writes."""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

from aikito.workspace.payload import decode_payload, encode_payload, payload_hash
from aikito.workspace.payload_io import capture_resources, prepare_payload_writes
from aikito.workspace.remote import LOCAL_CONFIG, SYNC_KINDS, RECONCILE_POLICY
from aikito.workspace.resource_write import (
    ResourceContent,
    ResourceWrite,
    verify_resource_snapshot,
)
from aikito.workspace.resources import snapshot_workspace
from aikito.workspace.transactions import PathPolicy, apply
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


def source_workspace(root: Path) -> Path:
    root = _workspace(root)
    for relative, text in {
        "memory/notes/payload.md": "# Payload\r\n",
        "projects/demo/memory/notes/payload.md": "project memory",
        "inbox/payload.md": "inbox",
        "global/AGENTS.md": "global instructions",
        "projects/demo/AGENTS.md": "project instructions",
        "skills/portable/SKILL.md": "# Portable\n",
        "skills/portable/run.sh": "echo portable\n",
        "agents/custom.toml": '[agents.custom]\nvalue = "portable"\n',
        "mcps/docs.toml": 'agents = ["custom"]\ncommand = "docs"\n',
        "subagents/review.md": '---\ndescription: "Review"\nagents: ["custom"]\n---\nReview\r\n',
        "skills.toml": 'skills = ["portable", "aikito"]\n',
        "projects/demo/agent.toml": 'name = "demo"\npaths = ["~/offline"]\nskills = ["portable"]\n[settings]\nstart = 2026-09-29\n',
        "config.toml": '"feature.flag" = true\nstart = 2026-09-29\nclock = 12:34:56\nstamp = 2026-09-29T12:34:56Z\nvalues = [1, "two", { nested = false }]\n[inbox]\npath = "inbox"\n[auth]\napi_key = "abcdefghijklmnopqrstuvwx"\n',
    }.items():
        write(root, relative, text)
    (root / "skills/portable/empty").mkdir()
    return root


def exercise(base: Path) -> None:
    source = source_workspace(base / "source")
    target = _workspace(base / "target")
    write(target, "config.toml", '[inbox]\npath = "capture"\n')
    snapshot = snapshot_workspace(source)
    identities = sorted(
        key
        for key, resource in snapshot.resources.items()
        if resource.kind in SYNC_KINDS
        and key != "config:auth.api_key"
        and not (resource.kind == "config" and resource.name in LOCAL_CONFIG)
    )
    resources = {key: snapshot.resources[key] for key in identities}
    assert {r.kind for r in resources.values()} == SYNC_KINDS
    payloads = capture_resources(ResourceContent.from_workspace(snapshot), identities)
    detached = {
        key: decode_payload(encode_payload(payload), payload_hash(payload))
        for key, payload in payloads.items()
    }
    assert "config:auth.api_key" not in detached
    assert b"abcdefghijklmnopqrstuvwx" not in b"".join(
        encode_payload(p) for p in detached.values()
    )
    right = snapshot_workspace(target)
    writes = []
    for key, resource in resources.items():
        before = right.resources.get(key)
        relative = Path(resource.parts[0].path)
        if resource.kind == "inbox":
            relative = Path("capture") / resource.name
        if before is None or before.fingerprint != resource.fingerprint:
            writes.append(
                ResourceWrite(
                    relative,
                    resource.kind,
                    resource.fingerprint,
                    resource.name,
                    before=before.fingerprint if before else None,
                )
            )
    staging = base / "staging"
    staging.mkdir()
    policy = PathPolicy(
        states=RECONCILE_POLICY.states, create_parents=True, inbox_prefix="capture"
    )
    changes, expected = prepare_payload_writes(
        resources, detached, right, writes, staging, policy=policy
    )
    assert len(changes) == len({change.path for change in changes})
    apply(
        (target,),
        changes,
        policy=policy,
        verify=lambda: verify_resource_snapshot(snapshot_workspace(target), expected),
    )
    config = tomllib.loads((target / "config.toml").read_text())
    assert config["inbox"]["path"] == "capture"
    assert "auth" not in config
    assert config["feature.flag"] is True
    assert (target / "capture/payload.md").read_bytes() == b"inbox"
    assert (target / "skills/portable/empty").is_dir()
    assert (
        snapshot_workspace(target).resources["skill:portable"].fingerprint
        == resources["skill:portable"].fingerprint
    )


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Portable resource payload smoke passed")
