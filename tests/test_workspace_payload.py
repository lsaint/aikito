"""Portable encoding, client validation, and safe local materialization."""

from __future__ import annotations

import hashlib
import os
import stat
from datetime import date, datetime, time, timezone
from pathlib import Path

import pytest
import aikito.workspace.payload_io as io

from aikito.workspace.payload import (
    FilePayload,
    MemberPayload,
    PayloadError,
    ResourceDescriptor,
    ResourceMutation,
    TomlPayload,
    TreeEntry,
    TreePayload,
    credential_payload,
    decode_payload,
    encode_payload,
    inspect_payload,
    payload_hash,
    validate_payload,
)
from aikito.workspace.payload_io import (
    capture_resources,
    capture_mutations,
    materialize_resources,
)
from aikito.workspace.remote import resource_for_id
from aikito.workspace.resource_write import (
    ResourceContent,
    ResourceWrite,
    credential_resources,
)
from aikito.workspace.resources import (
    SKILL_EXECUTABLE_METADATA,
    snapshot_workspace,
    value_fingerprint,
)
from aikito.workspace.toml_render import TomlValue
from aikito.workspace.transactions import _copy_resource
from workspace_payload_smoke import exercise, source_workspace


def test_all_resource_kinds_use_portable_payloads_and_shared_files_once(tmp_path):
    exercise(tmp_path)


@pytest.mark.parametrize(
    "value",
    [
        date(2026, 9, 29),
        time(12, 34, 56),
        datetime(2026, 9, 29, 12, 34, 56),
        datetime(2026, 9, 29, tzinfo=timezone.utc),
        [True, 1, 1.0, "text", {"a.b": date(2026, 9, 29)}],
        {"empty": [], "other": {"x": False}},
        float("inf"),
        float("nan"),
    ],
)
def test_typed_values_round_trip_without_stringification(value):
    payload = TomlPayload.from_value(TomlValue(("literal.dotted", ""), value))
    decoded = decode_payload(encode_payload(payload), payload_hash(payload))
    assert decoded.path == ("literal.dotted", "")
    assert type(decoded.field().value) is type(value)
    assert value_fingerprint(decoded.field().value) == value_fingerprint(value)


def test_nested_values_and_returned_copies_are_immutable():
    original = {"b": [1, {"z": 2}], "a": False}
    payload = TomlPayload.from_value(TomlValue(("field",), original))
    same = TomlPayload.from_value(
        TomlValue(("field",), {"a": False, "b": [1, {"z": 2}]})
    )
    assert encode_payload(payload) == encode_payload(same)
    original["b"].append("changed")
    payload.field().value["b"].append("changed again")
    assert encode_payload(payload) == encode_payload(same)
    with pytest.raises(PayloadError):
        TomlPayload.from_value(TomlValue(("field",), None))


def tree(executable=True):
    return TreePayload(
        (
            TreeEntry("run.sh", b"echo\n", executable),
            TreeEntry("SKILL.md", b"# Skill\n"),
            TreeEntry("empty"),
        )
    )


def skill(payload):
    placeholder = resource_for_id("skill:portable", "0" * 64)
    return resource_for_id("skill:portable", inspect_payload(placeholder, payload)[0])


def test_execution_flags_affect_transport_hash_but_not_fingerprint():
    left, right = tree(True), tree(False)
    assert payload_hash(left) != payload_hash(right)
    assert inspect_payload(skill(left), left) == inspect_payload(skill(left), right)
    assert decode_payload(encode_payload(left), payload_hash(left)) == left


def test_windows_metadata_re_read_and_posix_execution_restore(tmp_path, monkeypatch):
    payload = tree()
    resources = {"skill:portable": skill(payload)}
    win = tmp_path / "windows"
    win.mkdir()
    with monkeypatch.context() as context:
        context.setattr(io, "is_windows", lambda: True)
        content = materialize_resources(resources, {"skill:portable": payload}, win)
        assert (content.paths["skill:portable"] / SKILL_EXECUTABLE_METADATA).is_file()
        reread = capture_resources(content, ["skill:portable"])["skill:portable"]
        assert reread == payload
        copied = tmp_path / "copied"
        _copy_resource(content.paths["skill:portable"], copied, "skill")
        copied_content = ResourceContent(resources, {"skill:portable": copied})
        assert (
            capture_resources(copied_content, ["skill:portable"])["skill:portable"]
            == payload
        )
    posix = tmp_path / "posix"
    posix.mkdir()
    with monkeypatch.context() as context:
        context.setattr(io, "is_windows", lambda: False)
        restored = materialize_resources(resources, {"skill:portable": reread}, posix)
        if os.name != "nt":
            assert (
                restored.paths["skill:portable"] / "run.sh"
            ).stat().st_mode & stat.S_IXUSR
        assert not (
            restored.paths["skill:portable"] / SKILL_EXECUTABLE_METADATA
        ).exists()


@pytest.mark.parametrize(
    "path",
    [
        "/absolute",
        "../escape",
        "a/../b",
        "a//b",
        "a/./b",
        "C:/escape",
        "a\\b",
        "nul.txt",
        "con",
        "name.",
        "name ",
        "a\nfile",
        ".aikito-executable.json",
        "__pycache__/x",
    ],
)
def test_unsafe_paths_are_rejected(path):
    with pytest.raises(PayloadError):
        TreeEntry(path, b"data")


@pytest.mark.parametrize(
    "nodes",
    [
        (TreeEntry("A/x", b"a"), TreeEntry("a/y", b"b")),
        (TreeEntry("x", b"a"), TreeEntry("x", b"b")),
        (TreeEntry("x", b"a"), TreeEntry("x/y", b"b")),
        (TreeEntry("x"), TreeEntry("x/y", b"b")),
        (TreeEntry("é/x", b"a"), TreeEntry("é/y", b"b")),
    ],
)
def test_duplicate_and_target_platform_conflicts_are_rejected(nodes):
    with pytest.raises(PayloadError):
        TreePayload((TreeEntry("SKILL.md", b"skill"), *nodes))


def test_corruption_unknown_version_and_duplicate_encoding_fields():
    encoded = encode_payload(FilePayload(b"exact\r\n\x00"))
    with pytest.raises(PayloadError):
        decode_payload(encoded + b" ", hashlib.sha256(encoded).hexdigest())
    for invalid in (
        b'{"version":1,"version":1,"type":"member"}',
        b'{"version":true,"type":"member"}',
        b'{"version":99,"type":"member"}',
        b'{"version":1,"type":"link","target":"escape"}',
        b'{"version":1,"type":"file","data":"!!!"}',
    ):
        with pytest.raises(PayloadError):
            decode_payload(invalid, hashlib.sha256(invalid).hexdigest())


def test_selected_field_capture_excludes_credentials_and_filesystem_scanning(
    tmp_path, monkeypatch
):
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))

    def forbidden(*args, **kwargs):
        raise AssertionError("Credential scanning must not create temporary files")

    monkeypatch.setattr("tempfile.TemporaryDirectory", forbidden)
    assert credential_resources(
        content, {"config:auth.api_key", "config:feature.flag"}
    ) == frozenset({"config:auth.api_key"})
    captured = capture_resources(content, ["config:feature.flag"])
    assert not credential_payload(captured["config:feature.flag"])
    with pytest.raises(PayloadError, match="credential"):
        capture_resources(content, ["config:auth.api_key"])


def test_download_validation_precedes_staging_writes(tmp_path):
    good = FilePayload(b"good")
    resources = {
        "memory:notes/a.md": resource_for_id(
            "memory:notes/a.md", hashlib.sha256(good.data).hexdigest()
        ),
        "memory:notes/b.md": resource_for_id("memory:notes/b.md", "0" * 64),
    }
    with pytest.raises(PayloadError):
        materialize_resources(
            resources,
            {"memory:notes/a.md": good, "memory:notes/b.md": FilePayload(b"bad")},
            tmp_path,
        )
    assert not list(tmp_path.iterdir())


def test_client_validation_rejects_invalid_references_and_secret_download(tmp_path):
    payload = FilePayload(b'agents = ["changed"]\ncommand = "docs"\n')
    resource = resource_for_id(
        "mcp:docs", value_fingerprint({"agents": ["changed"], "command": "docs"})
    )
    with pytest.raises(PayloadError, match="references"):
        validate_payload(resource, payload)
    secret = FilePayload(b'password = "abcdefghijklmnopqrstuvwx"')
    resource = resource_for_id(
        "memory:notes/a.md", hashlib.sha256(secret.data).hexdigest()
    )
    with pytest.raises(PayloadError, match="credential"):
        materialize_resources({resource.id: resource}, {resource.id: secret}, tmp_path)
    assert not list(tmp_path.iterdir())


def test_capture_detects_post_plan_changes_and_symlinks(tmp_path):
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))
    path = source / "memory/notes/payload.md"
    path.write_bytes(b"changed")
    with pytest.raises(PayloadError, match="fingerprint"):
        capture_resources(content, ["memory:notes/payload.md"])
    path.unlink()
    path.symlink_to(source / "global/AGENTS.md")
    with pytest.raises(PayloadError, match="regular file"):
        capture_resources(content, ["memory:notes/payload.md"])


def test_mutations_distinguish_empty_fingerprint_from_absence(tmp_path):
    payload = MemberPayload()
    before = ResourceDescriptor("", payload_hash(payload), len(encode_payload(payload)))
    deletion = ResourceMutation("opaque-id", before, None, None)
    assert deletion.before.fingerprint == ""
    with pytest.raises(PayloadError):
        ResourceMutation("opaque-id", None, None, None)
    with pytest.raises(PayloadError):
        ResourceMutation("opaque-id", before, None, payload)
    with pytest.raises(PayloadError):
        ResourceMutation("opaque-id", None, before, FilePayload(b"bad"))
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))
    resource = content.resources["config:feature.flag"]
    write = ResourceWrite(
        Path("config.toml"), "config", resource.fingerprint, "feature.flag"
    )
    assert (
        capture_mutations(content, [write], {})[0].after.fingerprint
        == resource.fingerprint
    )


def test_member_deletion_and_duplicate_mutations(tmp_path):
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))
    member = content.resources["skill-selection:portable"]
    descriptor = ResourceDescriptor(
        "",
        payload_hash(MemberPayload()),
        len(encode_payload(MemberPayload())),
        member.references,
    )
    delete = ResourceWrite(
        Path("skills.toml"), "skill-selection", None, "portable", before=""
    )
    result = capture_mutations(content, [delete], {member.id: descriptor})
    assert result[0].after is None and result[0].before.fingerprint == ""
    with pytest.raises(PayloadError, match="Duplicate"):
        capture_mutations(content, [delete, delete], {member.id: descriptor})


def test_special_nodes_and_tree_symlinks_are_rejected(tmp_path):
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))
    tree_path = source / "skills/portable"
    (tree_path / "escape").symlink_to(source / "memory/notes/payload.md")
    with pytest.raises(PayloadError, match="unsafe node"):
        capture_resources(content, ["skill:portable"])
    (tree_path / "escape").unlink()
    if hasattr(os, "mkfifo"):
        os.mkfifo(tree_path / "special")
        with pytest.raises(PayloadError, match="unsafe node"):
            capture_resources(content, ["skill:portable"])


def test_metadata_cannot_smuggle_content_or_cross_symlinks(tmp_path):
    source = source_workspace(tmp_path / "source")
    content = ResourceContent.from_workspace(snapshot_workspace(source))
    tree_path = source / "skills/portable"
    metadata = tree_path / SKILL_EXECUTABLE_METADATA
    metadata.write_text(
        '{"version":1,"executable":[],"password":"abcdefghijklmnopqrstuvwx"}'
    )
    with pytest.raises(PayloadError, match="metadata"):
        capture_resources(content, ["skill:portable"])
    with pytest.raises(ValueError, match="metadata"):
        _copy_resource(tree_path, tmp_path / "copy", "skill")
    metadata.unlink()
    metadata.symlink_to(source / "config.toml")
    with pytest.raises(PayloadError, match="metadata"):
        capture_resources(content, ["skill:portable"])


@pytest.mark.parametrize("size", [-1, True, 1.0, "1", None])
def test_descriptor_rejects_invalid_encoded_size(size):
    with pytest.raises(PayloadError, match="descriptor"):
        ResourceDescriptor("opaque", "a" * 64, size)


def test_mutation_checks_size_independently_of_content_hash():
    payload = FilePayload(b"content")
    descriptor = ResourceDescriptor(
        "opaque", payload_hash(payload), len(encode_payload(payload)) + 1
    )
    with pytest.raises(PayloadError, match="size mismatch"):
        ResourceMutation("opaque", None, descriptor, payload)
