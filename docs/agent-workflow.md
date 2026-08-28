# Agent-First Workflows

Aikito is designed for coding agents to perform most workspace operations while
the user retains control over important decisions. In normal use, tell the
Agent what outcome you want instead of translating the task into CLI commands.

The prompts below are intentionally short. The bundled
[Aikito skill](../templates/skills/aikito/SKILL.md), the installed CLI help, and the
relevant documentation provide the operating details.

## Expected Operating Loop

For changes, an Agent should normally follow this loop:

```text
discover → plan → preview → confirm when required → apply → verify → report
```

Read-only discovery, status checks, and supported dry runs do not normally need
confirmation. The Agent should ask before adopting existing configuration,
resolving unmanaged conflicts, using `--force` or `--prune`, deleting durable
resources, starting authentication, registering an unrequested project, or
pushing commits.

After making changes, it should report what changed, where it was synchronized,
what it verified, and what remains unresolved. You do not need to repeat these
rules in every prompt when the Aikito skill is active.

## Set Up Aikito

Use this when Aikito is not yet installed or the workspace has not been
initialized:

> Set up Aikito for me. Inspect my existing Agent configuration first, ask
> before adopting or replacing anything, and verify the finished setup. Don't
> register a project or push yet.

## Register the Current Project

Use this from a code repository that needs project-specific instructions,
skills, or Memory:

> Set up the current repository as an Aikito project. Show me the proposed name,
> selected resources, and any conflicts before applying it, then sync and verify
> the result.

See [Set Up a Project](project-setup.md) for project layout and skill sync-mode
trade-offs.

## Create or Update a Skill

Use this when a repeatable workflow should be available across Agents:

> Create a `<global or project>` Aikito skill for `<workflow>`. Reuse or update
> an existing skill if appropriate, then sync it and verify the intended Agent
> targets.

If the scope is unclear, simply ask:

> Turn this workflow into an Aikito skill. Decide whether it should be global or
> project-specific and explain your choice.

## Configure an MCP Server

Use this when multiple supported Agents should share an MCP definition:

> Add the `<name>` MCP server to Aikito for `<target Agents>`, preview the
> changes, then sync and verify it. Keep credentials out of the workspace and
> ask before starting authentication.

See [Synchronize MCP Servers](mcp-servers.md) for configuration formats and
credential boundaries.

## Create or Update a Subagent

Use this when a specialized role should be reusable across Agent platforms:

> Create an Aikito subagent named `<name>` for `<responsibility>`. Target the
> Agent platforms that support it, then sync and verify the generated files.

To change an existing definition:

> Update the `<name>` subagent to `<new behavior>`. Show me any affected Agent
> targets before syncing it.

See [Manage Subagents](subagents.md) for supported formats and inspection
commands.

## Preserve a Durable Decision

Use this after a task produces knowledge that may matter again:

> Decide whether this is worth keeping in Aikito Memory and put it in the right
> scope if it is: `<conclusion>`.

For context from the current task:

> Capture any durable decisions from this task in Aikito Memory. Skip temporary
> details and don't create a duplicate note.

The `durable-memory` skill decides whether to write, which scope owns the
knowledge, and how to commit it. See [Work with Memory](durable-memory.md).

## Diagnose Drift

Use this when Agent integrations or project resources may be out of sync:

> Check my Aikito setup for drift or broken synchronization. Diagnose it without
> changing anything, then give me the safest repair plan.

To inspect one project:

> Check the current project's Aikito resources for drift. Don't fix or force
> anything yet.

## Adopt Existing Agent Configuration

Use this when Aikito is being introduced to an established local setup:

> See what Aikito can adopt from my existing Agent configuration. Show me the
> preview, conflicts, credentials, and backup plan, but don't apply it yet.

After reviewing the preview:

> Apply the adoption plan we just reviewed, one resource class at a time, and
> verify each result before syncing runtime configuration.

See the [Safety Model](safety.md#adoption) before applying an adoption plan.

## Maintain Memory

Use this for deliberate review of an entire global or project Memory scope:

> Review the current project's Aikito Memory. Propose what to update, merge,
> move, or retire, but wait for my approval before changing anything.

For global Memory:

> Review the global Aikito Memory scope and propose a cleanup. Focus on accuracy,
> duplication, scope, and obsolete knowledge.

Complete-scope maintenance may consume substantial model usage. See
[Proactive Scope Maintenance](durable-memory.md#proactive-scope-maintenance)
before running it on a large scope.

## Review Before Publishing

Use this before adding a remote or pushing the workspace:

> Check whether this Aikito workspace is safe to push. Look for secrets and
> private data in the files and commits that would be published. Report risks
> without printing sensitive values, and don't push anything.

Deleting a secret in a later commit does not remove it from Git history. Follow
the [Safety Model](safety.md#git-and-memory-privacy) before any push.

## Make a Prompt Your Own

Most requests only need three things:

```text
goal + important scope + unusual boundary
```

For example:

> Add our internal documentation MCP to Codex and OpenCode. Use the existing
> environment variable for its token, and let me review the preview first.

Avoid copying long command sequences into the prompt. Let the Agent use the
Aikito skill and current CLI help to choose the mechanics, while you state the
outcome and any decision you want to retain.
