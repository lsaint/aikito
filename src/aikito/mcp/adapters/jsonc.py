"""JSONC and JSON-based MCP config adapters (VS Code, Cursor, OpenCode, Claude, Copilot, agy)."""

import json
import tomllib
from typing import Any

from ..model import MCPConfigError, Token

_CONFIG_FORMAT_JSON_NAMES = {
    "agy_json": "agy",
    "claude_json": "Claude Code",
    "copilot_json": "GitHub Copilot CLI",
}


def _tokenize_jsonc(text: str) -> list[Token]:
    tokens = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace():
            index += 1
            continue
        if text.startswith("//", index):
            newline = text.find("\n", index + 2)
            index = len(text) if newline == -1 else newline + 1
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise MCPConfigError("Unterminated JSONC block comment")
            index = end + 2
            continue
        if char == '"':
            start = index
            index += 1
            while index < len(text):
                if text[index] == "\\":
                    index += 2
                    continue
                if text[index] == '"':
                    index += 1
                    break
                index += 1
            else:
                raise MCPConfigError("Unterminated JSONC string")
            tokens.append(Token("string", text[start:index], start, index))
            continue
        if char in "{}[]:,":
            tokens.append(Token(char, char, index, index + 1))
            index += 1
            continue

        start = index
        while (
            index < len(text)
            and not text[index].isspace()
            and text[index] not in "{}[]:,/"
        ):
            index += 1
        if start == index:
            raise MCPConfigError(f"Unexpected JSONC character at offset {index}")
        tokens.append(Token("literal", text[start:index], start, index))
    return tokens


def _parse_json_value(tokens: list[Token], index: int) -> tuple[Any, int]:
    if index >= len(tokens):
        raise MCPConfigError("Unexpected end of JSONC input")
    token = tokens[index]
    if token.kind == "string":
        return token.value, index + 1
    if token.kind == "{":
        result = {}
        index += 1
        while index < len(tokens) and tokens[index].kind != "}":
            key_token = tokens[index]
            if key_token.kind != "string":
                raise MCPConfigError("JSONC object keys must be strings")
            if index + 1 >= len(tokens) or tokens[index + 1].kind != ":":
                raise MCPConfigError("JSONC object key is missing ':'")
            value, index = _parse_json_value(tokens, index + 2)
            result[key_token.value] = value
            if index < len(tokens) and tokens[index].kind == ",":
                index += 1
        if index >= len(tokens) or tokens[index].kind != "}":
            raise MCPConfigError("Unterminated JSONC object")
        return result, index + 1
    if token.kind == "[":
        result = []
        index += 1
        while index < len(tokens) and tokens[index].kind != "]":
            value, index = _parse_json_value(tokens, index)
            result.append(value)
            if index < len(tokens) and tokens[index].kind == ",":
                index += 1
        if index >= len(tokens) or tokens[index].kind != "]":
            raise MCPConfigError("Unterminated JSONC array")
        return result, index + 1

    literals = {"true": True, "false": False, "null": None}
    if token.text in literals:
        return literals[token.text], index + 1
    try:
        return json.loads(token.text), index + 1
    except json.JSONDecodeError as exc:
        raise MCPConfigError(f"Invalid JSONC literal: {token.text}") from exc


def _parse_jsonc(text: str) -> Any:
    tokens = _tokenize_jsonc(text)
    if not tokens:
        return {}
    value, next_index = _parse_json_value(tokens, 0)
    if next_index != len(tokens):
        raise MCPConfigError("Unexpected content after JSONC document")
    return value


parse_jsonc = _parse_jsonc


def _object_members(
    tokens: list[Token], object_index: int
) -> tuple[dict[str, tuple[int, int, int]], int]:
    if tokens[object_index].kind != "{":
        raise MCPConfigError("Expected a JSONC object")

    members = {}
    index = object_index + 1
    while index < len(tokens) and tokens[index].kind != "}":
        key_index = index
        if tokens[key_index].kind != "string":
            raise MCPConfigError("JSONC object keys must be strings")
        if key_index + 1 >= len(tokens) or tokens[key_index + 1].kind != ":":
            raise MCPConfigError("JSONC object key is missing ':'")
        value_index = key_index + 2
        _, next_index = _parse_json_value(tokens, value_index)
        members[tokens[key_index].value] = (key_index, value_index, next_index - 1)
        index = next_index
        if index < len(tokens) and tokens[index].kind == ",":
            index += 1

    if index >= len(tokens) or tokens[index].kind != "}":
        raise MCPConfigError("Unterminated JSONC object")
    return members, index


def _line_indent(text: str, offset: int) -> str:
    line_start = text.rfind("\n", 0, offset) + 1
    return text[line_start:offset]


def _format_json_value(value: dict[str, Any], indent: str) -> str:
    lines = json.dumps(value, ensure_ascii=False, indent=2).splitlines()
    return lines[0] + "".join(f"\n{indent}{line}" for line in lines[1:])


def _load_document(config_format: str, text: str) -> dict[str, Any]:
    """Parse raw configuration text into a document dictionary for the given format."""
    if not text.strip():
        return {}

    if config_format == "toml":
        try:
            document = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            raise MCPConfigError(f"Invalid Codex TOML config: {exc}") from exc
        if not isinstance(document, dict):
            raise MCPConfigError("Codex TOML config root must be an object")
        return document

    if config_format == "jsonc":
        document = _parse_jsonc(text)
        if not isinstance(document, dict):
            raise MCPConfigError("OpenCode config root must be an object")
        return document

    if config_format in _CONFIG_FORMAT_JSON_NAMES:
        config_name = _CONFIG_FORMAT_JSON_NAMES[config_format]
        try:
            document = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MCPConfigError(f"Invalid {config_name} JSON config: {exc}") from exc
        if not isinstance(document, dict):
            raise MCPConfigError(f"{config_name} config root must be an object")
        return document

    if config_format == "dsh_cordis":
        from .cordis import _parse_dsh_cordis_entries

        return {"mcpServers": _parse_dsh_cordis_entries(text)}

    raise MCPConfigError(f"Unsupported config format: {config_format}")


def get_jsonc_server(text: str, server_name: str) -> dict[str, Any] | None:
    document = _load_document("jsonc", text)
    mcp = document.get("mcp")
    if mcp is None:
        return None
    if not isinstance(mcp, dict):
        raise MCPConfigError("OpenCode 'mcp' must be an object")
    server = mcp.get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(f"OpenCode MCP server '{server_name}' must be an object")
    return server


def get_agy_json_server(text: str, server_name: str) -> dict[str, Any] | None:
    return get_mcp_json_server(text, server_name, "agy_json", "agy")


def get_claude_json_server(text: str, server_name: str) -> dict[str, Any] | None:
    return get_mcp_json_server(text, server_name, "claude_json", "Claude Code")


def get_copilot_json_server(text: str, server_name: str) -> dict[str, Any] | None:
    return get_mcp_json_server(text, server_name, "copilot_json", "GitHub Copilot CLI")


def get_mcp_json_server(
    text: str, server_name: str, config_format: str, config_name: str
) -> dict[str, Any] | None:
    document = _load_document(config_format, text)
    servers = document.get("mcpServers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{config_name} 'mcpServers' must be an object")
    server = servers.get(server_name)
    if server is None:
        return None
    if not isinstance(server, dict):
        raise MCPConfigError(
            f"{config_name} MCP server '{server_name}' must be an object"
        )
    return server


def update_agy_json_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    return update_mcp_json_server(text, server_name, desired, "agy_json", "agy")


def update_claude_json_server(
    text: str, server_name: str, desired: dict[str, Any]
) -> str:
    return update_mcp_json_server(
        text, server_name, desired, "claude_json", "Claude Code"
    )


def update_copilot_json_server(
    text: str, server_name: str, desired: dict[str, Any]
) -> str:
    return update_mcp_json_server(
        text, server_name, desired, "copilot_json", "GitHub Copilot CLI"
    )


def update_mcp_json_server(
    text: str,
    server_name: str,
    desired: dict[str, Any],
    config_format: str,
    config_name: str,
) -> str:
    document = _load_document(config_format, text)
    servers = document.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise MCPConfigError(f"{config_name} 'mcpServers' must be an object")
    servers[server_name] = desired
    return json.dumps(document, ensure_ascii=False, indent=2) + "\n"


def remove_agy_json_server(text: str, server_name: str) -> str:
    return remove_mcp_json_server(text, server_name, "agy_json", "agy")


def remove_claude_json_server(text: str, server_name: str) -> str:
    return remove_mcp_json_server(text, server_name, "claude_json", "Claude Code")


def remove_copilot_json_server(text: str, server_name: str) -> str:
    return remove_mcp_json_server(
        text, server_name, "copilot_json", "GitHub Copilot CLI"
    )


def remove_mcp_json_server(
    text: str,
    server_name: str,
    config_format: str,
    config_name: str,
) -> str:
    if not text.strip():
        return text
    document = _load_document(config_format, text)
    servers = document.get("mcpServers")
    if isinstance(servers, dict) and server_name in servers:
        del servers[server_name]
        return json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    return text


def update_jsonc_server(text: str, server_name: str, desired: dict[str, Any]) -> str:
    if not text.strip():
        text = "{}\n"
    tokens = _tokenize_jsonc(text)
    if not tokens or tokens[0].kind != "{":
        raise MCPConfigError("OpenCode config root must be an object")

    root_members, root_close = _object_members(tokens, 0)
    mcp_member = root_members.get("mcp")
    if mcp_member is None:
        root_indent = _line_indent(text, tokens[0].start)
        child_indent = root_indent + "  "
        server_indent = child_indent + "  "
        formatted = _format_json_value(desired, server_indent)
        property_text = (
            f'{child_indent}"mcp": {{\n'
            f'{server_indent}"{server_name}": {formatted}\n'
            f"{child_indent}}}"
        )
        previous_token = tokens[root_close - 1]
        prefix = "\n" if previous_token.kind == "," or not root_members else ",\n"
        suffix = f"\n{root_indent}"
        close_offset = tokens[root_close].start
        return (
            text[:close_offset] + prefix + property_text + suffix + text[close_offset:]
        )

    _, mcp_value_index, _ = mcp_member
    if tokens[mcp_value_index].kind != "{":
        raise MCPConfigError("OpenCode 'mcp' must be an object")
    mcp_members, mcp_close = _object_members(tokens, mcp_value_index)
    server_member = mcp_members.get(server_name)
    if server_member is not None:
        key_index, value_start_index, value_end_index = server_member
        indent = _line_indent(text, tokens[key_index].start)
        formatted = _format_json_value(desired, indent)
        start = tokens[value_start_index].start
        end = tokens[value_end_index].end
        return text[:start] + formatted + text[end:]

    mcp_key_index, _, _ = mcp_member
    mcp_indent = _line_indent(text, tokens[mcp_key_index].start)
    child_indent = mcp_indent + "  "
    formatted = _format_json_value(desired, child_indent)
    property_text = f'{child_indent}"{server_name}": {formatted}'
    close_offset = tokens[mcp_close].start
    previous_token = tokens[mcp_close - 1]
    separator = "\n" if previous_token.kind == "," or not mcp_members else ",\n"
    return (
        text[:close_offset]
        + separator
        + property_text
        + f"\n{mcp_indent}"
        + text[close_offset:]
    )


def remove_jsonc_server(text: str, server_name: str) -> str:
    if not text.strip():
        return text
    tokens = _tokenize_jsonc(text)
    if not tokens or tokens[0].kind != "{":
        raise MCPConfigError("OpenCode config root must be an object")

    root_members, _ = _object_members(tokens, 0)
    mcp_member = root_members.get("mcp")
    if mcp_member is None:
        return text

    _, mcp_value_index, _ = mcp_member
    if tokens[mcp_value_index].kind != "{":
        raise MCPConfigError("OpenCode 'mcp' must be an object")
    mcp_members, mcp_close = _object_members(tokens, mcp_value_index)
    server_member = mcp_members.get(server_name)
    if server_member is None:
        return text

    key_idx, _, val_end_idx = server_member

    has_trailing_comma = (
        val_end_idx + 1 < mcp_close and tokens[val_end_idx + 1].kind == ","
    )
    has_leading_comma = (
        key_idx - 1 > mcp_value_index and tokens[key_idx - 1].kind == ","
    )

    cut_start = tokens[key_idx].start
    line_start = text.rfind("\n", 0, cut_start)
    if line_start != -1 and text[line_start + 1 : cut_start].strip() == "":
        cut_start = line_start + 1

    if has_trailing_comma:
        comma_tok = tokens[val_end_idx + 1]
        cut_end = comma_tok.end
        if cut_end < len(text) and text[cut_end] == "\n":
            cut_end += 1
        elif cut_end < len(text) and text[cut_end : cut_end + 2] == "\r\n":
            cut_end += 2
    elif has_leading_comma:
        comma_tok = tokens[key_idx - 1]
        cut_start = comma_tok.start
        cut_end = tokens[val_end_idx].end
        if cut_end < len(text) and text[cut_end] == "\n":
            cut_end += 1
        elif cut_end < len(text) and text[cut_end : cut_end + 2] == "\r\n":
            cut_end += 2
    else:
        cut_end = tokens[val_end_idx].end
        if cut_end < len(text) and text[cut_end] == "\n":
            cut_end += 1
        elif cut_end < len(text) and text[cut_end : cut_end + 2] == "\r\n":
            cut_end += 2

    result = text[:cut_start] + text[cut_end:]
    parsed = _parse_jsonc(result)
    if server_name in parsed.get("mcp", {}):
        raise MCPConfigError(
            f"Failed to remove server '{server_name}' from OpenCode JSONC"
        )
    return result
