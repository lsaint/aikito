import errno
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

from layout_helpers import write_agents

from aikito.web_console import ConsoleData, ConsoleServer, make_handler

ROOT = Path(__file__).resolve().parents[1]


class WebConsoleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        (self.root / "global").mkdir()
        (self.root / "global" / "AGENTS.md").write_text(
            "# Instructions\n", encoding="utf-8"
        )
        (self.root / "skills" / "sample").mkdir(parents=True)
        (self.root / "skills" / "sample" / "SKILL.md").write_text(
            "# Sample\n", encoding="utf-8"
        )
        (self.root / "skills.toml").write_text(
            'skills = ["sample"]\n', encoding="utf-8"
        )
        write_agents(
            self.root,
            """[agents.test]
display_name = "Test Agent"
instruction_path = ".test/AGENTS.md"
skills_path = ".test/skills"

[agents.test.mcp]
config_path = ".test/mcp.toml"
config_format = "toml"
name_style = "verbatim"
""",
        )
        (self.root / "subagents").mkdir(exist_ok=True)
        (self.root / "mcps").mkdir()
        (self.root / "mcps" / "sample.toml").write_text(
            'transport = "remote"\nurl = "https://example.test/mcp"\n'
            'agents = ["test"]\n[headers]\nAuthorization = "PRIVATE_TOKEN"\n',
            encoding="utf-8",
        )
        notes = self.root / "memory" / "notes"
        notes.mkdir(parents=True)
        (self.root / "memory" / "index.md").write_text(
            "[[current]]\n[[target]]\n", encoding="utf-8"
        )
        (notes / "current.md").write_text(
            "See [[target|Target note]] and [[missing]].\n", encoding="utf-8"
        )
        (notes / "target.md").write_text("# Target\n", encoding="utf-8")
        project = self.root / "projects" / "demo"
        project.mkdir(parents=True)
        (project / "AGENTS.md").write_text("# Demo instructions\n", encoding="utf-8")
        (project / "agent.toml").write_text(
            f'path = "{self.root.as_posix()}"\ndescription = "Demo service"\n',
            encoding="utf-8",
        )

        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("console", encoding="utf-8")
        (self.web / "marked.umd.js").write_text("/* marked */", encoding="utf-8")
        data = ConsoleData(self.root, self.root, "test")
        self.server = ConsoleServer(("127.0.0.1", 0), make_handler(data, self.web))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}"

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temporary_directory.cleanup()

    def get_json(self, path: str):
        with urllib.request.urlopen(self.base_url + path) as response:
            return json.load(response)

    def test_pending_requests_are_read_only(self):
        expected = {"origin": "https://recipient.example", "requests": []}
        with mock.patch(
            "aikito.console_access.ConsoleAccess.pending", return_value=expected
        ) as pending:
            self.assertEqual(self.get_json("/api/access"), expected)
        pending.assert_called_once_with()

    def test_access_pending_service_unavailable_returns_503(self):
        with mock.patch(
            "aikito.console_access.ConsoleAccess.pending",
            side_effect=RuntimeError("backend down"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.get_json("/api/access")
            self.assertEqual(error.exception.code, 503)
            self.assertEqual(
                json.loads(error.exception.read().decode("utf-8")),
                {"error": "Authorization service is unavailable"},
            )

    def test_client_disconnect_during_json_response_sets_close_connection(self):
        handler_cls = make_handler(ConsoleData(self.root, self.root, "test"), self.web)
        handler = handler_cls.__new__(handler_cls)
        handler.server = mock.MagicMock(server_port=8765)
        handler.wfile = mock.MagicMock()
        handler.wfile.write.side_effect = BrokenPipeError(errno.EPIPE, "Broken pipe")
        handler.headers = {"Host": "127.0.0.1:8765"}
        handler.send_response = mock.MagicMock()
        handler.send_header = mock.MagicMock()
        handler.end_headers = mock.MagicMock()
        handler.close_connection = False

        handler._json({"test": "value"})
        self.assertTrue(handler.close_connection)

    def test_console_server_suppresses_connection_errors(self):
        server = ConsoleServer.__new__(ConsoleServer)
        with mock.patch("sys.stderr") as stderr:
            try:
                raise BrokenPipeError(errno.EPIPE, "Broken pipe")
            except BrokenPipeError:
                server.handle_error(mock.MagicMock(), ("127.0.0.1", 12345))
            self.assertEqual(stderr.write.call_count, 0)

            try:
                raise ConnectionResetError(errno.ECONNRESET, "Connection reset by peer")
            except ConnectionResetError:
                server.handle_error(mock.MagicMock(), ("127.0.0.1", 12345))
            self.assertEqual(stderr.write.call_count, 0)

            with mock.patch.object(ThreadingHTTPServer, "handle_error") as super_handle:
                try:
                    raise ValueError("unexpected boom")
                except ValueError:
                    server.handle_error(mock.MagicMock(), ("127.0.0.1", 12345))
                super_handle.assert_called_once()

    def test_mutations_require_local_origin_and_console_token(self):
        token = self.get_json("/api/console-session")["token"]
        payload = json.dumps({"confirmation": "fixture"}).encode()
        path = "/api/access/" + "d" * 64 + "/confirm"
        for origin, secret in [
            ("https://attacker.example", token),
            (self.base_url, "wrong"),
            (None, token),
        ]:
            headers = {
                "Content-Type": "application/json",
                "X-Aikito-Console-Token": secret,
            }
            if origin:
                headers["Origin"] = origin
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(
                    urllib.request.Request(
                        self.base_url + path, data=payload, headers=headers
                    )
                )
            self.assertEqual(error.exception.code, 403)
        with mock.patch(
            "aikito.console_access.ConsoleAccess.perform",
            return_value={"status": "active"},
        ) as perform:
            request = urllib.request.Request(
                self.base_url + path,
                data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.base_url,
                    "X-Aikito-Console-Token": token,
                },
            )
            with urllib.request.urlopen(request) as response:
                self.assertEqual(json.load(response), {"status": "active"})
            perform.assert_called_once_with(
                "d" * 64, "confirm", {"confirmation": "fixture"}
            )

    def test_rebinding_host_and_cross_origin_session_are_rejected(self):
        for headers in [
            {"Host": "attacker.example"},
            {"Origin": "https://attacker.example"},
        ]:
            request = urllib.request.Request(
                self.base_url + "/api/console-session", headers=headers
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertEqual(error.exception.code, 403)

    def test_access_errors_do_not_expose_provider_secrets(self):
        token = self.get_json("/api/console-session")["token"]
        with mock.patch(
            "aikito.console_access.ConsoleAccess.perform",
            side_effect=RuntimeError("PRIVATE_TOKEN"),
        ):
            request = urllib.request.Request(
                self.base_url + "/api/access/" + "d" * 64 + "/confirm",
                data=b'{"confirmation":"fixture"}',
                headers={
                    "Content-Type": "application/json",
                    "Origin": self.base_url,
                    "X-Aikito-Console-Token": token,
                },
            )
            with self.assertRaises(urllib.error.HTTPError) as error:
                urllib.request.urlopen(request)
            self.assertNotIn("PRIVATE_TOKEN", error.exception.read().decode())
        with mock.patch(
            "aikito.console_access.ConsoleAccess.pending",
            side_effect=RuntimeError("PRIVATE_TOKEN"),
        ):
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.get_json("/api/access")
            self.assertNotIn("PRIVATE_TOKEN", error.exception.read().decode())

    def render_markdown(self, source: str, wikilinks: dict) -> str:
        if shutil.which("node") is None:
            raise unittest.SkipTest("Node.js is not installed on this system")
        script = """
const fs = require("fs");
const app = fs.readFileSync(process.argv[1], "utf8");
const document = {querySelector: () => ({})};
eval(app.split("function show")[0] + `
process.stdout.write(markdown(JSON.parse(process.argv[2]), JSON.parse(process.argv[3])));`);
"""
        return subprocess.run(
            [
                "node",
                "-e",
                script,
                str(ROOT / "src" / "aikito" / "web" / "app.js"),
                json.dumps(source),
                json.dumps(wikilinks),
            ],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    def test_serves_static_console_and_resource_api(self) -> None:
        with urllib.request.urlopen(self.base_url + "/") as response:
            self.assertEqual(response.read(), b"console")

        with urllib.request.urlopen(self.base_url + "/marked.umd.js") as response:
            self.assertEqual(response.read(), b"/* marked */")
            self.assertIn("javascript", response.headers.get("Content-Type", ""))

        skills = self.get_json("/api/skills")
        self.assertEqual(skills[0]["skill_name"], "sample")
        detail = self.get_json("/api/skills/sample")
        self.assertEqual(detail["content"], "# Sample\n")

        instructions = self.get_json("/api/instructions")
        self.assertEqual([item["name"] for item in instructions], ["global", "demo"])
        project_instructions = self.get_json("/api/instructions/demo")
        self.assertEqual(project_instructions["scope"], "Project: demo")
        self.assertEqual(project_instructions["content"], "# Demo instructions\n")

        project = self.get_json("/api/projects/demo")
        self.assertEqual(project["content"]["description"], "Demo service")

    def test_overview_serializes_memory_update_dates(self) -> None:
        # ConsoleData reads workspace files lazily per request, so give
        # this test a valid empty subagent registry without touching setUp.
        (self.root / "subagents").mkdir(exist_ok=True)
        overview = self.get_json("/api/overview")
        self.assertEqual(overview["version"], "test")
        scopes = overview["memory_scopes"]
        self.assertTrue(scopes)
        updated_values = [scope["updated_on"] for scope in scopes]
        self.assertTrue(any(isinstance(value, str) for value in updated_values))
        for value in updated_values:
            if value is not None:
                self.assertRegex(value, r"^\d{4}-\d{2}-\d{2}$")

    def test_remote_status_endpoint_and_overview(self) -> None:
        overview = self.get_json("/api/overview")
        self.assertIn("remote", overview)
        self.assertIsNone(overview["remote"]["binding"])
        self.assertIsNone(overview["remote"]["store_id"])

        remote = self.get_json("/api/remote")
        self.assertIn("service", remote)
        self.assertIn("workspace", remote)
        self.assertIsNone(remote["store_id"])

    def test_remote_mutations_unbind(self) -> None:
        token = self.get_json("/api/console-session")["token"]
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "X-Aikito-Console-Token": token,
        }
        request = urllib.request.Request(
            self.base_url + "/api/remote/unbind",
            data=json.dumps({"force": True}).encode(),
            headers=headers,
        )
        with urllib.request.urlopen(request) as response:
            result = json.load(response)
            self.assertFalse(result["unbound"])
            self.assertIsNone(result["remote"]["binding"])

    def test_remote_mutations_bind_with_recovery_code(self) -> None:
        token = self.get_json("/api/console-session")["token"]
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "X-Aikito-Console-Token": token,
        }
        mock_binding = mock.MagicMock()
        mock_binding.endpoint = "https://example.com/v1/remote"
        mock_binding.sync_id = "11223344"
        mock_binding.status = "recovered"

        with mock.patch(
            "aikito.web_console.bind_workspace_remote", return_value=mock_binding
        ) as mock_bind:
            request = urllib.request.Request(
                self.base_url + "/api/remote/bind",
                data=json.dumps(
                    {
                        "endpoint": "https://example.com/v1/remote",
                        "recovery_code": "RC-1234-ABCD",
                    }
                ).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
                self.assertTrue(result["bound"])
                self.assertEqual(result["status"], "recovered")
                mock_bind.assert_called_once()
                self.assertEqual(
                    mock_bind.call_args.kwargs.get("recovery_code"),
                    "RC-1234-ABCD",
                )

    def test_remote_mutations_sync(self) -> None:
        token = self.get_json("/api/console-session")["token"]
        headers = {
            "Content-Type": "application/json",
            "Origin": self.base_url,
            "X-Aikito-Console-Token": token,
        }
        mock_plan = mock.MagicMock()
        mock_plan.revision = 5
        mock_plan.applied_items = ("item1",)
        mock_plan.changes = ("item1",)
        mock_plan.conflicts = ()

        with mock.patch(
            "aikito.web_console.sync_workspace_remote", return_value=mock_plan
        ):
            request = urllib.request.Request(
                self.base_url + "/api/remote/sync",
                data=json.dumps({"dry_run": False}).encode(),
                headers=headers,
            )
            with urllib.request.urlopen(request) as response:
                result = json.load(response)
                self.assertTrue(result["synced"])
                self.assertEqual(result["revision"], 5)
                self.assertEqual(result["applied_count"], 1)
                self.assertEqual(result["conflicts_count"], 0)

    def test_redacts_sensitive_mcp_values(self) -> None:
        detail = self.get_json("/api/mcps/sample")
        self.assertEqual(detail["content"]["headers"]["Authorization"], "<redacted>")
        self.assertNotIn("PRIVATE_TOKEN", json.dumps(detail))

    def test_resolves_existing_wikilinks_only(self) -> None:
        detail = self.get_json("/api/memory/Global/current")

        self.assertEqual(
            detail["wikilinks"]["target"],
            {"kind": "memory", "name": "Global/target"},
        )
        self.assertNotIn("missing", detail["wikilinks"])

    def test_markdown_matches_raw_wikilinks_before_escaping(self) -> None:
        target_name = 'Global/A & B\'s "notes"'
        rendered = self.render_markdown(
            '[[A & B\'s "notes"|Open <note>]]',
            {
                'A & B\'s "notes"': {
                    "kind": "memory",
                    "name": target_name,
                }
            },
        )

        self.assertIn('class="wikilink"', rendered)
        self.assertIn('data-name="Global/A &amp; B&#39;s &quot;notes&quot;"', rendered)
        self.assertIn("Open &lt;note&gt;", rendered)

    def test_markdown_escapes_url_attributes_after_matching(self) -> None:
        rendered = self.render_markdown(
            'Visit https://example.test/search?a=1&b="quoted" and <unsafe>.', {}
        )

        self.assertIn(
            'href="https://example.test/search?a=1&amp;b=&quot;quoted&quot;"',
            rendered,
        )
        self.assertIn(
            "https://example.test/search?a=1&amp;b=&quot;quoted&quot;</a>",
            rendered,
        )
        self.assertIn("and &lt;unsafe&gt;.", rendered)

    def test_markdown_renders_standard_markdown_links_with_hover_title(self) -> None:
        rendered = self.render_markdown(
            "Check [sample-resource](file:///workspace/sample/resource) and [Docs](https://example.com/doc?q=1&v=2).",
            {},
        )
        self.assertIn(
            '<span class="external-link" title="file:///workspace/sample/resource">sample-resource</span>',
            rendered,
        )
        self.assertIn(
            '<a class="external-link" href="https://example.com/doc?q=1&amp;v=2" title="https://example.com/doc?q=1&amp;v=2" target="_blank" rel="noopener noreferrer">Docs</a>',
            rendered,
        )

    def test_markdown_renders_frontmatter_as_borderless_table(self) -> None:
        rendered = self.render_markdown(
            "---\nname: obsidian-cli\ndescription: xxx\n---\n# Content\n---", {}
        )

        self.assertIn('<table class="markdown-frontmatter">', rendered)
        self.assertIn("<th>name</th><td>obsidian-cli</td>", rendered)
        self.assertIn("<th>description</th><td>xxx</td>", rendered)
        self.assertEqual(rendered.count('<hr class="markdown-divider">'), 1)
        self.assertNotIn("<p>name: obsidian-cli</p>", rendered)

    def test_markdown_renders_pipe_table(self) -> None:
        rendered = self.render_markdown(
            "| Scenario | MCP Tool |\n"
            "|---|---|\n"
            "| `*/browse/DM-XXXXX` | `getJiraIssue` |\n"
            "| Jira search | **searchJiraIssuesUsingJql** |",
            {},
        )

        self.assertIn('<table class="markdown-table">', rendered)
        self.assertIn(
            "<thead><tr><th>Scenario</th><th>MCP Tool</th></tr></thead>", rendered
        )
        self.assertIn('<span class="inline-code">*/browse/DM-XXXXX</span>', rendered)
        self.assertIn("<strong>searchJiraIssuesUsingJql</strong>", rendered)
        self.assertNotIn("<p>| Scenario | MCP Tool |</p>", rendered)

    def test_rejects_path_traversal(self) -> None:
        with self.assertRaises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(self.base_url + "/api/skills/../global")
        self.assertEqual(error.exception.code, 404)

    def test_package_web_assets_contain_marked(self) -> None:
        from aikito.web_console import _resolve_web_dir

        web_dir = _resolve_web_dir()
        self.assertTrue((web_dir / "marked.umd.js").is_file())
        self.assertTrue((web_dir / "index.html").is_file())
        self.assertTrue((web_dir / "app.js").is_file())
        self.assertTrue((web_dir / "styles.css").is_file())
        self.assertIn(
            "Confirm authorization",
            (web_dir / "resource-access.js").read_text(encoding="utf-8"),
        )


class WebCommandParserTest(unittest.TestCase):
    def test_web_command_options(self) -> None:
        from aikito import cli as module

        args = module.build_parser().parse_args(["web", "--port", "0", "--no-open"])
        self.assertEqual(args.port, 0)
        self.assertTrue(args.no_open)
        self.assertEqual(args.func, module.cmd_web)

    def test_web_command_reports_port_conflict_without_traceback(self) -> None:
        from aikito import cli as module

        args = module.build_parser().parse_args(["web", "--port", "8765"])

        error = OSError(errno.EADDRINUSE, "Address already in use")
        with (
            mock.patch.object(module, "serve_console", side_effect=error),
            mock.patch("sys.stderr") as stderr,
            self.assertRaises(SystemExit) as exit_error,
        ):
            module.cmd_web(args)

        self.assertEqual(exit_error.exception.code, 1)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("[ERROR] Port 8765 is already in use.", output)
        self.assertIn("http://127.0.0.1:8765", output)
        self.assertIn("aikito web --port 0", output)


if __name__ == "__main__":
    unittest.main()
