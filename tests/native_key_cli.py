"""Test-only CLI entry using an explicitly isolated macOS keychain."""

import os
from pathlib import Path

from aikito.cli import main
from aikito.workspace import device_keys
from aikito.workspace.native_keys import NativeKeyStorage

if __name__ == "__main__":
    path = Path(os.environ["AIKITO_NATIVE_TEST_KEYCHAIN"])
    assert path.is_absolute() and path.name == "native-test.keychain-db"
    device_keys.NativeKeyStorage = lambda: NativeKeyStorage(keychain=path)
    main()
