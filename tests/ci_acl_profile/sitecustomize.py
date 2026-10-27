"""Count permission-tool subprocesses in explicitly profiled CI smoke commands."""

import json
import os
import subprocess
import time
from pathlib import Path

_profile = os.environ.get("AIKITO_CI_ACL_PROFILE")
_run = subprocess.run
_tools = {"whoami", "powershell", "icacls"}


def _profiled_run(args, *positional, **keywords):
    tool = (
        Path(os.fsdecode(args[0])).stem.lower()
        if isinstance(args, (list, tuple)) and args
        else ""
    )
    if tool not in _tools:
        return _run(args, *positional, **keywords)
    started = time.monotonic()
    try:
        return _run(args, *positional, **keywords)
    finally:
        try:
            with open(_profile, "a", encoding="utf-8") as stream:
                stream.write(
                    json.dumps({"tool": tool, "seconds": time.monotonic() - started})
                    + "\n"
                )
        except OSError:
            pass


if _profile:
    subprocess.run = _profiled_run
