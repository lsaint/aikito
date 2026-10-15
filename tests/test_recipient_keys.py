"""Recipient trust changes require explicit acceptance and preserve local state."""

import base64

import pytest

from aikito.workspace import recipient_keys as keys
from aikito.workspace.remote_wire import _canonical


@pytest.fixture
def recipient(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config))
    monkeypatch.setenv("APPDATA", str(config))
    document = {
        "version": 1,
        "algorithm": "hpke-x25519-hkdf-sha256-a256gcm",
        "key_id": "recipient-1",
        "public_key": base64.b64encode(bytes(range(32))).decode(),
    }
    monkeypatch.setattr(keys, "_fetch", lambda origin: dict(document))
    return home, workspace, config, document


def invoke(recipient, tmp_path, name, **kwargs):
    home, workspace, _, _ = recipient
    return keys.fetch_recipient(
        workspace,
        home,
        origin="https://recipient.example",
        output=tmp_path / name,
        **kwargs,
    )


def test_first_use_repeat_and_explicit_change(recipient, tmp_path):
    _, workspace, config, document = recipient
    assert invoke(recipient, tmp_path, "first.json") == document
    pins = list((config / "aikito/remote-recipients").glob("*.json"))
    assert len(pins) == 1
    original = pins[0].read_bytes()
    assert (tmp_path / "first.json").read_bytes() == _canonical(document)
    assert not list(workspace.rglob("*.json"))
    invoke(recipient, tmp_path, "second.json")
    assert pins[0].read_bytes() == original
    document["key_id"] = "recipient-2"
    with pytest.raises(keys.RecipientKeyError, match="recipient_key_changed"):
        invoke(recipient, tmp_path, "rejected.json")
    assert not (tmp_path / "rejected.json").exists()
    assert pins[0].read_bytes() == original
    invoke(recipient, tmp_path, "accepted.json", accept_key_change=True)
    assert pins[0].read_bytes() != original
    invoke(recipient, tmp_path, "repeat.json")


def test_same_id_changed_public_key_requires_acceptance(recipient, tmp_path):
    invoke(recipient, tmp_path, "first.json")
    recipient[3]["public_key"] = base64.b64encode(b"x" * 32).decode()
    with pytest.raises(keys.RecipientKeyError, match="recipient_key_changed"):
        invoke(recipient, tmp_path, "rejected.json")


@pytest.mark.parametrize(
    "origin",
    [
        "http://example.com",
        "https://example.com/",
        "https://example.com/path",
        "https://example.com?",
        "https://example.com#",
        "https://user@example.com",
        "https://example.com:",
    ],
)
def test_invalid_origin_precedes_network(recipient, tmp_path, monkeypatch, origin):
    def unexpected(origin):
        pytest.fail("untrusted destination reached network")

    monkeypatch.setattr(keys, "_fetch", unexpected)
    home, workspace, config, _ = recipient
    with pytest.raises(keys.RecipientKeyError):
        keys.fetch_recipient(
            workspace, home, origin=origin, output=tmp_path / "out.json"
        )
    assert not config.exists()


def test_default_port_and_case_share_one_pin():
    assert keys.canonical_origin("https://EXAMPLE.com:443") == "https://example.com"
    assert keys.canonical_origin("https://[::1]:444") == "https://[::1]:444"


def test_existing_output_and_workspace_config_are_rejected(
    recipient, tmp_path, monkeypatch
):
    (tmp_path / "existing.json").write_bytes(b"original")
    with pytest.raises(keys.RecipientKeyError):
        invoke(recipient, tmp_path, "existing.json")
    assert (tmp_path / "existing.json").read_bytes() == b"original"
    home, workspace, _, _ = recipient
    monkeypatch.setenv("XDG_CONFIG_HOME", str(workspace))
    monkeypatch.setenv("APPDATA", str(workspace))
    with pytest.raises(keys.RecipientKeyError):
        invoke(recipient, tmp_path, "blocked.json")
    assert not list(workspace.iterdir())


@pytest.mark.parametrize(
    "kind",
    [
        "corrupt",
        "duplicate",
        "wrong-origin",
        "wrong-version",
        "hardlink",
        "symlink",
        "permissions",
    ],
)
def test_corrupt_or_unsafe_pin_never_resets_trust(recipient, tmp_path, kind):
    invoke(recipient, tmp_path, "first.json")
    pin = next((recipient[2] / "aikito/remote-recipients").glob("*.json"))
    if kind == "corrupt":
        pin.write_bytes(b"broken")
    elif kind == "duplicate":
        pin.write_bytes(b'{"version":1,' + pin.read_bytes()[1:])
    elif kind == "wrong-origin":
        pin.write_bytes(
            pin.read_bytes().replace(b"recipient.example", b"different.example")
        )
    elif kind == "wrong-version":
        pin.write_bytes(pin.read_bytes().replace(b'"version":1', b'"version":true', 1))
    elif kind == "hardlink":
        (tmp_path / "alias").hardlink_to(pin)
    elif kind == "symlink":
        actual = tmp_path / "actual.json"
        pin.rename(actual)
        try:
            pin.symlink_to(actual)
        except OSError:
            pytest.skip("Creating symlinks is unavailable")
    elif kind == "permissions":
        if keys.is_windows():
            pytest.skip("POSIX permissions only")
        pin.chmod(0o644)
    before = pin.read_bytes()
    with pytest.raises(keys.RecipientKeyError):
        invoke(recipient, tmp_path, "blocked.json", accept_key_change=True)
    assert not (tmp_path / "blocked.json").exists()
    assert pin.read_bytes() == before


def test_failed_output_keeps_successful_pin(recipient, tmp_path):
    with pytest.raises(keys.RecipientKeyError):
        invoke(recipient, tmp_path, "missing/output.json")
    assert len(list((recipient[2] / "aikito/remote-recipients").glob("*.json"))) == 1
    invoke(recipient, tmp_path, "retry.json")
