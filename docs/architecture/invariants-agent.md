# Agent Domain Invariants

Contracts governing canonical Agent identity, Agent definition loading, and the dependency
direction between the Agent domain and resource subsystems such as MCP.

---

## Core Rules

### INV-AGENT-01: Agent Domain Owns Canonical Agent Definitions `[current]` {: #inv-agent-01 }

Canonical Agent identity and migrated capability declarations (`[agents.<name>.mcp]`, `[agents.<name>.subagents]`, and `[agents.<name>.runner]`) are represented by `agents.py::AgentDefinition` and loaded by `agents.py::load_agent_definitions()` or the targeted `load_agent_definition()`. MCP declarations are modeled as the `AgentDefinition.mcp` capability (`MCPCapability`): `None` means no section is declared, while `MCPCapability.is_supported == False` means the section explicitly declares `config_format = "unsupported"` and still carries its `config_path` and `reason`. Subagent and runner declarations are modeled as `AgentDefinition.subagents` (`SubagentCapability`) and `AgentDefinition.runner` (`RunnerCapability`); `None` means no section is declared. Resource subsystems may consume these definitions but must not define, load, or re-export Agent-domain symbols. `agents.py` must not import the `aikito.mcp`, `aikito.subagent`, or `aikito.maintain` module, and no production module may import Agent-domain symbols from `aikito.mcp`.

*Targeted tests*: `tests/test_agents.py`, `tests/test_architecture_dependencies.py`

### INV-AGENT-02: Agent Registry Failures Keep Agent-Domain Error Ownership `[current]` {: #inv-agent-02 }

Parsing or validating canonical Agent declarations raises `AgentRegistryError`. A resource subsystem may translate that error into its own error type only at its explicit boundary (for example `mcp/loader.py::_load_agent_definitions()` raising `MCPConfigError` for `load_agent_specs()`), and the translation must preserve the original message verbatim. User-visible Finding codes such as `MCP_CONFIG_ERROR` remain unchanged by this ownership rule.

*Targeted tests*: `tests/test_agents.py`, `tests/test_mcp.py`
