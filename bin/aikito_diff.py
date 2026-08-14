"""Render safe unified diffs for all resources reported as drifted."""

import difflib
import json
from pathlib import Path
from typing import Any

from aikito_mcp import (
    evaluate_spec_status,
    load_agent_specs,
    read_entry,
    redact_mcp_entry,
)
from aikito_project import collect_project_skill_diffs
from aikito_subagent import build_plan


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


def collect_drift_diffs(aikito_dir: Path, home: Path) -> list[tuple[str, str]]:
    """Return display labels and redacted unified diffs for every drifted resource."""
    results: list[tuple[str, str]] = []

    for spec in load_agent_specs(aikito_dir, home):
        if evaluate_spec_status(spec) != "DRIFT":
            continue
        current = read_entry(spec, spec.config_path.read_text(encoding="utf-8"))
        if current is None:
            continue
        actual_label = str(spec.config_path)
        expected_label = f"mcps.toml ({spec.server})"
        diff = _unified_diff(
            _json_lines(redact_mcp_entry(current)),
            _json_lines(redact_mcp_entry(spec.desired)),
            actual_label,
            expected_label,
        )
        if not diff:
            diff = _redacted_only_diff(actual_label, expected_label)
        results.append((f"MCP {spec.agent}/{spec.server}", diff))

    plan, _ = build_plan(aikito_dir, home, allow_empty=True)
    for item in plan:
        if item.action != "UPDATE":
            continue
        actual = item.target_path.read_text(encoding="utf-8", errors="replace")
        diff = _unified_diff(
            actual.splitlines(keepends=True),
            item.rendered_content.splitlines(keepends=True),
            str(item.target_path),
            f"subagents/{item.subagent_name}.md ({item.agent_name})",
        )
        if diff:
            results.append((f"Subagent {item.agent_name}/{item.subagent_name}", diff))

    results.extend(collect_project_skill_diffs(aikito_dir, home))

    return results


def render_drift_diffs(diffs: list[tuple[str, str]]) -> str:
    if not diffs:
        return "No drift detected."
    return "\n\n".join(f"[{label}]\n{diff}" for label, diff in diffs)
