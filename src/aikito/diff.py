"""Render safe unified diffs for all resources reported as drifted."""

import difflib
import json
from pathlib import Path
from typing import Any

from .mcp import (
    build_mcp_plan,
    evaluate_spec_status,
    load_agent_specs,
    read_entry,
    redact_mcp_entry,
)
from .project import collect_project_skill_diffs
from .subagent import build_plan, build_subagent_plan


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
    try:
        specs = load_agent_specs(aikito_dir, home)
        plan = build_mcp_plan(aikito_dir, home, specs=specs)
    except Exception:
        specs = load_agent_specs(aikito_dir, home)
        plan = None

    for spec in specs:
        if evaluate_spec_status(spec, home=home, plan=plan) not in ("DRIFT", "UPDATE"):
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
        results.append((f"MCP {spec.agent}/{spec.server}", diff))

    plan_out = build_plan(aikito_dir, home, allow_empty=True)
    items = plan_out[0] if isinstance(plan_out, tuple) else getattr(plan_out, "operations", plan_out)
    for item in items:
        action = getattr(item, "action", None)
        if action != "UPDATE":
            continue
        target_path = getattr(item, "target_path", None) or item.target.path
        subagent_name = getattr(item, "subagent_name", None) or item.target.logical_identity
        agent_name = getattr(item, "agent_name", None) or item.target.agent
        rendered = getattr(item, "rendered_content", None) or getattr(item, "rendered_payload", "")
        actual = target_path.read_text(encoding="utf-8", errors="replace")
        diff = _unified_diff(
            actual.splitlines(keepends=True),
            rendered.splitlines(keepends=True),
            str(target_path),
            f"subagents/{subagent_name}.md ({agent_name})",
        )
        if diff:
            results.append((f"Subagent {agent_name}/{subagent_name}", diff))

    results.extend(collect_project_skill_diffs(aikito_dir, home))

    return results


def render_drift_diffs(diffs: list[tuple[str, str]]) -> str:
    if not diffs:
        return "No drift detected."
    return "\n\n".join(f"[{label}]\n{diff}" for label, diff in diffs)
