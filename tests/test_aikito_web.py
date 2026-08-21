import errno
import importlib.machinery
import importlib.util
import json
import subprocess
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "bin"))

from aikito_web import ConsoleData, make_handler  # noqa: E402


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
        (self.root / "agents.toml").write_text(
            """[agents.test]
display_name = "Test Agent"
instruction_path = ".test/AGENTS.md"
skills_path = ".test/skills"

[agents.test.mcp]
config_path = ".test/mcp.toml"
config_format = "toml"
name_style = "verbatim"
""",
            encoding="utf-8",
        )
        (self.root / "subagents.toml").write_text("", encoding="utf-8")
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
        self.web = self.root / "web"
        self.web.mkdir()
        (self.web / "index.html").write_text("console", encoding="utf-8")
        data = ConsoleData(self.root, self.root, "test")
        self.server = ThreadingHTTPServer(
            ("127.0.0.1", 0), make_handler(data, self.web)
        )
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

    def render_markdown(self, source: str, wikilinks: dict) -> str:
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
                str(ROOT / "web" / "app.js"),
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

        skills = self.get_json("/api/skills")
        self.assertEqual(skills[0]["skill_name"], "sample")
        detail = self.get_json("/api/skills/sample")
        self.assertEqual(detail["content"], "# Sample\n")

        instructions = self.get_json("/api/instructions")
        self.assertEqual([item["name"] for item in instructions], ["global", "demo"])
        project_instructions = self.get_json("/api/instructions/demo")
        self.assertEqual(project_instructions["scope"], "Project: demo")
        self.assertEqual(project_instructions["content"], "# Demo instructions\n")

    def test_redacts_sensitive_mcp_values(self) -> None:
        detail = self.get_json("/api/mcps/sample")
        self.assertEqual(detail["content"]["headers"]["Authorization"], "[configured]")
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


class WebCommandParserTest(unittest.TestCase):
    def test_web_command_options(self) -> None:
        loader = importlib.machinery.SourceFileLoader(
            "aikito_web_cli", str(ROOT / "bin" / "aikito")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)

        args = module.build_parser().parse_args(["web", "--port", "0", "--no-open"])
        self.assertEqual(args.port, 0)
        self.assertTrue(args.no_open)
        self.assertEqual(args.func, module.cmd_web)

    def test_web_command_reports_port_conflict_without_traceback(self) -> None:
        loader = importlib.machinery.SourceFileLoader(
            "aikito_web_cli_port_conflict", str(ROOT / "bin" / "aikito")
        )
        spec = importlib.util.spec_from_loader(loader.name, loader)
        module = importlib.util.module_from_spec(spec)
        loader.exec_module(module)
        args = module.build_parser().parse_args(["web", "--port", "8765"])

        error = OSError(errno.EADDRINUSE, "Address already in use")
        with mock.patch.object(module, "serve_console", side_effect=error):
            with mock.patch("sys.stderr") as stderr:
                with self.assertRaises(SystemExit) as exit_error:
                    module.cmd_web(args)

        self.assertEqual(exit_error.exception.code, 1)
        output = "".join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn("[ERROR] Port 8765 is already in use.", output)
        self.assertIn("http://127.0.0.1:8765", output)
        self.assertIn("aikito web --port 0", output)


if __name__ == "__main__":
    unittest.main()
