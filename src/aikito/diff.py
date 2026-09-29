"""Render safe unified diffs for all resources reported as drifted."""

import difflib
import json
import posixpath
from collections import defaultdict
from pathlib import Path
from typing import Any

from .diff_model import DriftDiff
from .mcp import (
    build_mcp_plan,
    evaluate_spec_status,
    load_agent_specs,
    read_entry,
    redact_mcp_entry,
)
from .project import collect_project_skill_diffs
from .subagent import build_subagent_plan


def _json_lines(value: dict[str, Any]) -> list[str]:
    return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True).splitlines(
        keepends=True
    )


def _unified_diff(
    actual: list[str], expected: list[str], actual_label: str, expected_label: str
) -> str:
    return "".join(
        difflib.unified_diff(
            actual,
            expected,
            fromfile=f"actual: {actual_label}",
            tofile=f"expected: {expected_label}",
        )
    ).rstrip()


def _redacted_only_diff(actual_label: str, expected_label: str) -> str:
    """Represent a drift hidden entirely by redaction without exposing values."""
    return _unified_diff(
        ["<redacted value differs>\n"],
        ["<expected redacted value>\n"],
        actual_label,
        expected_label,
    )


def collect_drift_diffs(
    aikito_dir: Path,
    home: Path,
    *,
    project_filter: str | None = None,
    kind: str | None = None,
) -> list[DriftDiff]:
    """Collect diffs, reading only the requested resource kind when specified."""
    results: list[DriftDiff] = []
    if kind in (None, "mcp"):
        try:
            specs = load_agent_specs(aikito_dir, home)
            plan = build_mcp_plan(aikito_dir, home, specs=specs)
        except Exception:
            specs = load_agent_specs(aikito_dir, home)
            plan = None

        for spec in specs:
            if evaluate_spec_status(spec, home=home, plan=plan) not in (
                "DRIFT",
                "UPDATE",
            ):
                continue
            current = read_entry(spec, spec.config_path.read_text(encoding="utf-8"))
            if current is None:
                continue
            actual_label = str(spec.config_path)
            expected_label = f"mcps/{spec.server}.toml"
            diff = _unified_diff(
                _json_lines(redact_mcp_entry(current)),
                _json_lines(redact_mcp_entry(spec.desired)),
                actual_label,
                expected_label,
            )
            if not diff:
                diff = _redacted_only_diff(actual_label, expected_label)
            results.append(
                DriftDiff(
                    kind="mcp",
                    name=spec.server,
                    agent=spec.agent,
                    diff=diff,
                )
            )

    if kind in (None, "subagent"):
        try:
            subagent_plan = build_subagent_plan(aikito_dir, home, allow_empty=True)
        except Exception:
            subagent_plan = None

        if subagent_plan:
            for op in subagent_plan.operations:
                if op.action != "UPDATE":
                    continue
                target_path = op.target.path
                subagent_name = op.target.logical_identity
                agent_name = op.target.agent
                rendered = op.rendered_payload or ""
                actual = target_path.read_text(encoding="utf-8", errors="replace")
                diff = _unified_diff(
                    actual.splitlines(keepends=True),
                    rendered.splitlines(keepends=True),
                    str(target_path),
                    f"subagents/{subagent_name}.md ({agent_name})",
                )
                if diff:
                    results.append(
                        DriftDiff(
                            kind="subagent",
                            name=subagent_name,
                            agent=agent_name,
                            diff=diff,
                        )
                    )

    if kind in (None, "project_skill"):
        results.extend(
            collect_project_skill_diffs(aikito_dir, home, project_filter=project_filter)
        )

    return results


def filter_drift_diffs(
    diffs: list[DriftDiff],
    *,
    kind: str | None = None,
    project: str | None = None,
    name: str | None = None,
    file: str | None = None,
    agent: str | None = None,
) -> list[DriftDiff]:
    """Filter structured diffs by field."""
    if file is not None:
        file = posixpath.normpath(file.replace("\\", "/"))
    results = []
    for item in diffs:
        if kind is not None and item.kind != kind:
            continue
        if project is not None and item.project != project:
            continue
        if name is not None and item.name != name:
            continue
        if file is not None and item.file != file:
            continue
        if agent is not None and item.agent != agent:
            continue
        results.append(item)
    return results


def render_drift_index(diffs: list[DriftDiff]) -> str:
    """Render high-level index of drifted resources."""
    if not diffs:
        return "No drift detected."

    lines: list[str] = ["Drift detected:"]

    # MCP
    mcp_items = [d for d in diffs if d.kind == "mcp"]
    if mcp_items:
        lines.append("")
        lines.append("MCP")
        for item in sorted(mcp_items, key=lambda x: f"{x.agent}/{x.name}"):
            lines.append(f"  {item.agent}/{item.name}")

    # Subagents
    subagent_items = [d for d in diffs if d.kind == "subagent"]
    if subagent_items:
        lines.append("")
        lines.append("Subagents")
        for item in sorted(subagent_items, key=lambda x: f"{x.agent}/{x.name}"):
            lines.append(f"  {item.agent}/{item.name}")

    # Projects
    project_items = [d for d in diffs if d.kind == "project_skill" and d.project]
    if project_items:
        lines.append("")
        lines.append("Projects")

        projects: dict[str, dict[str, dict[str, int]]] = defaultdict(
            lambda: defaultdict(lambda: defaultdict(int))
        )
        for item in project_items:
            projects[item.project or ""][item.checkout or ""][item.name] += 1

        for project_number, proj in enumerate(sorted(projects)):
            if project_number:
                lines.append("")
            lines.append(f"  {proj}")
            for checkout, skills in sorted(projects[proj].items()):
                indent = "    "
                if checkout:
                    lines.append(f"    Checkout: {checkout}")
                    indent = "      "
                for skill_name, count in sorted(skills.items()):
                    plural = "file" if count == 1 else "files"
                    lines.append(f"{indent}{skill_name:<14} {count} {plural} changed")

    # Review hints
    hints: list[str] = []
    if project_items:
        hints.append("aikito diff project <project> <skill>")
    if mcp_items:
        hints.append("aikito diff mcp <agent> <server>")
    if subagent_items:
        hints.append("aikito diff subagent <agent> <name>")

    if hints:
        lines.append("")
        lines.append("Review details:")
        for hint in hints:
            lines.append(f"  {hint}")

    return "\n".join(lines)


def render_project_drift_index(project_name: str, diffs: list[DriftDiff]) -> str:
    """Render skill and file index for a specific project."""
    matching = [
        d for d in diffs if d.kind == "project_skill" and d.project == project_name
    ]
    if not matching:
        return "No matching drift detected."

    checkouts: dict[str, dict[str, list[str]]] = defaultdict(lambda: defaultdict(list))
    for item in matching:
        if item.file:
            checkouts[item.checkout or ""][item.name].append(item.file)

    lines: list[str] = [f"Project {project_name}"]
    for checkout, skills in sorted(checkouts.items()):
        if checkout:
            lines.extend(["", f"Checkout: {checkout}"])
        for skill_name, files in sorted(skills.items()):
            lines.extend(["", skill_name])
            for relative_file in sorted(files):
                lines.append(f"  {relative_file}")

    lines.append("")
    lines.append("Review details:")
    lines.append(f"  aikito diff project {project_name} <skill>")

    return "\n".join(lines)


def render_drift_diffs(
    diffs: list[DriftDiff], *, empty_message: str = "No drift detected."
) -> str:
    """Render full unified diffs for structured diff objects."""
    if not diffs:
        return empty_message
    return "\n\n".join(f"[{item.display_label}]\n{item.diff}" for item in diffs)
