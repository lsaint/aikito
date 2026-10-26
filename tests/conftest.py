"""Opt-in platform isolation for business tests; smoke tests use real OS ACLs."""

import sys

import pytest

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
