"""Directory discovery never discloses resource bodies or bypasses recipient trust."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from aikito.workspace import access_catalog as access
from aikito.workspace.remote_access import RemoteBindingError


def test_catalog_lists_only_supported_collections(tmp_path):
    project = tmp_path / "projects/demo"
    project.mkdir(parents=True)
    (project / "agent.toml").write_text('secret = "must-not-be-published"')
    result = access.collections(tmp_path)
    assert result == [
        "global/instructions",
        "global/memory",
        "skills",
        "subagents",
        "inbox",
        "project/demo/instructions",
        "project/demo/memory",
    ]
    assert not any(
        "secret" in name or name in ("mcps", "agents", "config", "skills.toml")
        for name in result
    )


def test_untrusted_recipient_cannot_receive_directory(tmp_path, monkeypatch):
    monkeypatch.setattr(
        access, "target", lambda _: (object(), "https://untrusted.example", "main")
    )
    monkeypatch.setattr(
        access,
        "require_pinned_recipient",
        Mock(side_effect=ValueError("fixture untrusted recipient")),
    )
    transport = Mock()
    monkeypatch.setattr(access, "exchange", transport)
    with pytest.raises(ValueError):
        access.publish_catalog(tmp_path, tmp_path)
    transport.assert_not_called()


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://remote.example/v1/stores/main/remote",
        "https://remote.example/v1/stores/main/remote?token=secret",
        "https://remote.example/other",
    ],
)
def test_only_exact_bound_https_store_can_poll(endpoint, tmp_path, monkeypatch):
    binding = SimpleNamespace(endpoint=endpoint)
    monkeypatch.setattr(access, "load_remote_binding", lambda _: binding)
    with pytest.raises((ValueError, RemoteBindingError)):
        access.target(tmp_path)
