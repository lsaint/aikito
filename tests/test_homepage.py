"""Check homepage links that MkDocs cannot validate inside a Jinja template."""

import os
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit


ROOT = Path(__file__).resolve().parents[1]


class PageLinks(HTMLParser):
    def __init__(self, source):
        super().__init__()
        self.links = []
        self.ids = set()
        self.feed(source)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.add(attrs["id"])
        for key in ("href", "src"):
            if key in attrs:
                self.links.append(attrs[key])


class HomepageTests(unittest.TestCase):
    def test_readmes_reference_the_bundled_aikito_skill(self):
        skill_path = "src/aikito/templates/skills/aikito/SKILL.md"
        self.assertTrue((ROOT / skill_path).is_file())
        for readme_name in ("README.md", "README.zh-CN.md"):
            readme = (ROOT / readme_name).read_text(encoding="utf-8")
            with self.subTest(readme=readme_name):
                self.assertIn(skill_path, readme)

    def test_homepage_routes_existing_configuration_through_adoption(self):
        template = (ROOT / "docs/overrides/home.html").read_text(encoding="utf-8")
        self.assertIn("aikito adopt", template)
        self.assertIn("after init and before sync", template)

    @unittest.skipUnless(
        (ROOT / "docs/overrides/home.html").is_file(), "Requires docs/ directory"
    )
    def test_template_routes_exist(self):
        template = (ROOT / "docs/overrides/home.html").read_text(encoding="utf-8")
        routes = re.findall(r"{{\s*'([^']+)'\s*\|\s*url\s*}}", template)
        self.assertTrue(routes)
        for route in routes:
            path = urlsplit(route).path
            if path == ".":
                continue
            target = ROOT / "docs" / path
            if path.endswith("/"):
                target = target.with_suffix(".md")
            with self.subTest(route=route):
                self.assertTrue(target.is_file(), target)

    @unittest.skipUnless(
        os.environ.get("AIKITO_DOCS_SITE"), "Requires a built documentation site"
    )
    def test_built_homepage_links_and_fragments(self):
        site = Path(os.environ["AIKITO_DOCS_SITE"]).resolve()
        source = (site / "index.html").read_text(encoding="utf-8")
        self.assertNotIn("{{", source)
        page = PageLinks(source)
        for link in page.links:
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc:
                continue
            with self.subTest(link=link):
                target = site / unquote(parsed.path)
                if target.is_dir():
                    target /= "index.html"
                self.assertTrue(target.is_file(), target)
                if parsed.fragment:
                    ids = PageLinks(target.read_text(encoding="utf-8")).ids
                    self.assertIn(unquote(parsed.fragment), ids)
        guide = (site / "guide/index.html").read_text(encoding="utf-8")
        self.assertIn('id="start-with-one-project"', guide)
        self.assertIn('id="find-a-specific-operation"', guide)
