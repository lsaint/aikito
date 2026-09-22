import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from aikito import update_notifier
from aikito.config import AikitoConfig, UpdateConfig


class SemverTest(unittest.TestCase):
    def test_parse_semver_valid(self) -> None:
        self.assertEqual(update_notifier.parse_semver("1.39.0"), (1, 39, 0))
        self.assertEqual(update_notifier.parse_semver("v1.39.0"), (1, 39, 0))
        self.assertEqual(update_notifier.parse_semver("2.0.1-rc1"), (2, 0, 1))
        self.assertEqual(update_notifier.parse_semver("v3.1.0+build42"), (3, 1, 0))

    def test_parse_semver_invalid(self) -> None:
        self.assertIsNone(update_notifier.parse_semver(""))
        self.assertIsNone(update_notifier.parse_semver("invalid"))
        self.assertIsNone(update_notifier.parse_semver(None))  # type: ignore

    def test_is_newer_version(self) -> None:
        self.assertTrue(update_notifier.is_newer_version("1.40.0", "1.39.0"))
        self.assertTrue(update_notifier.is_newer_version("1.39.1", "1.39.0"))
        self.assertTrue(update_notifier.is_newer_version("2.0.0", "1.99.99"))
        self.assertFalse(update_notifier.is_newer_version("1.39.0", "1.39.0"))
        self.assertFalse(update_notifier.is_newer_version("1.38.0", "1.39.0"))
        self.assertFalse(update_notifier.is_newer_version("invalid", "1.39.0"))


class CacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.tmp.name) / "cache" / "version_check.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_read_cache_non_existent(self) -> None:
        self.assertIsNone(update_notifier.read_cache(self.cache_file))

    def test_write_and_read_cache(self) -> None:
        data = {"last_checked": 1234567.8, "latest_version": "1.40.0"}
        update_notifier.write_cache(data, self.cache_file)
        self.assertTrue(self.cache_file.is_file())

        loaded = update_notifier.read_cache(self.cache_file)
        self.assertEqual(loaded, data)

    def test_read_corrupted_cache(self) -> None:
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        self.cache_file.write_text("not json content", encoding="utf-8")
        self.assertIsNone(update_notifier.read_cache(self.cache_file))

    def test_get_cache_path_override(self) -> None:
        with patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}):
            self.assertEqual(update_notifier.get_cache_path(), self.cache_file)

    def test_is_cache_fresh(self) -> None:
        now = 1000000.0
        fresh_cache = {"last_checked": now - 3600, "latest_version": "1.40.0"}
        stale_cache = {"last_checked": now - 90000, "latest_version": "1.40.0"}
        missing_ver = {"last_checked": now - 100}
        self.assertTrue(
            update_notifier.is_cache_fresh(fresh_cache, check_interval=86400, now=now)
        )
        self.assertFalse(
            update_notifier.is_cache_fresh(stale_cache, check_interval=86400, now=now)
        )
        self.assertFalse(
            update_notifier.is_cache_fresh(missing_ver, check_interval=86400, now=now)
        )
        self.assertFalse(
            update_notifier.is_cache_fresh(None, check_interval=86400, now=now)
        )

    def test_get_check_interval_falls_back_for_invalid_value(self) -> None:
        with patch.dict(
            os.environ, {"AIKITO_UPDATE_CHECK_INTERVAL": "invalid"}, clear=False
        ):
            self.assertEqual(
                update_notifier.get_check_interval(),
                float(update_notifier.DEFAULT_CHECK_INTERVAL),
            )


class UpgradeCommandTest(unittest.TestCase):
    def test_upgrade_command_homebrew(self) -> None:
        with patch.object(
            sys, "executable", "/opt/homebrew/Cellar/aikito/1.39.0/bin/python"
        ):
            cmd = update_notifier.get_upgrade_command()
            self.assertEqual(cmd, "brew upgrade lsaint/tap/aikito")

    def test_upgrade_command_uv(self) -> None:
        with patch.object(
            sys, "executable", "/home/user/.local/share/uv/tools/aikito/bin/python"
        ):
            cmd = update_notifier.get_upgrade_command()
            self.assertEqual(cmd, "uv tool upgrade aikito")

    def test_upgrade_command_pipx(self) -> None:
        with patch.object(
            sys, "executable", "/home/user/.local/share/pipx/venvs/aikito/bin/python"
        ):
            cmd = update_notifier.get_upgrade_command()
            self.assertEqual(cmd, "pipx upgrade aikito")

    def test_upgrade_command_fallback(self) -> None:
        with patch.object(sys, "executable", "/usr/bin/python3"):
            with patch(
                "pathlib.Path.resolve",
                return_value=Path("/usr/lib/python3/dist-packages/aikito"),
            ):
                cmd = update_notifier.get_upgrade_command()
                self.assertEqual(cmd, "uv tool upgrade aikito")


class SuppressionTest(unittest.TestCase):
    def test_force_update_check_bypasses_all(self) -> None:
        with patch.dict(os.environ, {"AIKITO_FORCE_UPDATE_CHECK": "1"}):
            self.assertTrue(update_notifier.should_check_update())

    def test_not_atty_suppresses(self) -> None:
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = False
        with patch("sys.stderr", mock_stderr), patch.dict(os.environ, {}, clear=True):
            self.assertFalse(update_notifier.should_check_update())

    def test_env_var_suppresses(self) -> None:
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        with patch("sys.stderr", mock_stderr):
            with patch.dict(os.environ, {"AIKITO_NO_UPDATE_NOTIFIER": "1"}):
                self.assertFalse(update_notifier.should_check_update())
            with patch.dict(os.environ, {"NO_UPDATE_NOTIFIER": "1"}):
                self.assertFalse(update_notifier.should_check_update())

    def test_ci_suppresses(self) -> None:
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        with patch("sys.stderr", mock_stderr):
            with patch.dict(os.environ, {"CI": "true"}):
                self.assertFalse(update_notifier.should_check_update())
            with patch.dict(os.environ, {"GITHUB_ACTIONS": "true"}):
                self.assertFalse(update_notifier.should_check_update())

    def test_exempt_commands_suppress(self) -> None:
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        with patch("sys.stderr", mock_stderr), patch.dict(os.environ, {}, clear=True):
            for cmd in ("completion", "path"):
                self.assertFalse(update_notifier.should_check_update(command=cmd))

    def test_config_update_disabled_suppresses(self) -> None:
        mock_stderr = MagicMock()
        mock_stderr.isatty.return_value = True
        with patch("sys.stderr", mock_stderr), patch.dict(os.environ, {}, clear=True):
            with patch(
                "aikito.update_notifier.is_development_mode", return_value=False
            ):
                with patch(
                    "aikito.config.load_workspace_config",
                    return_value=AikitoConfig(update=UpdateConfig(check=False)),
                ):
                    self.assertFalse(
                        update_notifier.should_check_update(
                            aikito_dir=Path("/dummy/workspace")
                        )
                    )


class FetchTest(unittest.TestCase):
    def test_fetch_pypi_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps(
            {"info": {"version": "1.40.0"}}
        ).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            ver = update_notifier.fetch_latest_version_pypi()
            self.assertEqual(ver, "1.40.0")

    def test_fetch_github_success(self) -> None:
        mock_resp = MagicMock()
        mock_resp.geturl.return_value = (
            "https://github.com/lsaint/aikito/releases/tag/v1.41.0"
        )
        mock_opener = MagicMock()
        mock_opener.open.return_value = mock_resp
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.build_opener", return_value=mock_opener):
            ver = update_notifier.fetch_latest_version_github()
            self.assertEqual(ver, "1.41.0")

    def test_fetch_latest_version_fallback(self) -> None:
        with patch(
            "aikito.update_notifier.fetch_latest_version_pypi", return_value=None
        ):
            with patch(
                "aikito.update_notifier.fetch_latest_version_github",
                return_value="1.42.0",
            ):
                ver = update_notifier.fetch_latest_version()
                self.assertEqual(ver, "1.42.0")


class NotificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.tmp.name) / "version_check.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_check_and_notify_prints_when_newer(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time(), "latest_version": "9.9.9"},
            self.cache_file,
        )
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.should_check_update", return_value=True),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.check_and_notify_update(
                current_version="1.0.0",
                output=out,
            )

        rendered = out.getvalue()
        self.assertIn(
            "[NOTICE] A new version of Aikito is available: 1.0.0 -> 9.9.9", rendered
        )
        self.assertIn("To upgrade, run: uv tool upgrade aikito", rendered)

    def test_check_and_notify_silent_when_up_to_date(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time(), "latest_version": "1.0.0"},
            self.cache_file,
        )
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.should_check_update", return_value=True),
        ):
            update_notifier.check_and_notify_update(
                current_version="1.0.0",
                output=out,
            )

        self.assertEqual(out.getvalue(), "")

    def test_check_and_notify_triggers_spawn_when_stale(self) -> None:
        # Cache checked 2 days ago
        update_notifier.write_cache(
            {"last_checked": time.time() - 200000, "latest_version": "1.0.0"},
            self.cache_file,
        )
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.should_check_update", return_value=True),
            patch("aikito.update_notifier.spawn_background_check") as mock_spawn,
        ):
            update_notifier.check_and_notify_update(
                current_version="1.0.0",
                output=out,
            )
            mock_spawn.assert_called_once()

    def test_check_and_notify_suppressed_by_workspace_config(self) -> None:
        ws_dir = Path(self.tmp.name) / "ws"
        ws_dir.mkdir(parents=True)
        (ws_dir / "config.toml").write_text(
            "[update]\ncheck = false\n", encoding="utf-8"
        )
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("sys.stderr.isatty", return_value=True),
            patch("aikito.update_notifier.is_development_mode", return_value=False),
            patch("aikito.update_notifier.spawn_background_check") as mock_spawn,
        ):
            update_notifier.check_and_notify_update(
                current_version="1.0.0",
                aikito_dir=ws_dir,
                output=out,
            )
            self.assertEqual(out.getvalue(), "")
            mock_spawn.assert_not_called()


class CmdVersionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.cache_file = Path(self.tmp.name) / "version_check.json"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_cmd_version_default(self) -> None:
        args = MagicMock(check=False, force=False, json=False)
        out = io.StringIO()
        with patch("sys.stdout", out):
            update_notifier.cmd_version(args)

        self.assertEqual(
            out.getvalue().strip(), f"aikito {update_notifier.__version__}"
        )

    def test_cmd_version_check_up_to_date(self) -> None:
        args = MagicMock(check=True, force=False, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch(
                "aikito.update_notifier.fetch_latest_version",
                return_value=update_notifier.__version__,
            ),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            update_notifier.cmd_version(args)

        self.assertIn(f"aikito {update_notifier.__version__}", out.getvalue())
        self.assertIn(
            f"Aikito is up to date (version {update_notifier.__version__}).",
            err.getvalue(),
        )

    def test_cmd_version_check_has_update(self) -> None:
        args = MagicMock(check=True, force=False, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.fetch_latest_version", return_value="99.0.0"),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.cmd_version(args)

        self.assertIn(f"aikito {update_notifier.__version__}", out.getvalue())
        self.assertIn("[NOTICE] A new version of Aikito is available:", err.getvalue())
        self.assertIn("99.0.0", err.getvalue())

    def test_cmd_version_json(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time(), "latest_version": "99.0.0"},
            self.cache_file,
        )
        args = MagicMock(check=False, force=False, json=True)
        out = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("sys.stdout", out),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.cmd_version(args)

        result = json.loads(out.getvalue())
        self.assertEqual(result["version"], update_notifier.__version__)
        self.assertEqual(result["latest_version"], "99.0.0")
        self.assertTrue(result["update_available"])
        self.assertEqual(result["upgrade_command"], "uv tool upgrade aikito")

    def test_cmd_version_check_network_failed(self) -> None:
        args = MagicMock(check=True, force=False, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.fetch_latest_version", return_value=None),
            patch("sys.stdout", out),
            patch("sys.stderr", err),
        ):
            update_notifier.cmd_version(args)

        self.assertIn(f"aikito {update_notifier.__version__}", out.getvalue())
        self.assertIn("[WARNING] Could not check for updates", err.getvalue())

    def test_cmd_version_check_reuses_fresh_cache(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time(), "latest_version": "99.0.0"},
            self.cache_file,
        )
        args = MagicMock(check=True, force=False, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch("aikito.update_notifier.fetch_latest_version") as mock_fetch,
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.cmd_version(args)
            mock_fetch.assert_not_called()
            self.assertIn("99.0.0", err.getvalue())

    def test_cmd_version_check_refetches_when_stale(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time() - 200000, "latest_version": "1.0.0"},
            self.cache_file,
        )
        args = MagicMock(check=True, force=False, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch(
                "aikito.update_notifier.fetch_latest_version", return_value="100.0.0"
            ) as mock_fetch,
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.cmd_version(args)
            mock_fetch.assert_called_once()
            self.assertIn("100.0.0", err.getvalue())

    def test_cmd_version_force_refetches_even_when_fresh(self) -> None:
        update_notifier.write_cache(
            {"last_checked": time.time(), "latest_version": "99.0.0"},
            self.cache_file,
        )
        args = MagicMock(check=True, force=True, json=False)
        out = io.StringIO()
        err = io.StringIO()
        with (
            patch.dict(os.environ, {"AIKITO_UPDATE_CACHE_FILE": str(self.cache_file)}),
            patch(
                "aikito.update_notifier.fetch_latest_version", return_value="100.0.0"
            ) as mock_fetch,
            patch("sys.stdout", out),
            patch("sys.stderr", err),
            patch(
                "aikito.update_notifier.get_upgrade_command",
                return_value="uv tool upgrade aikito",
            ),
        ):
            update_notifier.cmd_version(args)
            mock_fetch.assert_called_once()
            self.assertIn("100.0.0", err.getvalue())


if __name__ == "__main__":
    unittest.main()
