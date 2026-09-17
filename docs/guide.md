# Aikito Documentation

Aikito keeps coding-agent instructions, skills, MCP definitions, subagents, and
durable memory in one Git-managed workspace and connects them to your agents and
code projects.

## Bring your existing setup under control

Aikito assumes you already use one or more coding agents and have accumulated
instructions, MCP servers, or subagents that are becoming repetitive to
maintain. Follow these steps to bring those resources into one source of truth
without replacing the working setup before it is safe.

| Step | What you will achieve |
| --- | --- |
| [1. Install Aikito](installation.md) | Run the CLI from your terminal |
| [2. Adopt your existing setup](workspace-setup.md) | Import the resources your Agents already use |
| [3. Synchronize and verify](sync-existing-setup.md) | Connect the canonical resources back to every Agent |
| [4. Connect a project](project-setup.md) | Link an existing repository to the workspace |

Already installed? Start at the first step you have not completed.

Starting from scratch is supported, but it is not the primary tutorial. Create
the workspace, skip adoption when nothing is detected, then
[connect a project](project-setup.md) or [add a skill](skills.md).

## Find a specific operation

- [Add or import a skill](skills.md).
- [Manage instructions and rules](instructions.md).
- [Keep useful decisions in memory](durable-memory.md).
- [Synchronize MCP servers](mcp-servers.md) or [manage subagents](subagents.md).
- [Stage notes in Inbox](inbox.md) or [distill conversations](chat-distiller.md).
- [Diagnose missing links, conflicts, or drift](troubleshooting.md).
- [Configure multiple project paths or copied skills](project-configuration.md).
- [Connect an existing workspace on another machine](workspace-portability.md).

## Understand or look up details

Read [Workspace and synchronization](architecture.md) for file ownership and
resource types, or [Memory scope and lifecycle](memory-workflow.md) for what to
retain and where. Use the [CLI reference](cli-reference.md) to look up commands,
and the [Safety model](safety.md) for write and recovery rules.
