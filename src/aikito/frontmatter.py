"""Markdown frontmatter parser and manipulator for Aikito.

Provides YAML frontmatter extraction, block scalar handling (e.g. >-, |),
and targeted key updates.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional, Tuple

from .subagent import KNOWN_PLATFORM_FIELDS


def _split_markdown_frontmatter(
    content: str,
) -> Optional[Tuple[str, str, str]]:
    """Split markdown content into (bom, frontmatter_text, body_text).

    Frontmatter must begin with a standalone '---' line and end with a standalone '---' line.
    Returns None if content does not have a valid frontmatter block.
    """
    bom = "\ufeff" if content.startswith("\ufeff") else ""
    raw = content[len(bom) :]
    lines = raw.splitlines(keepends=True)
    if not lines:
        return None

    first_line = lines[0].rstrip("\r\n")
    if first_line.rstrip() != "---" or first_line.startswith((" ", "\t")):
        return None

    closing_idx = None
    for idx in range(1, len(lines)):
        line_stripped = lines[idx].rstrip("\r\n")
        if line_stripped.rstrip() == "---" and not line_stripped.startswith(
            (" ", "\t")
        ):
            closing_idx = idx
            break

    if closing_idx is None:
        return None

    frontmatter_raw = "".join(lines[1:closing_idx])
    body = "".join(lines[closing_idx + 1 :])
    return bom, frontmatter_raw, body


def _parse_yaml_value(val_str: str) -> Any:
    val_str = val_str.strip()
    if not val_str:
        return ""
    if val_str.startswith("[") and val_str.endswith("]"):
        try:
            return json.loads(val_str)
        except Exception:
            inner = val_str[1:-1].strip()
            if not inner:
                return []
            return [_parse_yaml_value(item.strip()) for item in inner.split(",")]
    if val_str.startswith("{") and val_str.endswith("}"):
        try:
            return json.loads(val_str)
        except Exception:
            inner = val_str[1:-1].strip()
            if not inner:
                return {}
            res: Dict[str, Any] = {}
            for pair in inner.split(","):
                if ":" in pair:
                    pk, pv = pair.split(":", 1)
                    res[pk.strip().strip("\"'")] = _parse_yaml_value(pv.strip())
            return res
    if val_str.lower() == "true":
        return True
    if val_str.lower() == "false":
        return False
    if val_str.lower() in ("null", "~"):
        return None
    return val_str.strip("\"'")


def _parse_markdown_frontmatter(content: str) -> Tuple[Dict[str, Any], str]:
    split_res = _split_markdown_frontmatter(content)
    if not split_res:
        split_res = _split_markdown_frontmatter(content.strip())
    if not split_res:
        return {}, content.strip()

    _, frontmatter_raw, body = split_res

    meta: Dict[str, Any] = {}
    lines = frontmatter_raw.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if not line.startswith((" ", "\t")) and ":" in line:
            k, v = line.split(":", 1)
            key = k.strip()
            val_str = v.strip()

            is_block_scalar = val_str in ("|", ">", "|-", ">-", "|+", ">+")
            is_folded = val_str.startswith(">")

            if val_str and not is_block_scalar:
                meta[key] = _parse_yaml_value(val_str)
                i += 1
            else:
                i += 1
                child_lines: List[str] = []
                while i < len(lines):
                    next_line = lines[i]
                    if next_line.strip() == "":
                        child_lines.append(next_line)
                        i += 1
                        continue
                    if next_line.startswith((" ", "\t")):
                        child_lines.append(next_line)
                        i += 1
                    else:
                        break

                non_empty = [
                    c
                    for c in child_lines
                    if c.strip() and not c.strip().startswith("#")
                ]
                if is_block_scalar:
                    if is_folded:
                        meta[key] = " ".join(c.strip() for c in non_empty)
                    else:
                        meta[key] = "\n".join(c.strip() for c in non_empty)
                elif not non_empty:
                    meta[key] = ""
                elif key in KNOWN_PLATFORM_FIELDS and any(":" in c for c in non_empty):
                    sub_dict: Dict[str, Any] = {}
                    for c in non_empty:
                        if ":" in c:
                            sub_k, sub_v = c.split(":", 1)
                            sub_dict[sub_k.strip()] = _parse_yaml_value(sub_v)
                    meta[key] = sub_dict
                elif any(c.strip().startswith("- ") for c in non_empty):
                    items: List[Any] = []
                    for c in non_empty:
                        s = c.strip()
                        if s.startswith("- "):
                            items.append(_parse_yaml_value(s[2:]))
                    meta[key] = items
                else:
                    meta[key] = " ".join(c.strip() for c in non_empty)
        else:
            i += 1

    return meta, body.strip()


def _format_yaml_scalar(val: str) -> str:
    """Format a string value as a valid YAML scalar, escaping special characters as needed."""
    if not val:
        return '""'
    needs_quotes = (
        any(c in val for c in ':#{}[]|>&*!%@`"\n\r\t,?')
        or val.strip() != val
        or val.startswith(("-", "?", "@", "`", "%"))
        or val.lower() in ("true", "false", "null", "yes", "no", "on", "off")
    )
    if needs_quotes:
        escaped = (
            val.replace("\\", "\\\\")
            .replace('"', '\\"')
            .replace("\n", "\\n")
            .replace("\r", "\\r")
        )
        return f'"{escaped}"'
    return val


def _update_markdown_frontmatter(content: str, updates: Dict[str, str]) -> str:
    """Targetedly update specific keys in markdown frontmatter while preserving all other keys,
    comments, formatting, and body.
    """
    split_res = _split_markdown_frontmatter(content)
    if not split_res:
        fm_lines = ["---"]
        for k, v in updates.items():
            fm_lines.append(f"{k}: {_format_yaml_scalar(v)}")
        fm_lines.append("---")
        return "\n".join(fm_lines) + "\n\n" + content.lstrip("\ufeff").lstrip()

    bom, frontmatter_raw, body = split_res

    fm_lines = frontmatter_raw.splitlines()
    remaining_updates = dict(updates)

    new_lines: List[str] = []
    i = 0
    while i < len(fm_lines):
        line = fm_lines[i]
        matched_key = None
        for key in list(remaining_updates.keys()):
            if re.match(rf"^{re.escape(key)}\s*:", line):
                matched_key = key
                break

        if matched_key:
            new_val = remaining_updates.pop(matched_key)
            formatted_val = _format_yaml_scalar(new_val)
            new_lines.append(f"{matched_key}: {formatted_val}")
            i += 1
            # Skip indented continuation lines or blank lines of previous scalar
            while i < len(fm_lines):
                next_line = fm_lines[i]
                if next_line.startswith((" ", "\t")):
                    i += 1
                elif next_line.strip() == "":
                    j = i + 1
                    while j < len(fm_lines) and fm_lines[j].strip() == "":
                        j += 1
                    if j < len(fm_lines) and fm_lines[j].startswith((" ", "\t")):
                        i = j + 1
                    else:
                        break
                else:
                    break
        else:
            new_lines.append(line)
            i += 1

    for key, val in remaining_updates.items():
        formatted_val = _format_yaml_scalar(val)
        if key == "name":
            insert_idx = 0
            while insert_idx < len(new_lines) and new_lines[
                insert_idx
            ].strip().startswith("#"):
                insert_idx += 1
            new_lines.insert(insert_idx, f"name: {formatted_val}")
        else:
            new_lines.append(f"{key}: {formatted_val}")

    joined_fm = "\n".join(new_lines).strip("\n")
    if joined_fm:
        return f"{bom}---\n{joined_fm}\n---\n\n{body.lstrip()}"
    return f"{bom}---\n---\n\n{body.lstrip()}"
