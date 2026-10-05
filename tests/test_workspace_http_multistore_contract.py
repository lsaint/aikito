"""Multi-store isolation and durable restart are external implementation contracts."""

import os
import shlex
import sys
from pathlib import Path

import pytest

from workspace_external_contract import (
    STORES,
    access_rejections,
    concurrent_cas,
    isolation,
    restart,
)
from workspace_http_remote import ExternalServer


@pytest.fixture
def service(tmp_path, monkeypatch):
    template = os.environ.get("AIKITO_REMOTE_TEST_SERVER_CMD")
    if template and "{config}" not in template:
        pytest.skip("Multi-store contract requires {config} in the command")
    if not template:
        monkeypatch.setenv(
            "AIKITO_REMOTE_TEST_SERVER_CMD",
            shlex.join(
                [
                    sys.executable,
                    str(Path(__file__).with_name("http_remote_server.py")),
                    "--root",
                    "{root}",
                    "--test-config",
                    "{config}",
                ]
            ),
        )
    with ExternalServer(tmp_path / "center with spaces", STORES) as server:
        yield server


def test_multi_store_isolation(service):
    isolation(service)


def test_access_rejections_precede_body_and_existence(service):
    access_rejections(service)


def test_restart_preserves_identity_receipt_and_cas(service):
    restart(service)


def test_concurrent_commits_accept_exactly_one(service):
    concurrent_cas(service)
