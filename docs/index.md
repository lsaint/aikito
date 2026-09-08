# Aikito Documentation

Aikito keeps coding-agent instructions, skills, and durable memory in one
Git-managed workspace and connects them to your agents and code projects.

## Start with one project

Follow these steps in order. You will connect a project named `example`,
add a project instruction, and verify that your coding agent can read it.
Each step includes an agent request, manual commands, and a completion check.

| Step | What you will achieve |
| --- | --- |
| [1. Install Aikito](installation.md) | Run the CLI from your terminal |
| [2. Create a workspace](workspace-setup.md) | Establish the source files for your agent resources |
| [3. Connect a project](project-setup.md) | Give a code project its own resource scope |
| [4. Verify your first instruction](first-instruction.md) | Change a rule and confirm the project receives it |

Already installed? Start at the first step you have not completed.

## Find a specific operation

- [Edit instructions and select skills](instructions-and-skills.md).
- [Keep useful decisions in memory](durable-memory.md).
- [Synchronize MCP servers](mcp-servers.md) or [manage subagents](subagents.md).
- [Stage notes in Inbox](inbox.md).
- [Diagnose missing links, conflicts, or drift](troubleshooting.md).
- [Configure multiple project paths or copied skills](project-configuration.md).
- [Connect an existing workspace on another machine](workspace-portability.md).

## Understand or look up details

Read [Workspace and synchronization](architecture.md) for file ownership and
resource types, or [Memory scope and lifecycle](memory-workflow.md) for what to
retain and where. Use the [CLI reference](cli-reference.md) to look up commands,
and the [Safety model](safety.md) for write and recovery rules.
