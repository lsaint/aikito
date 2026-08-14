<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="docs/assets/logo-light.png">
    <img src="docs/assets/logo-light.png" alt="Aikito Logo" width="160">
  </picture>
</p>

<h1 align="center">Aikito</h1>

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12 | 3.13 | 3.14](https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg)](https://www.python.org/downloads/)
![Platform: macOS | Linux](https://img.shields.io/badge/platform-macOS%20%7C%20Linux-lightgrey.svg)
![Dependencies: stdlib only](https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg)

[English](README.md) · [详细文档（英文）](docs/README.md)

Aikito 是由 Git 管理的 AI Agent 上下文与长期记忆治理工作区。

```text
   Aikito  =  Agent资源治理   ×   长期记忆沉淀
```

纯文件定义唯一事实来源，显式作用域定义谁能看到什么，Git 保留完整历史。

资源由你定义，memory 由 Agent 维护，Aikito 治理整个工作区。

单个工作区即可让你的 AI 工作流在不同 Agent 与机器间保持一致。

<p align="center">
  <img src="docs/assets/aikito-overview-1.png" alt="Aikito 概览图 1">
</p>
<p align="center">
  <img src="docs/assets/aikito-overview-2.png" alt="Aikito 概览图 2">
</p>

## 为什么需要 Aikito？

AI Agent 资源会在三个方向上变得碎片化：

* 跨工具维度：每种 Agent 都需要不同的配置格式
* 跨项目维度：可复用的知识、skill 与 instruction 在多个仓库中被重复复制或维护
* 跨时间维度：有价值的决策与经验教训随时间消失在旧会话中

Aikito 将这些资源集中在一个个人 Git 工作区中，并将选定的资源暴露给各个 Agent 和项目：


```text
~/aikito
├── skills/                         跨项目复用的 skill
├── memory/                         全局长期 memory
├── global/                         全局指令
├── mcps/                           共享 MCP 定义
├── subagents/                      可复用 subagent
└── projects/
    └── <project-name>/
        ├── agent.toml              选定的共享资源
        ├── AGENTS.md               项目指令
        └── memory/                 项目长期 memory
            ├── index.md
            └── notes/
```

无需数据库、后台守护进程、向量数据库或托管服务。

## Aikito 管理什么？

| 资源 | 规范来源 | 同步目标 |
| --- | --- | --- |
| Memory | `memory/`、`projects/<name>/memory/` | 全局读取及 `<project>/.agents/memory/` |
| Skills | `skills/<name>/` | 全局和项目 skill 目录 |
| Instructions | `global/AGENTS.md`、`projects/<name>/AGENTS.md` | Agent 原生及项目运行时指令 |
| MCP server | `mcps/*.toml` | Agent 原生 TOML、JSON 或 JSONC 配置 |
| Subagent | `subagents.toml`、`subagents/` | Agent 原生 subagent 定义 |

默认注册表包含 Codex、Claude Code、Antigravity CLI（`agy`）、OpenCode 和
GitHub Copilot CLI。完整心智模型和
能力边界见[架构文档（英文）](docs/architecture.md)。

### 共享或隔离

- `link` 保持资源共享并实时同步更新
- `copy` 为项目提供可独立演进的隔离快照
- 项目 memory 始终与其规范作用域保持`link`模式，以维护统一的历史

## 长期 Memory

内置的 `durable-memory` skill 承担 memory 维护工作。它引导 Coding Agent 在动手前
检索相关笔记、把学到的东西提炼成可长期复用的结论、更新已经过时的笔记，并选择正确的
全局或项目作用域。

笔记就是普通 Markdown，所以 Git 直接提供了历史、审查、回滚和可移植性——而 Agent 昨天在
Claude Code 里写下的 memory，明天 Codex 读到的是同一份。

`aikito show memory` 按作用域列出已经沉淀下来的笔记，典型示例如下：

```text
┌────────┬───────────────────────┬─────────────────────────────────────┬───────┬──────┐
│ Scope  │ Note File             │ Title                               │ Index │ Link │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ Global │ commit-message-style  │ Conventional commits, English only  │ ✓     │ –    │
│ Global │ review-tone           │ Ask before large refactors          │ ✓     │ –    │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ aikito │ versioning-principles │ Version bumps skip round numbers    │ ✓     │ ✓    │
│ aikito │ release-checklist     │ Tag only after tests pass           │ ✓     │ ✓    │
├────────┼───────────────────────┼─────────────────────────────────────┼───────┼──────┤
│ blog   │ draft-workflow        │ Drafts live in content/ until dated │ ✓     │ ✓    │
└────────┴───────────────────────┴─────────────────────────────────────┴───────┴──────┘
```

全局笔记在任何地方都可读；每个项目的笔记只链接进该项目。因此示例中 Agent 在 `aikito` 里工作
时，只会看到全局笔记加上 `aikito` 自己的笔记，不含 `blog` 的内容。

## 设计边界

Aikito 管理持久化文件、显式作用域与可控同步。为了保持轻量与可移植，它明确选择**不做**
以下事情：

- 自动捕获每一个 Agent 操作或对话
- 运行向量数据库、embedding 管线或记忆服务
- 通过后台守护进程向每个 prompt 注入上下文
- 编排 supervisor 与 worker agent
- 替代你所使用的 Coding Agent 的原生运行时

Aikito 管控工作区及其中的 memory，推理交给你的 Agent。

## 环境要求

- macOS 或 Linux；Windows 用户使用 WSL2。
- Python 3.12、3.13 或 3.14。
- Git。

Aikito 的同步和凭据安全模型依赖软链接及 POSIX 文件权限，因此暂不支持原生 Windows。

## 快速开始

```bash
brew install lsaint/tap/aikito

aikito init workspace ~/aikito
aikito sync global
aikito status
```

通过 Homebrew 安装后，Zsh、Bash、Fish 的 Tab 补全会自动配置，无需额外操作。

手动安装时，在 `~/.zshrc` 中添加一行：

```zsh
eval "$(aikito completion zsh)"
```

Workspace 是 Aikito 所有资源的 Git 管理中心。通常每个用户或每台机器只需初始化一份。

需要项目专属 instruction、skill 或 memory 时，在对应代码目录中注册 project：

```bash
cd ~/code/example
aikito init project
```

该命令会在 `~/aikito/projects/example/` 创建项目的规范资源，并连接到当前目录的
`./.agents/`。一份 workspace 可以管理多个 project；project 代表一个代码目录及其专属
Agent 资源，不保存项目源码本身。

`aikito status` 会展示各个受支持 Agent 的资源状态：

```text
┌───────────────────────┬──────────────┬────────┬────────────┬───────────┐
│ Agent                 │ Instructions │ Skills │ MCP Config │ Subagents │
├───────────────────────┼──────────────┼────────┼────────────┼───────────┤
│ Codex                 │ ✓            │ 2 ›    │ –          │ –         │
│ Claude Code           │ ✓            │ 2 »    │ –          │ –         │
│ Antigravity CLI (agy) │ ✓            │ 2 »    │ –          │ –         │
│ OpenCode              │ ✓            │ 2 ›    │ –          │ –         │
│ GitHub Copilot CLI    │ ✓            │ 2 ›    │ –          │ –         │
└───────────────────────┴──────────────┴────────┴────────────┴───────────┘

✓ all synced · 5 agents · 2 skills · 0 notes across 1 scopes
```

如需从源码构建、使用自定义安装路径或查看高级参数，请参阅[项目配置指南（英文）](docs/project-setup.md)。

## 迁移现有配置

如果你已经在使用 Coding Agent 并存有已有的 instruction、MCP 定义或 subagent，可用
`aikito adopt` 将它们导入规范工作区：

```bash
aikito adopt
aikito adopt --apply
```

接管默认先只读预览。应用计划会在 `~/.aikito/backups/adopt_<timestamp>` 下创建带时间戳
的备份，并导入检测到的配置，不会覆盖原有文件。详见[安全指南（英文）](docs/safety.md)。

## 配套工具：Chat Distiller

[Chat Distiller](https://github.com/lsaint/chat-distiller) 可将浏览器中的 AI 对话提炼为
可审阅的 Markdown 笔记，并直接保存至 Aikito 的 `inbox/` 目录。

浏览器 AI 对话 → 提炼笔记 → 审阅归档 → 长期 memory

完整工作流请参阅[捕捉浏览器 AI 对话（英文）](docs/chat-distiller.md)。

## 安全优先

`aikito init workspace` 创建的是本地 Git 仓库，不会自动将其设为私有，也不代表它可以
安全公开。添加远端或推送前，请检查 memory 和配置中是否包含凭据、客户数据、内部地址、
私密源码等敏感信息。后续删除一次提交并不能从 Git 历史中清除秘密。

同步现有环境前，请阅读完整的[安全模型（英文）](docs/safety.md)。

## 文档

详细文档以英文作为规范来源。通过[文档索引](docs/README.md)查看核心概念、操作指南、
CLI 参考、安全模型、路线图和[常见问题（FAQ，英文）](docs/faq.md)。

- [设计边界与对比（英文）](docs/comparison.md)——Aikito 与记忆系统、单项目同步工具、
  Agent 编排平台之间的定位差异

## 关注作者

作者也在微信公众号分享关于 AI、编程、阅读与长期知识积累的实践和思考。欢迎在微信中搜索
「不是很南」关注。

## 参与贡献

欢迎提交 Issue 和 Pull Request。提交代码前请运行：

```bash
python3 -m unittest discover -s tests
```

安全漏洞请按[安全策略](SECURITY.md)私下报告。

## 支持

如果你觉得 Aikito 对你有帮助，可以[支持它的开发](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito)。
