"""Interactive sync progress clears before results, failures and interruption."""

import io
import os
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from aikito.remote_cli import cmd_remote_sync


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.mark.parametrize("interactive", [False, True])
@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_sync_progress_output(tmp_path, interactive, failure):
    stdout = Terminal() if interactive else io.StringIO()
    stderr = io.StringIO()
    plan = SimpleNamespace(
        blocked=0, conflicts=(), blocked_items=(), stop_reason=None, deferred=()
    )
    binding = SimpleNamespace(
        endpoint="https://example.com", auth=SimpleNamespace(env="TOKEN")
    )
    args = SimpleNamespace(workspace=str(tmp_path), dry_run=True, verbose=True)

    def synchronize(workspace, *, dry_run, home, on_progress):
        assert dry_run
        if interactive:
            on_progress("Opening remote connection")
            assert "Opening remote connection" in stdout.getvalue()
            on_progress("Comparing local resources")
        else:
            assert on_progress is None
        if failure:
            raise failure("Stopped")
        return plan

    with (
        patch("aikito.remote_cli.is_recognized_workspace", return_value=True),
        patch("aikito.remote_cli.get_workspace_remote_binding", return_value=binding),
        patch("aikito.remote_cli.sync_workspace_remote", side_effect=synchronize),
        patch(
            "aikito.remote_cli.render_remote_sync_plan", return_value="Remote sync plan"
        ),
        patch(
            "aikito.remote_cli.shutil.get_terminal_size",
            return_value=os.terminal_size((50, 20)),
        ),
        patch("sys.stdout", stdout),
        patch("sys.stderr", stderr),
    ):
        if failure:
            expected = KeyboardInterrupt if failure is KeyboardInterrupt else SystemExit
            with pytest.raises(expected):
                cmd_remote_sync(args)
        else:
            cmd_remote_sync(args)

    output = stdout.getvalue()
    if interactive:
        assert "Loading:" not in output
        assert "Comparing local resources..." in output
        assert "\n" not in output.split("Remote sync plan")[0]
        assert output.endswith("\r\033[K" if failure else "\r\033[KRemote sync plan\n")
        for line in output.split("\r"):
            assert len(line.split("\033[K")[0]) <= 49
    else:
        assert output == ("" if failure else "Remote sync plan\n")
    if failure is RuntimeError:
        assert "[ERROR]" in stderr.getvalue()
