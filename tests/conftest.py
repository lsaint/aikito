"""Opt-in platform isolation for business tests; smoke tests use real OS ACLs."""

import sys

import pytest

from aikito import project_approval, skill_state
from aikito.workspace import device_keys, recipient_keys, recovery_codes


@pytest.fixture
def isolated_windows_key_permissions(monkeypatch):
    if sys.platform != "win32":
        return
    for module in (device_keys, recipient_keys, recovery_codes):
        monkeypatch.setattr(
            module, "check_credential_permissions", lambda path: (True, "private")
        )
        monkeypatch.setattr(module, "secure_file_permissions", lambda path: True)
        monkeypatch.setattr(module, "secure_directory_permissions", lambda path: True)


@pytest.fixture
def isolated_windows_state_permissions(monkeypatch, request):
    # Benchmarks retain the real ACL path so measurements include OS overhead.
    if sys.platform != "win32" or request.node.get_closest_marker("benchmark"):
        return
    monkeypatch.setattr(
        skill_state, "check_directory_permissions", lambda path: (True, "private")
    )
    monkeypatch.setattr(skill_state, "secure_directory_permissions", lambda path: True)
    for module in (skill_state, project_approval):
        monkeypatch.setattr(module, "secure_file_permissions", lambda path: True)
