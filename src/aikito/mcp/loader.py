"""Loader for MCP server definitions from workspace configuration."""

import tomllib
from pathlib import Path
from typing import Any

from ..agents import (
    AgentDefinition,
    AgentRegistryError,
    is_agent_installed,
    load_agent_definitions,
)
from .adapters import _build_desired
from .model import (
    DEFAULT_AGENTS_CONFIG,
    DEFAULT_MCPS_DIR,
    AgentSpec,
    BasicTokenAuth,
    MCPConfigError,
)


def _load_agent_definitions(aikito_dir: Path, home: Path) -> dict[str, AgentDefinition]:
    """MCP boundary: expose Agent registry failures as MCPConfigError."""
    try:
        return load_agent_definitions(aikito_dir, home)
    except AgentRegistryError as exc:
        raise MCPConfigError(str(exc)) from exc


def _target_name(name_style: str, server_name: str) -> str:
    if name_style == "underscore":
        return server_name.replace("-", "_")
    return server_name


def _render_command(template: tuple[str, ...], target: str) -> tuple[str, ...]:
    return tuple(part.replace("{target}", target) for part in template)


def _load_basic_token_auth(
    server_name: str, server: dict[str, Any]
) -> BasicTokenAuth | None:
    authentication = server.get("authentication")
    if authentication is None:
        return None
    if not isinstance(authentication, dict):
        raise MCPConfigError(f"Server '{server_name}' authentication must be a table")
    method = authentication.get("method")
    if method != "basic_api_token":
        raise MCPConfigError(
            f"Server '{server_name}' has unsupported authentication method: {method!r}"
        )

    fields = {}
    for auth_field in ("account_email", "token_env", "authorization_env"):
        value = authentication.get(auth_field)
        if not isinstance(value, str) or not value:
            raise MCPConfigError(
                f"Server '{server_name}' authentication requires '{auth_field}'"
            )
        fields[auth_field] = value
    return BasicTokenAuth(**fields)


def load_agent_specs(aikito_dir: Path, home: Path) -> list[AgentSpec]:
    mcps_dir = aikito_dir / DEFAULT_MCPS_DIR
    if not mcps_dir.exists():
        raise MCPConfigError(f"MCP config directory not found: {mcps_dir}")
    if not mcps_dir.is_dir():
        raise MCPConfigError(f"MCP config path is not a directory: {mcps_dir}")

    servers: dict[str, dict[str, Any]] = {}
    for config_path in sorted(mcps_dir.glob("*.toml")):
        server_name = config_path.stem
        try:
            document = tomllib.loads(config_path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise MCPConfigError(f"Invalid MCP config {config_path}: {exc}") from exc

        if "servers" in document and isinstance(document["servers"], dict):
            for s_name, s_val in document["servers"].items():
                if isinstance(s_val, dict):
                    servers[s_name] = s_val
                else:
                    raise MCPConfigError(
                        f"Server '{s_name}' in {config_path} must be a table"
                    )
        else:
            servers[server_name] = document

    registry = _load_agent_definitions(aikito_dir, home)

    specs = []
    for server_name, server in servers.items():
        if not isinstance(server, dict):
            raise MCPConfigError(f"Server '{server_name}' must be a table")
        if server.get("transport") != "remote":
            raise MCPConfigError(f"Server '{server_name}' must use remote transport")
        url = server.get("url")
        agents = server.get("agents")
        if not isinstance(url, str) or not url:
            raise MCPConfigError(f"Server '{server_name}' requires a URL")
        if not isinstance(agents, list) or not all(
            isinstance(agent, str) for agent in agents
        ):
            raise MCPConfigError(f"Server '{server_name}' requires an agents list")
        overrides = server.get("overrides", {})
        if not isinstance(overrides, dict):
            raise MCPConfigError(f"Server '{server_name}' overrides must be a table")
        authentication = _load_basic_token_auth(server_name, server)
        headers = server.get("headers")
        if headers is not None and (
            not isinstance(headers, dict)
            or not all(
                isinstance(key, str) and isinstance(value, str)
                for key, value in headers.items()
            )
        ):
            raise MCPConfigError(
                f"Server '{server_name}' headers must be a string-to-string table"
            )

        for agent in agents:
            definition = registry.get(agent)
            if definition is None:
                raise MCPConfigError(
                    f"Server '{server_name}' references unknown agent '{agent}'; "
                    f"define it in {DEFAULT_AGENTS_CONFIG}"
                )

            override = overrides.get(agent, {})
            if not isinstance(override, dict):
                raise MCPConfigError(
                    f"Server '{server_name}' override for {agent} must be a table"
                )
            enabled = override.get("enabled", True)
            reason = str(override.get("reason", ""))

            capability = definition.mcp
            if capability is None or not capability.is_supported:
                specs.append(
                    AgentSpec(
                        agent=agent,
                        server=server_name,
                        config_path=capability.config_path if capability else Path(),
                        config_format="unsupported",
                        target_name=server_name,
                        desired={},
                        enabled=False,
                        reason=(
                            reason
                            or (capability.reason if capability else "")
                            or f"MCP synchronization is not supported for agent '{agent}'"
                        ),
                        home=home,
                    )
                )
                continue

            name_style = capability.name_style
            default_target = _target_name(name_style, server_name)
            target_name = str(override.get("name", default_target))
            desired, contains_secret, missing_credential_env = _build_desired(
                capability.config_format,
                url,
                override,
                authentication,
                headers,
                agent=agent,
                server_name=server_name,
            )
            if capability.config_format == "dsh_cordis" and not desired.get(
                "serverName"
            ):
                desired = dict(desired)
                desired["serverName"] = target_name
            specs.append(
                AgentSpec(
                    agent=agent,
                    server=server_name,
                    config_path=capability.config_path,
                    config_format=capability.config_format,
                    target_name=target_name,
                    desired=desired,
                    enabled=bool(enabled),
                    reason=reason,
                    live_command=_render_command(capability.live_command, target_name),
                    auth_command=(
                        ()
                        if authentication
                        else _render_command(capability.auth_command, target_name)
                    ),
                    contains_secret=contains_secret,
                    missing_credential_env=missing_credential_env,
                    home=home,
                )
            )
    return specs


def _find_agent_spec(specs: list[AgentSpec], agent: str, server: str) -> AgentSpec:
    try:
        return next(
            spec for spec in specs if spec.agent == agent and spec.server == server
        )
    except StopIteration as exc:
        raise MCPConfigError(
            f"MCP server '{server}' is not configured for agent '{agent}'"
        ) from exc


def _agent_detected(spec: AgentSpec) -> bool:
    if spec.home is not None:
        installed = is_agent_installed(spec.agent, spec.home)
        if installed is not None:
            return installed
    return spec.config_path.parent.exists()
