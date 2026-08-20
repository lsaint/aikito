"""Read-only local Web Console for an Aikito workspace."""

from __future__ import annotations

import dataclasses
import json
import mimetypes
import re
import threading
import tomllib
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit

from aikito_config import get_inbox_path, load_workspace_config
from aikito_diff import collect_drift_diffs
from aikito_doctor import run_doctor
from aikito_inbox import collect_inbox_rows
from aikito_mcp import load_agents
from aikito_project import collect_project_summaries
from aikito_status import (
    collect_mcp_details,
    collect_memory_notes_rows,
    collect_skills_rows,
    collect_subagent_details,
    get_status_report_data,
)

_SENSITIVE_KEY = re.compile(
    r"(token|secret|password|credential|authorization|api[_-]?key)", re.I
)


def _json_value(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return {
            field.name: _json_value(getattr(value, field.name))
            for field in dataclasses.fields(value)
        }
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_value(item) for item in value]
    return value


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _safe_child(root: Path, relative: str) -> Path:
    target = (root / relative).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise FileNotFoundError(relative)
    if not target.is_file():
        raise FileNotFoundError(relative)
    return target


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): "[configured]"
            if _SENSITIVE_KEY.search(str(key))
            else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


class ConsoleData:
    """Expose the workspace as a small, stable set of JSON resources."""

    def __init__(self, aikito_dir: Path, home: Path, version: str):
        self.aikito_dir = aikito_dir.resolve()
        self.home = home.resolve()
        self.version = version

    def overview(self) -> dict[str, Any]:
        status = get_status_report_data(self.aikito_dir, self.home)
        inbox = collect_inbox_rows(get_inbox_path(self.aikito_dir))
        projects = collect_project_summaries(self.aikito_dir, self.home)
        return {
            "workspace": str(self.aikito_dir),
            "version": self.version,
            "healthy": status.issues_count == 0,
            "counts": {
                "skills": status.total_skills_count,
                "mcps": status.total_mcp_count,
                "subagents": status.total_subagents_count,
                "memory": status.total_memory_notes,
                "inbox": len(inbox),
                "projects": len(projects),
                "agents": len(status.agents),
                "issues": status.issues_count,
            },
            "agents": _json_value(status.agents),
            "memory_scopes": _json_value(status.memories),
            "projects": _json_value(projects),
        }

    def _instruction_resources(self) -> dict[str, tuple[Path, str]]:
        resources: dict[str, tuple[Path, str]] = {}
        global_path = self.aikito_dir / "global" / "AGENTS.md"
        if global_path.is_file():
            resources["global"] = (global_path, "Global")

        projects = self.aikito_dir / "projects"
        if projects.is_dir():
            for project in sorted(projects.iterdir()):
                path = project / "AGENTS.md"
                if (
                    project.is_dir()
                    and not project.name.startswith(".")
                    and path.is_file()
                    and project.name != "global"
                ):
                    resources[project.name] = (path, f"Project: {project.name}")
        return resources

    def list_resources(self, kind: str) -> list[dict[str, Any]]:
        if kind == "instructions":
            return [
                {"name": name, "scope": scope, "path": str(path)}
                for name, (path, scope) in self._instruction_resources().items()
            ]
        if kind == "skills":
            return _json_value(collect_skills_rows(self.aikito_dir))
        if kind == "mcps":
            return [
                {"name": path.stem, "path": str(path)}
                for path in sorted((self.aikito_dir / "mcps").glob("*.toml"))
            ]
        if kind == "subagents":
            paths = sorted((self.aikito_dir / "subagents").glob("*.md"))
            return [{"name": path.stem, "path": str(path)} for path in paths]
        if kind == "inbox":
            return _json_value(collect_inbox_rows(get_inbox_path(self.aikito_dir)))
        if kind == "memory":
            return _json_value(collect_memory_notes_rows(self.aikito_dir, self.home))
        if kind == "projects":
            return _json_value(collect_project_summaries(self.aikito_dir, self.home))
        if kind == "agents":
            return _json_value(
                get_status_report_data(self.aikito_dir, self.home).agents
            )
        raise FileNotFoundError(kind)

    def resource(self, kind: str, name: str) -> dict[str, Any]:
        if kind == "instructions":
            instruction = self._instruction_resources().get(name)
            if instruction is None:
                raise FileNotFoundError(name)
            path, scope = instruction
            return self._markdown_detail(name, path, scope, "Canonical")
        if kind == "skills":
            path = _safe_child(self.aikito_dir / "skills", f"{name}/SKILL.md")
            rows = [
                row
                for row in collect_skills_rows(self.aikito_dir)
                if row.skill_name == name
            ]
            scope = rows[0].scope if rows else "Unknown"
            detail = self._markdown_detail(name, path, scope, "Canonical")
            detail["consumers"] = [
                agent.display_name
                for agent in load_agents(self.aikito_dir, self.home).values()
            ]
            return detail
        if kind == "subagents":
            path = _safe_child(self.aikito_dir / "subagents", f"{name}.md")
            detail = self._markdown_detail(name, path, "Global", "Canonical")
            detail["consumers"] = _json_value(
                collect_subagent_details(self.aikito_dir, self.home, name)
            )
            return detail
        if kind == "mcps":
            path = _safe_child(self.aikito_dir / "mcps", f"{name}.toml")
            with path.open("rb") as stream:
                content = _redact(tomllib.load(stream))
            return {
                "name": name,
                "kind": "toml",
                "source": str(path),
                "scope": "Global",
                "trust": "Canonical",
                "content": content,
                "consumers": _json_value(
                    collect_mcp_details(self.aikito_dir, self.home, name)
                ),
            }
        if kind == "inbox":
            root = get_inbox_path(self.aikito_dir)
            path = _safe_child(root, f"{name}.md")
            return self._markdown_detail(name, path, "Inbox", "Unreviewed")
        if kind == "memory":
            scope, separator, note = name.partition("/")
            if not separator:
                raise FileNotFoundError(name)
            root = (
                self.aikito_dir / "memory"
                if scope == "Global"
                else self.aikito_dir / "projects" / scope / "memory"
            )
            path = _safe_child(root / "notes", f"{note}.md")
            detail = self._markdown_detail(note, path, scope, "Durable")
            rows = collect_memory_notes_rows(self.aikito_dir, self.home)
            row = next(
                (
                    item
                    for item in rows
                    if item.scope_name == scope and item.note_name == note
                ),
                None,
            )
            detail["indexed"] = row.is_indexed if row else False
            detail["freshness_days"] = (
                datetime.now().timestamp() - path.stat().st_mtime
            ) // 86400
            detail["stale_after_days"] = load_workspace_config(
                self.aikito_dir
            ).memory.stale_days
            return detail
        if kind == "projects":
            project = next(
                (
                    item
                    for item in collect_project_summaries(self.aikito_dir, self.home)
                    if item.name == name
                ),
                None,
            )
            if project is None:
                raise FileNotFoundError(name)
            return {
                "name": name,
                "kind": "properties",
                "source": str(project.config_path),
                "scope": f"Project: {name}",
                "trust": "Canonical",
                "content": _json_value(project),
            }
        if kind == "agents":
            row = next(
                (
                    item
                    for item in get_status_report_data(
                        self.aikito_dir, self.home
                    ).agents
                    if item.agent_name == name
                ),
                None,
            )
            if row is None:
                raise FileNotFoundError(name)
            return {
                "name": row.display_name,
                "kind": "properties",
                "source": str(self.aikito_dir / "agents.toml"),
                "scope": "Target",
                "trust": "Canonical",
                "content": _json_value(row),
            }
        raise FileNotFoundError(f"{kind}/{name}")

    def doctor(self) -> dict[str, Any]:
        return _json_value(run_doctor(self.aikito_dir, self.home))

    def diff(self) -> list[dict[str, str]]:
        return [
            {"resource": name, "diff": content}
            for name, content in collect_drift_diffs(self.aikito_dir, self.home)
        ]

    def _markdown_resources(self) -> dict[Path, dict[str, str]]:
        resources: dict[Path, dict[str, str]] = {}

        def add(path: Path, kind: str, name: str) -> None:
            if path.is_file():
                resources[path.resolve()] = {"kind": kind, "name": name}

        for name, (path, _) in self._instruction_resources().items():
            add(path, "instructions", name)
        for path in (self.aikito_dir / "skills").glob("*/SKILL.md"):
            add(path, "skills", path.parent.name)
        for path in (self.aikito_dir / "subagents").glob("*.md"):
            add(path, "subagents", path.stem)

        inbox = get_inbox_path(self.aikito_dir)
        if inbox.is_dir():
            for path in inbox.rglob("*.md"):
                add(path, "inbox", str(path.relative_to(inbox).with_suffix("")))

        global_notes = self.aikito_dir / "memory" / "notes"
        if global_notes.is_dir():
            for path in global_notes.rglob("*.md"):
                name = str(path.relative_to(global_notes).with_suffix(""))
                add(path, "memory", f"Global/{name}")

        projects = self.aikito_dir / "projects"
        if projects.is_dir():
            for project in projects.iterdir():
                notes = project / "memory" / "notes"
                if not notes.is_dir():
                    continue
                for path in notes.rglob("*.md"):
                    name = str(path.relative_to(notes).with_suffix(""))
                    add(path, "memory", f"{project.name}/{name}")
        return resources

    def _resolve_wikilinks(
        self, source: Path, content: str
    ) -> dict[str, dict[str, str]]:
        resources = self._markdown_resources()
        resolved: dict[str, dict[str, str]] = {}
        for match in re.finditer(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", content):
            target = match.group(1).strip()
            file_target = target.split("#", 1)[0].strip()
            if not file_target:
                continue
            relative = Path(
                file_target if file_target.endswith(".md") else f"{file_target}.md"
            )
            direct = (source.parent / relative).resolve()
            destination = resources.get(direct)
            if destination is None:
                stem = relative.stem
                matches = [
                    item
                    for path, item in resources.items()
                    if path.stem == stem and path.parent == source.parent
                ]
                if len(matches) != 1:
                    matches = [
                        item for path, item in resources.items() if path.stem == stem
                    ]
                destination = matches[0] if len(matches) == 1 else None
            if destination is not None:
                resolved[target] = destination
        return resolved

    def _markdown_detail(
        self, name: str, path: Path, scope: str, trust: str
    ) -> dict[str, Any]:
        content = _read_text(path)
        return {
            "name": name,
            "kind": "markdown",
            "source": str(path),
            "scope": scope,
            "trust": trust,
            "content": content,
            "wikilinks": self._resolve_wikilinks(path, content),
            "updated": datetime.fromtimestamp(path.stat().st_mtime).isoformat(
                timespec="minutes"
            ),
        }


def make_handler(data: ConsoleData, web_dir: Path) -> type[BaseHTTPRequestHandler]:
    class ConsoleHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            path = unquote(urlsplit(self.path).path)
            try:
                if path.startswith("/api/"):
                    self._api(path)
                else:
                    self._static(path)
            except FileNotFoundError:
                self._json({"error": "Not found"}, 404)
            except Exception as exc:
                self._json({"error": str(exc)}, 500)

        def _api(self, path: str) -> None:
            parts = [part for part in path.removeprefix("/api/").split("/") if part]
            if parts == ["overview"]:
                self._json(data.overview())
            elif parts == ["doctor"]:
                self._json(data.doctor())
            elif parts == ["diff"]:
                self._json(data.diff())
            elif len(parts) == 1:
                self._json(data.list_resources(parts[0]))
            elif len(parts) >= 2:
                self._json(data.resource(parts[0], "/".join(parts[1:])))
            else:
                raise FileNotFoundError(path)

        def _static(self, path: str) -> None:
            relative = "index.html" if path in ("", "/") else path.lstrip("/")
            target = _safe_child(web_dir, relative)
            content_type = (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            payload = target.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", f"{content_type}; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def _json(self, value: Any, status: int = 200) -> None:
            payload = json.dumps(value, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ConsoleHandler


def serve_console(
    aikito_dir: Path,
    home: Path,
    version: str,
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    web_dir = Path(__file__).resolve().parent.parent / "web"
    server = ThreadingHTTPServer(
        ("127.0.0.1", port),
        make_handler(ConsoleData(aikito_dir, home, version), web_dir),
    )
    url = f"http://127.0.0.1:{server.server_port}"
    print("Aikito Console")
    print(url)
    if open_browser:
        threading.Timer(0.15, webbrowser.open, args=(url,)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nAikito Console stopped.")
    finally:
        server.server_close()
