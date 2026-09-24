# Comparison and Design Boundaries

Aikito and configuration distribution tools address some of the same problems:
keeping coding-agent resources consistent across different native formats. [Ruler](https://github.com/intellectronica/ruler) is a close overlap. Their organizing models differ, so the useful question is which workflow each model serves.

## Where Aikito Fits

| Category | Primary job |
| --- | --- |
| Agent configuration distribution | Apply shared configuration to agent-native files |
| Agent memory systems | Capture and retrieve historical context |
| Agent orchestration platforms | Run and coordinate agents |
| Aikito | Govern reusable agent resources across agents, projects, and machines |

These categories can overlap. Aikito manages the resources consumed by existing agents; it does not run agents or automatically capture their sessions.

## Aikito and Ruler

Both Aikito and Ruler manage instructions, MCP servers, skills, and subagents across coding agents. Both provide a canonical source, adapt resources to native agent locations, and offer a preview before applying changes. Ruler's [skills](https://github.com/intellectronica/ruler#skills-support-experimental) and [subagent](https://github.com/intellectronica/ruler#subagents-support-experimental) support are experimental; subagent propagation is disabled by default.

Ruler primarily applies shared configuration to a project. Aikito keeps a persistent workspace independent of any one repository, where projects register and consume selected resources.

| Dimension | Aikito | Ruler |
| --- | --- | --- |
| Organizing source | Persistent Aikito workspace with a project registry | Project `.ruler/`; global configuration is a fallback when no local `.ruler/` is found |
| Instructions and memory | Global and project instructions and curated Markdown memory | Shared rules; durable memory is not a primary resource |
| Skills, MCP, subagents | Workspace resources distributed to supported agents; projects select skills | Project configuration propagates resources to supported agents; skills and subagents are experimental |
| Cross-project workflow | Registered projects can select shared skills and have multiple local paths | Apply project configuration to each repository, or use the global fallback |
| Distribution | Links for project instructions and memory; project skills can be linked or copied; native MCP and subagent configuration is synchronized | Writes agent-native configuration and copies supported skills and subagents |
| Existing setup | `aikito adopt` imports supported existing agent resources | `ruler init` and `ruler apply` establish and apply Ruler configuration |
| Inspection | `status` and `show` inspect resources; `diff` shows supported managed-resource drift | `apply --dry-run` and verbose output preview changes; the README shows a CI drift-check workflow |
| Write safety | `adopt` and `sync` preflight complete plans before writing and stop on conflicts | Dry-run, backups, and revert; subagent output has separate cleanup behavior |
| Moving between machines | Bring a Git-managed workspace to another host, connect local paths, then sync | Install and apply project configuration or a global fallback on each host |

### Workspace and project ownership

Aikito's workspace stores reusable skills, MCP definitions, subagents, and global resources separately from code repositories. Its project registry holds each project's instructions, memory, selected skills, and candidate paths. This lets the same project resolve different checkouts or host-specific paths. Ruler looks for the nearest project `.ruler/` when applying configuration and falls back to its global configuration if no local one exists. See [Aikito's architecture](architecture.md) and [Ruler's README](https://github.com/intellectronica/ruler#usage-the-apply-command).

### Durable memory

Aikito treats [curated, Git-versioned Markdown memory](durable-memory.md) as a resource with global and project scopes. Agents decide what to retrieve and preserve. It is not an automatic session recorder, vector store, or prompt-injection engine. Ruler's documented model centers on distributing agent configuration rather than maintaining curated memory.

### Adoption, inspection, and distribution

Aikito can [adopt supported existing configuration](workspace-setup.md), preflight synchronization, and show aggregate state across registered projects. `aikito diff` covers drifted MCP entries, subagents, and copied project skills; other missing resources and unmanaged conflicts appear in status findings. The project's `link` or `copy` setting applies only to skills: project instructions and memory remain linked to the workspace. Ruler generates agent-native output from its configuration and offers dry-run, backups, and revert. Its README also demonstrates checking generated-file drift in CI. See [Aikito's project skill modes](architecture.md#project-skill-sync-modes), [CLI reference](cli-reference.md#drift-diff), and [Ruler's README](https://github.com/intellectronica/ruler).

## Which Workflow Fits?

**Ruler may fit well** when one repository's agent configuration is the main concern, especially if generated native files or nested project rules suit the team. Ruler's nested mode is currently experimental.

**Aikito may fit well** when several projects need selected shared resources, a persistent workspace independent of their repositories, curated memory, or an aggregate view of resource state and drift. A [Git-managed workspace can be connected on another machine](workspace-portability.md), followed by local path setup and synchronization.

The tools could serve different projects or resources in one workflow. If used in the same repository, assign ownership of native output paths so their writes do not collide. There is no implied integration between them.

## Adjacent Layers

**Memory systems** can capture sessions, index history, and retrieve or inject context automatically. Aikito instead keeps selected durable conclusions in plain Markdown.

**Orchestration platforms** schedule work and coordinate agent execution. Aikito supplies resources to agent runtimes but does not supervise their tasks.

Other configuration tools also distribute agent resources. Ruler is the detailed comparison here because its documented resource coverage closely overlaps with Aikito's.

## Design Choices: Why These Boundaries Exist

### Plain files over a database

Markdown, TOML, and JSON keep canonical resources inspectable, portable, and reviewable through Git, without a background database service.

### Curated memory over automatic capture

Aikito keeps durable conclusions instead of recording every agent event. The [durable-memory workflow](durable-memory.md) guides retrieval, curation, and correction.

### Existing agents over orchestration

Aikito configures supported agent runtimes. Execution, reasoning, and prompt assembly stay with those agents.

### A persistent workspace across projects

Aikito's source of truth is independent of any single project repository. Registered projects consume selected workspace resources, while their own instructions and memory remain project-specific.

### Explicit scopes over implicit context

Instructions and memory have explicit global and project scopes. Skills live in the workspace and can be selected by projects. MCP and subagent definitions are shared workspace resources. Agent capabilities differ, so Aikito synchronizes only supported resource types to each runtime.

Aikito supports macOS, Linux, Windows, and WSL2. Its [workspace portability guide](workspace-portability.md) explains how to connect another machine and resolve local project paths.

## When You May Not Need Aikito

Aikito may add little value if:

- you use one coding agent in one project and have few resources to maintain
- manual copying is sufficient for your workflow
- you prefer your agent's built-in memory and do not need cross-tool sharing
- you only need to distribute agent configuration within one repository; a project-focused tool such as Ruler may be sufficient
