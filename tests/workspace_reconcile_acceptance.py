"""End-to-end acceptance of two independent replicas through one resource center."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from pathlib import Path
from unittest.mock import patch

from aikito.workspace import transactions
from aikito.workspace.reconcile import (
    WorkspaceReconcileError,
)
from aikito.workspace.remote import (
    FilesystemRemote,
)
from aikito.workspace.resource_state import (
    LOCAL_CONFIG,
    REPLICA_STATE,
    SYNC_KINDS,
)
from aikito.workspace.resources import snapshot_workspace
from workspace_reconcile_backend import (
    FilesystemBackend,
    ReconciliationBackend,
    files as _files,
)
from workspace_reconcile_resources_smoke import write
from workspace_reconcile_smoke import _workspace


def _resources(root: Path) -> dict[str, str]:
    return {
        identity: resource.fingerprint
        for identity, resource in snapshot_workspace(root).resources.items()
        if resource.kind in SYNC_KINDS
        and not (resource.kind == "config" and resource.name in LOCAL_CONFIG)
    }


def exercise_behavior(base: Path, backend: ReconciliationBackend) -> None:
    a, b = _workspace(base / "left"), _workspace(base / "right")
    home = base / "home"

    def apply(local: Path, **kwargs):
        plan = backend.run(local, home, **kwargs)
        assert not plan.blocked, plan.findings
        return plan

    def converge():
        for local in (a, b, a):
            assert not apply(local).conflicts
        fingerprints = backend.fingerprints()
        assert _resources(a) == _resources(b) == fingerprints
        for local, prefix in ((a, "capture-a"), (b, "capture-b")):
            config = tomllib.loads((local / "config.toml").read_text(encoding="utf-8"))
            assert config["inbox"]["path"] == prefix
        assert "config:inbox.path" not in fingerprints

    for local, prefix in ((a, "capture-a"), (b, "capture-b")):
        write(local, "config.toml", f'[inbox]\npath = "{prefix}"\n')
        assert not apply(local).conflicts
    payloads = {
        "memory/notes/acceptance.md": "first",
        "projects/demo/memory/notes/acceptance.md": "first",
        "capture-a/deep/acceptance.md": "first",
        "global/AGENTS.md": "first",
        "projects/demo/AGENTS.md": "first",
        "skills/example/SKILL.md": "# Example\n",
        "skills/example/run.sh": "echo first\n",
        "agents/custom.toml": '[agents.custom]\nvalue = "first"\n',
        "mcps/docs.toml": 'agents = ["custom"]\ncommand = "first"\n',
        "subagents/review.md": '---\ndescription: "Review"\nagents: ["custom"]\n---\nfirst\n',
        "skills.toml": 'skills = ["example", "aikito"]\n',
        "projects/demo/agent.toml": 'name = "demo"\npaths = ["~/offline"]\nskills = ["example"]\n',
        "config.toml": '[inbox]\npath = "capture-a"\n[memory]\nstale_days = 10\n[update]\ncheck = true\n',
    }
    for relative, value in payloads.items():
        write(a, relative, value)
    (a / "skills/example/empty").mkdir()
    before = _files(a), _files(b), backend.checkpoint()
    preview = backend.plan(a)
    assert preview.changes and not preview.conflicts and not preview.blocked
    assert before == (_files(a), _files(b), backend.checkpoint())
    converge()
    assert {identity.partition(":")[0] for identity in _resources(a)} == SYNC_KINDS
    assert (b / "skills/example/empty").is_dir()

    # Credentials block only their resource; safe neighboring fields still sync.
    text = (a / "config.toml").read_text(encoding="utf-8")
    write(a, "config.toml", text + '[service]\napi_key = "abcdefghijklmnop123456"\n')
    write(a, "capture-a/credential-safe.md", "safe alongside a blocked field")
    blocked = apply(a)
    assert (
        next(
            item for item in blocked.items if item.id == "config:service.api_key"
        ).action
        == "BLOCKED"
    )
    assert "config:service.api_key" not in backend.fingerprints()
    assert (
        "config:service.api_key"
        not in json.loads((a / REPLICA_STATE).read_text())["base"]
    )
    apply(b)
    assert (b / "capture-b/credential-safe.md").is_file()
    assert "service" not in tomllib.loads((b / "config.toml").read_text())
    write(a, "config.toml", text)
    converge()

    # Every admitted kind can be deleted as one batch, then recreated safely.
    preserved = {relative: (a / relative).read_bytes() for relative in payloads}
    for area in (
        "memory/notes",
        "projects",
        "capture-a",
        "global",
        "skills",
        "agents",
        "mcps",
        "subagents",
    ):
        shutil.rmtree(a / area)
        (a / area).mkdir(parents=True)
    write(a, "config.toml", '[inbox]\npath = "capture-a"\n')
    write(a, "skills.toml", "skills = []\n")
    revision = backend.revision()
    deleted = apply(a)
    assert {item.id.partition(":")[0] for item in deleted.changes} == SYNC_KINDS
    assert backend.revision() == revision + 1
    converge()
    assert not backend.fingerprints()
    for relative, content in preserved.items():
        path = a / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    (a / "skills/example/empty").mkdir()
    converge()

    # Standalone bytes, skill trees, typed fields, and member identities update
    # through the same payload boundary, then return to the original version.
    for relative, content in preserved.items():
        updated = (
            content.decode("utf-8")
            .replace("\r\n", "\n")
            .replace("first", "updated")
            .replace("# Example", "# Updated")
            .replace("~/offline", "~/updated")
            .replace("stale_days = 10", "stale_days = 11")
        )
        write(a, relative, updated)
    converge()
    assert (b / "skills/example/SKILL.md").read_text() == "# Updated\n"
    assert (b / "memory/notes/acceptance.md").read_text() == "updated"
    for relative, content in preserved.items():
        (a / relative).write_bytes(content)
    converge()

    # Identical concurrent shared-field edits confirm bases without another upload.
    for local in (a, b):
        text = (local / "config.toml").read_text(encoding="utf-8")
        write(local, "config.toml", text.replace("true", "false"))
    apply(b)
    revision = backend.revision()
    plan = apply(a)
    assert (
        next(item for item in plan.items if item.id == "config:update.check").action
        == "NOOP"
    )
    assert backend.revision() == revision
    converge()

    # Conflicts retain their old base while independent additions still converge.
    ancestor = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))["base"][
        "config:memory.stale_days"
    ]
    for local, days in ((a, 20), (b, 30)):
        text = (local / "config.toml").read_text(encoding="utf-8")
        write(local, "config.toml", text.replace("10", str(days)))
    apply(b)
    write(a, "capture-a/safe.md", "safe")
    plan = apply(a)
    assert {item.id for item in plan.conflicts} == {"config:memory.stale_days"}
    assert (
        json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))["base"][
            "config:memory.stale_days"
        ]
        == ancestor
    )
    assert backend.fingerprints()["inbox:safe.md"] == _resources(a)["inbox:safe.md"]
    apply(b)
    assert (b / "capture-b/safe.md").read_text(encoding="utf-8") == "safe"
    assert not apply(a, resolutions={"config:memory.stale_days": "local"}).conflicts
    converge()

    # A stale local plan and a stale center revision both fail without any writes.
    write(a, "memory/notes/pending.md", "pending")
    plan = backend.plan(a)
    write(a, "projects/demo/AGENTS.md", "after preview")
    before = _files(a), _files(b), backend.checkpoint()
    try:
        backend.apply(plan, home)
    except WorkspaceReconcileError as exc:
        assert "changed after planning" in str(exc)
    else:
        raise AssertionError("A stale local plan was accepted")
    assert before == (_files(a), _files(b), backend.checkpoint())
    plan = backend.plan(a)
    write(b, "global/AGENTS.md", "new revision")
    apply(b)
    before = _files(a), _files(b), backend.checkpoint()
    try:
        backend.apply(plan, home)
    except WorkspaceReconcileError as exc:
        assert "changed after planning" in str(exc)
    else:
        raise AssertionError("A stale center plan was accepted")
    assert before == (_files(a), _files(b), backend.checkpoint())
    converge()

    # Recover a shared-file download together with its unchanged replica base.
    text = (b / "config.toml").read_text(encoding="utf-8")
    write(b, "config.toml", text.replace("20", "40"))
    apply(b)
    before = _files(a)
    original = os.replace

    def interrupt(src, dst):
        if "stage" in Path(src).parts and Path(dst) == a / "config.toml":
            original(src, dst)
            raise KeyboardInterrupt
        original(src, dst)

    try:
        with patch("aikito.workspace.transactions.os.replace", side_effect=interrupt):
            apply(a)
    except KeyboardInterrupt:
        pass
    else:
        raise AssertionError("Expected shared-file interruption")
    try:
        apply(a)
    except WorkspaceReconcileError as exc:
        assert "Recovered an interrupted" in str(exc)
    else:
        raise AssertionError("Expected recovery before replay")
    assert _files(a) == before
    converge()

    # Center acceptance and local application are separate transactions. Fail
    # after a simultaneous upload has published, keeping downloads and old Base.
    for failure in ("download", "base"):
        write(b, "capture-b/accepted-before-local.md", failure)
        apply(b)
        write(a, f"memory/notes/accepted-{failure}.md", "accepted upload")
        before = _files(a)
        state_before = (a / REPLICA_STATE).read_bytes()
        revision = backend.revision()
        original_replace = os.replace
        original_text = transactions.atomic_text
        failed = False

        def fail_download(src, dst):
            nonlocal failed
            if (
                not failed
                and "stage" in Path(src).parts
                and Path(dst) == a / "capture-a/accepted-before-local.md"
            ):
                failed = True
                raise OSError("Local download failed after center acceptance")
            original_replace(src, dst)

        def fail_base(path, content):
            nonlocal failed
            if not failed and Path(path) == a / REPLICA_STATE:
                failed = True
                raise OSError("Local Base failed after center acceptance")
            original_text(path, content)

        # Fault injection concerns only local transactions, never backend files.
        injection = (
            patch("aikito.workspace.transactions.os.replace", side_effect=fail_download)
            if failure == "download"
            else patch(
                "aikito.workspace.transactions.atomic_text", side_effect=fail_base
            )
        )
        try:
            with injection:
                apply(a)
        except OSError as exc:
            assert "after center acceptance" in str(exc)
        else:
            raise AssertionError("Expected local application failure")
        assert failed
        assert backend.revision() == revision + 1
        assert f"memory:notes/accepted-{failure}.md" in backend.fingerprints()
        assert _files(a) == before and (a / REPLICA_STATE).read_bytes() == state_before
        converge()
        assert backend.revision() == revision + 1
        assert (a / "capture-a/accepted-before-local.md").read_text() == failure

    # Removal and local relocation preserve pairing and host-local paths.
    (b / "capture-b/safe.md").unlink()
    apply(b)
    converge()
    assert not (a / "capture-a/safe.md").exists()
    identity = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))
    shutil.move(a, base / "moved-left")
    a = base / "moved-left"
    write(a, "capture-a/relocated.md", "relocated")
    converge()
    assert (b / "capture-b/relocated.md").read_text(encoding="utf-8") == "relocated"
    state = json.loads((a / REPLICA_STATE).read_text(encoding="utf-8"))
    assert (state["sync_id"], state["replica_id"]) == (
        identity["sync_id"],
        identity["replica_id"],
    )
    write(b, "projects/demo/memory/notes/acceptance.md", "final")
    converge()
    assert (a / "projects/demo/memory/notes/acceptance.md").read_text(
        encoding="utf-8"
    ) == "final"
    before = _files(a), _files(b), backend.checkpoint()
    for local in (a, b, a, b):
        plan = apply(local)
        assert not plan.changes and not plan.conflicts
    assert before == (_files(a), _files(b), backend.checkpoint())


def exercise(base: Path) -> None:
    backend = FilesystemBackend(base / "center")
    exercise_behavior(base, backend)
    # Center relocation is a FilesystemRemote lifecycle check, not shared behavior.
    before = backend.remote.read()
    shutil.move(backend.remote.root, base / "moved-center")
    backend.remote = FilesystemRemote(base / "moved-center")
    assert backend.remote.read() == before
    a, b, home = base / "moved-left", base / "right", base / "home"
    write(a, "capture-a/center-relocated.md", "after center relocation")
    for local in (a, b, a):
        plan = backend.run(local, home)
        assert not plan.blocked and not plan.conflicts
    assert (
        b / "capture-b/center-relocated.md"
    ).read_text() == "after center relocation"
    assert _resources(a) == _resources(b) == backend.fingerprints()


if __name__ == "__main__":
    exercise(Path(sys.argv[1]).resolve())
    print("[SUCCESS] Workspace reconciliation acceptance passed")
