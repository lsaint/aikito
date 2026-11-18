# Add an Agent Integration

Aikito's bundled registry provides built-in support for a curated set of popular
Agents. Community contributions can add more integrations without changing
Aikito's core when the Agent uses capabilities and configuration formats that
Aikito already understands. A new adapter or runtime behavior requires a code
contribution.

This guide covers both paths and points to the files that define the contract.

## First decide whether code is needed

Try a workspace-local definition first. Add `agents/<name>.toml` to your
workspace directory. The file name stem must be lowercase kebab-case (e.g.
`my-agent.toml`), and the file must contain exactly one top-level table matching
that stem (`[agents.my-agent]`).

See [Agent definitions and adapters](architecture.md#agent-definitions-and-adapters)
for architectural concepts, or inspect the built-in examples in
`src/aikito/templates/agents/` for real-world table schemas. Declare only the
capabilities you have verified:

- `instruction_path`, `project_instruction_path`, and `skills_path` describe
  instruction and skill targets.
- `[agents.<name>.mcp]` enables MCP synchronization using an existing MCP
  `config_format` and adapter.
- `[agents.<name>.subagents]` enables subagent synchronization using an existing
  subagent `config_format`.
- `[agents.<name>.runner]` enables commands that launch the Agent, such as
  `aikito maintain memory`. Omit it when the Agent lacks a non-interactive CLI
  or is not intended to run maintenance tasks.
- `[agents.<name>.detect]` is optional. It lists CLI commands and home-relative
  paths used to detect an installation.

Use paths relative to the user's home directory, not absolute paths. A capability
can be omitted when its native format is unsupported or has not been verified.
An Agent definition is capability-based: an integration can support instructions
and skills while leaving MCP or subagents out.

If the Agent needs a native config format or behavior not supported by an
existing adapter, follow the built-in contribution path below.

## Add a bundled Agent

### 1. Inspect the native formats

Confirm the Agent's actual paths and formats from its documentation or a working
installation. Check how global and project instructions differ, whether skills
are shared or agent-specific, and how MCP and subagent entries are represented.
Record any availability conditions, such as a required extension.

Prefer reusing an existing adapter when its behavior matches. Similar file
extensions do not guarantee matching semantics: for example, two TOML MCP
configs may differ in environment-variable expansion or authentication.

### 2. Add the registry definition and register the Agent

Create `src/aikito/templates/agents/<name>.toml`. Use a lowercase kebab-case
filename and matching registry key. Start from the smallest applicable example
among the existing definitions in `src/aikito/templates/agents/`.

For example, an Agent that supports instructions, skills, and detection only:

```toml
[agents.example]
display_name = "Example Agent"
instruction_path = ".example/AGENTS.md"
project_instruction_path = "AGENTS.md"
skills_path = ".agents/skills"

[agents.example.detect]
commands = ["example"]
paths = [".example"]
```

Add capability tables only when the implementation can safely read, update,
remove, and verify the Agent's native configuration. Preserve user configuration,
protect unmanaged content, and declare `requires_path` under
`[agents.<name>.subagents]` when subagent support depends on an optional
runtime file or extension. Capabilities like `runner` can remain omitted if the
Agent is not invoked directly by Aikito.

Next, register the Agent in code so Aikito recognizes it as a built-in:

- Add the Agent name to `BUILTIN_AGENTS` in `src/aikito/agents.py`. Aikito's
  workspace templating (`templating.py`), adoption engine (`adopt.py`), and
  bundled spec loader (`bundled_agent_spec()`) rely on this tuple to locate
  and instantiate built-in definitions. Without this registration, the template
  will not be generated into user workspaces.
- Register the template fingerprint in `src/aikito/workspace/templates.py`. Add
  an `"agent:<name>"` entry to `TEMPLATE_HISTORY` containing the
  `value_fingerprint()` of the parsed `[agents.<name>]` table (not a hash of the
  file bytes):

  ```bash
  uv run python -c "import tomllib, pathlib; \
    from aikito.workspace.resources import value_fingerprint; \
    path = pathlib.Path('src/aikito/templates/agents/<name>.toml'); \
    doc = tomllib.loads(path.read_text('utf-8')); \
    print(value_fingerprint(doc['agents']['<name>']))"
  ```

  Aikito uses this history during import and reconciliation to determine whether
  a workspace file has been modified from a shipped template. When a change
  alters an existing template's table values, append the new fingerprint without
  deleting older ones; comment-only edits do not change it.
  `tests/test_workspace_characterization.py` enforces that every current
  template fingerprint is listed.
- If the Agent should be targeted by default when users run `aikito add mcp` or
  `aikito add subagent`, update `DEFAULT_MCP_AGENTS` or `DEFAULT_SUBAGENT_AGENTS`
  in `src/aikito/add.py`.

### 3. Implement a new adapter only when necessary

Keep the Agent registry as the source of paths and capability declarations.
Add adapter behavior in the owning subsystem rather than adding Agent-specific
branches to shared synchronization code:

- MCP definitions and adapters: `src/aikito/mcp/` (format adapters live in
  `src/aikito/mcp/adapters/`, e.g. `toml.py`, `jsonc.py`, `cordis.py`)
- Subagent definitions and adapters: `src/aikito/subagent_adapters.py`
- Agent identity and definition loading: `src/aikito/agents.py`

Follow [Agent Domain Invariants](architecture/invariants-agent.md) and the
subsystem invariants linked from the architecture documentation. Adapters should
validate native configuration and preserve unrelated user data. Keep capability
absence distinct from a declared but unsupported format, and report useful
errors when a definition cannot be used.

### 4. Add focused tests

Cover the new registry definition and each capability you declare. For a native
config adapter, test create/update/remove behavior, malformed input, preservation
of unrelated settings, conflict handling, and any format-specific fields. For
optional capabilities, test both available and unavailable states. Place tests
beside the existing subsystem tests (for example, `tests/test_agents.py`,
`tests/test_mcp_adapters.py`, `tests/test_subagent_adapters.py`, or
`tests/agent_adapters_smoke.py`).

### 5. Add a real CLI smoke assertion

The `ubuntu-smoke-test` in `.github/workflows/ci-ubuntu.yml` must exercise the
Agent through Aikito's CLI and assert the resulting files or links. Add
cross-platform coverage to the macOS or Windows smoke workflow when behavior
depends on that platform. Adapter-level tests alone do not verify that the
bundled Agent is detected, configured, and synchronized end to end.

### 6. Update documentation and registry expectations

Update the consumer summaries and registry expectations across the project:

- Update `README.md` (the `Consumers (N)` count and list in the status summary).
- Update the Agent list in `docs/architecture.md`.
- Update any tests or smoke fixtures that assert the bundled registry set
  or characterization (e.g. `tests/test_workspace_characterization.py`).
- Document capability-specific behavior and prerequisites where users need to
  know them. Keep this guide focused on the contribution process.

## Before opening a contribution

- The Agent's supported capabilities match its documented native behavior.
- Existing user configuration and unrelated entries survive synchronization.
- Unsupported or unavailable capabilities are handled without writing invalid
  targets.
- Unit tests cover the new definition and adapter behavior.
- CI smoke tests invoke the real CLI and check created files or links.
- Documentation names the Agent and explains relevant limitations.

For general workspace behavior, see [Workspace and synchronization](architecture.md).
