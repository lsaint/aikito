"""Test-only CLI driver: release-gate bypass is confined to isolated loopback fixtures."""

import os
import sys
from pathlib import Path
from urllib.parse import urlparse

from aikito import __version__
from aikito.cli import main
from aikito.workspace import encrypted_migration
from aikito.workspace.remote_binding import load_remote_binding

if __name__ == "__main__":
    index = sys.argv.index("--workspace")
    binding = load_remote_binding(Path(sys.argv[index + 1]))
    assert binding is not None
    assert urlparse(binding.endpoint).hostname in {"127.0.0.1", "::1"}
    assert os.environ.get(binding.auth.env) == "migration-smoke-only-token"
    encrypted_migration.MINIMUM_GUARD_RELEASE = __version__
    main()
