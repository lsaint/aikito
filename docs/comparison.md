# Comparison and Design Boundaries

Different approaches exist for managing configurations, context, and persistent knowledge for AI coding agents. This document outlines where Aikito fits in the landscape, compares specific closest overlaps, explains core design choices, and helps you evaluate when to combine tools or when you may not need Aikito.

---

## Where Aikito Fits

| Category | Primary job | Typical state |
| --- | --- | --- |
| **Agent memory systems** | Preserve and retrieve past context | DB, index, Markdown, embeddings |
| **Project-local sync tools** | Keep one project's Agent configs aligned | Project `.agents/` or equivalent |
| **Skill managers** | Discover and install Agent skills | Registry + installed skills |
| **Agent orchestration platforms** | Run and coordinate multiple agents | Tasks, workers, sessions, DB |
| **Aikito** | **Govern reusable resources across agents and projects** | **Canonical Git workspace** |

These categories overlap. Aikito intentionally focuses on the workspace and resource-governance layer rather than replacing specialized memory, skill, or orchestration engines.

---

## Closest Overlaps

### 1. Aikito vs AgentSync

AgentSync focuses on aligning multi-agent configurations within an individual project directory, whereas Aikito establishes a personal canonical workspace for cross-project resource management.

| Dimension | Aikito | AgentSync |
| --- | --- | --- |
| **Center of gravity** | Personal AI workspace | Individual project |
| **Canonical source** | Configurable `<workspace>` | Project `.agents/` |
| **Cross-agent sync** | Yes | Yes |
| **Cross-project reuse** | Core model | Secondary |
| **Skills** | Yes | Yes |
| **Instructions** | Yes | Yes |
| **MCP** | Yes | Yes |
| **Durable memory** | First-class resource | Not primary focus |
| **Distribution** | Link or copy | Primarily symlink |
| **Git model** | Workspace itself is Git-managed state | Project repository manages source |
| **Main goal** | Reusable resources across tools and projects | Keep one project's Agent configs synchronized |

* **Choose AgentSync** when your primary problem is keeping one repository's Agent configuration synchronized across tools.
* **Choose Aikito** when you want a personal canonical workspace whose resources can be reused and selectively exposed across many projects.

---

### 2. Aikito vs agent-memory

agent-memory provides dedicated Markdown context persistence with hybrid search and context injection, whereas Aikito manages memory as one part of a broader file-based workspace.

| Dimension | Aikito | agent-memory |
| --- | --- | --- |
| **Primary focus** | Entire Agent workspace | Persistent memory |
| **Memory storage** | Curated Markdown | Markdown |
| **Search engine** | Native file search / agent-driven retrieval | `qmd` BM25 / vector / hybrid |
| **Automatic injection** | No dedicated injection engine | Yes |
| **Background indexing** | No | Optional `qmd` indexing |
| **Skills / Instructions / MCP** | Managed resources | Outside core scope |
| **Memory philosophy** | Small, curated, durable | Searchable persistent memory |

* **Choose agent-memory** when memory retrieval and automatic context injection are the main problem.
* **Choose Aikito** when lightweight durable memory is one part of a broader reusable Agent workspace.

---

### 3. Aikito vs claude-mem

*claude-mem remembers agent activity. Aikito curates durable agent resources.*

| Dimension | Aikito | claude-mem |
| --- | --- | --- |
| **Automatic capture** | No (manual distillation) | Yes (automated session capture) |
| **Storage layer** | Ordinary Markdown files | SQLite & Chroma vector database |
| **Vector search** | No | Yes |
| **Automatic context injection** | No | Yes |
| **Memory artifact** | Human-readable curated Markdown | Tool-generated observations and summaries |
| **Resource breadth** | Memory, skills, instructions, MCP, subagents | Memory & conversation context |
| **Git-native review** | Yes | Tool-managed DB |

* **Choose claude-mem** when you want automated session capture, background memory processing, and vector-based retrieval across supported agent workflows.
* **Choose Aikito** when you prefer explicit, Git-versioned curation of durable memory alongside your other agent resources.

---

### 4. Aikito vs CAS

*CAS runs the agents. Aikito prepares their workspace.*

| Dimension | Aikito | CAS |
| --- | --- | --- |
| **Primary role** | Workspace / resource governance | Agent execution / orchestration |
| **Runtime model** | Uses existing Agent runtimes | Runs and coordinates workers |
| **State storage** | Plain-file state | Structured DB / context system |
| **Execution pattern** | No supervisor | Supervisor / worker factory |
| **Task scheduling** | No task scheduler | Task / dependency coordination |

* **Choose CAS** when building an automated multi-agent execution pipeline or worker factory.
* **Choose Aikito** when governing the workspace resources consumed by the coding agents you already run.

---

## Design Choices: Why These Boundaries Exist

Aikito's boundaries are deliberate design choices aimed at keeping the system transparent, portable, and low-maintenance.

### Plain files over a database
Aikito prioritizes inspectability, Git history, and portability. Storing resources as plain Markdown, TOML, and JSON files ensures they remain human-readable and versionable without background daemons or database migrations.

### Curated memory over automatic capture
Aikito stores durable conclusions rather than attempting to retain every Agent event. Manual or semi-automated distillation prevents noise accumulation and keeps memory notes concise, reliable, and easy to review via Git diffs.

### Existing agents over orchestration
Aikito configures and supplies resources to Agent runtimes rather than replacing them. It leaves execution, reasoning, and prompt assembly to native coding agents like Codex, Claude Code, Antigravity `agy`, OpenCode, GitHub Copilot CLI, or DeepSeek Harness (`dsh`).

### Central workspace over project-local ownership
Reusable resources live once in the active Aikito workspace, and projects select what they need. This eliminates duplication while allowing projects to remain isolated when required.

### Explicit scopes over implicit context
Aikito separates global and project-specific resources explicitly instead of depending on an opaque retrieval layer to decide where context belongs.

### Additional Engineering Considerations
* **Capability Asymmetry**: Supported agents do not expose identical resource models. Aikito normalizes only the capabilities available for each agent runtime.
* **File-Based Context Boundary**: Aikito manages durable files and configurations. It does not automatically decide which memory should be injected into every prompt; context loading remains subject to each agent's native behavior.

### Platform Limitations
* **macOS / Linux / WSL2**: Relies on POSIX file permissions and symbolic links; native Windows is currently unsupported.

---

## Which Tool Solves Which Problem?

AI tooling is not mutually exclusive. Different tools address different layers of your development workflow:

* **Need automatic conversation/history memory?** → Use a dedicated memory system (e.g., `claude-mem`, `agent-memory`).
* **Need one project's Agent configs synchronized?** → Use a project-local sync tool (e.g., `AgentSync`).
* **Need to run many coding agents in parallel?** → Use an orchestration platform (e.g., `CAS`).
* **Need reusable resources shared across agents AND projects?** → Use **Aikito**.
* **Need several of these?** → **Combine them.**

---

## When You May Not Need Aikito

Aikito is designed for developers managing multiple agents and projects. You may not need Aikito if:

* you use only one coding agent
* you work mostly in a single project repository
* you have only a few instructions or skills that rarely change
* manual copy-paste is still sufficient for your workflow
* you prefer your agent's built-in memory system without cross-tool sharing
