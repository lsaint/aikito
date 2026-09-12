<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-dark.png">
    <source media="(prefers-color-scheme: light)" srcset="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-light.png">
    <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/logo-light.png" alt="Aikito Logo" width="160">
  </picture>
</p>

<h1 align="center">Aikito</h1>

<p align="center">
  <b>Multi-Agent · Multi-Project · Multi-OS · Multi-Machine</b>
</p>

<p align="center">
  <a href="https://github.com/lsaint/aikito/releases"><img src="https://img.shields.io/github/v/release/lsaint/aikito" alt="Release"></a>
  <a href="https://github.com/lsaint/aikito/actions/workflows/ci.yml"><img src="https://github.com/lsaint/aikito/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/lsaint/aikito/blob/main/LICENSE"><img src="https://img.shields.io/github/license/lsaint/aikito" alt="License"></a>
  <img src="https://img.shields.io/badge/platform-macOS%20%7C%20Linux%20%7C%20Windows-informational" alt="Platforms">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg" alt="Python 3.12 | 3.13 | 3.14"></a>
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg" alt="Dependencies: stdlib only">
</p>

[English](README.md) · [详细文档（英文）](https://lsaint.github.io/aikito/)

Aikito 将 Coding Agent 的指令、Skills、MCP、Subagents 和长期记忆集中在一个
Git 管理的工作区中，供不同 Agent 与项目使用。

Aikito 治理工作区，Agent 维护 memory，而一切由你把关。

<p align="center">
  <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/aikito-overview.png" alt="Aikito 概览图">
</p>

## 为什么需要 Aikito？

AI Agent 资源会在三个方向上变得碎片化：

- 跨工具：每种 Agent 需要不同的配置格式。
- 跨项目：可复用的知识、Skills 与指令在多个仓库中重复维护。
- 跨时间：有价值的决策与经验消失在旧会话中。

Aikito 将源文件集中在个人工作区中，把选定的资源连接到各个 Agent 和项目。
无需数据库、后台守护进程、向量数据库或托管服务。

## 长期 Memory

内置的 [durable-memory skill](src/aikito/templates/skills/durable-memory/SKILL.md) 引导 Agent
检索有用笔记、保留经过验证的结论，并更新过时知识。笔记是普通 Markdown，历史由 Git 管理。

例如，写作偏好放在全局 memory，API 重试策略放在对应项目的 memory。
项目默认连接全局和自身的笔记；这些作用域用于组织上下文，不构成文件访问权限隔离。

新工作区默认启用该工作流，同步后连接到 Agent。详见
[Memory 使用与停用（英文）](docs/durable-memory.md) 和
[Memory 也需要维护者](docs/programming-agent-memory.zh-CN.md)。

## 快速开始

### 让 Coding Agent 完成配置（推荐）

> 请从 https://github.com/lsaint/aikito 安装并配置 Aikito。阅读 README、
> `templates/skills/aikito/SKILL.md` 及其中与本次配置相关的链接文档，按照其安全要求初始化
> workspace、使用 `aikito sync` 同步资源，并使用 `aikito status` 验证结果。导入或更改任何已有的
> Agent 配置前，先向我展示计划变更和冲突并等待确认。配置完成后，总结已经就绪的内容，
> 并引导我完成下一步，包括是否注册第一个代码项目；未经我确认，不要注册项目。

<details>
<summary>手动安装（macOS / Linux / Windows）</summary>

跨平台推荐使用 [uv](https://docs.astral.sh/uv/)：

```bash
uv tool install aikito
```

macOS / Linux 也可使用 Homebrew：

```bash
brew install lsaint/tap/aikito
```

或使用 pipx：

```bash
pipx install aikito
```

初始化与同步 workspace：

```bash
aikito init workspace ~/aikito
aikito sync --dry-run
aikito sync
aikito status
```

应用同步前先检查预览；已有配置的处理方式见[迁移与安全](#迁移与安全)。

Windows 请开启 Developer Mode，使用 `uv tool install aikito` 或参考 [PowerShell 安装指南（英文）](docs/installation.md#install-manually)。

</details>

接下来按照 **[四步入门教程（英文）](docs/installation.md)** 接入第一个项目并验证指令生效。
其他任务可参考 [Agent 请求示例（英文）](docs/agent-workflow.md)。

## 查看运行结果

`aikito status` 展示各 Agent 的资源状态。以下为已配置工作区的典型输出，
实际 Agent 和数量取决于你的配置：

```text
┌───────────────────────┬──────────────┬────────┬────────────┬───────────┐
│ Agent                 │ Instructions │ Skills │ MCP Config │ Subagents │
├───────────────────────┼──────────────┼────────┼────────────┼───────────┤
│ Codex                 │ ✓            │ 2 ›    │ 0          │ 0         │
│ Claude Code           │ ✓            │ 2 »    │ 0          │ 0         │
│ Antigravity CLI       │ ✓            │ 2 »    │ 0          │ 0         │
│ OpenCode              │ ✓            │ 2 ›    │ 0          │ 0         │
│ GitHub Copilot CLI    │ ✓            │ 2 ›    │ 0          │ 0         │
│ DeepSeek Harness      │ ✓            │ 2 ›    │ 0          │ 0         │
│ Grok Build            │ ✓            │ 2 ›    │ 0          │ 0         │
│ Pi                    │ ✓            │ 2 ›    │ –          │ –         │
└───────────────────────┴──────────────┴────────┴────────────┴───────────┘

✓ all synced · 8 agents · 2 skills · 0 notes across 1 scopes
```

`aikito show memory` 按作用域列出保留的知识。下面是另一组示例，
包含 1 条全局笔记和 `example` 项目的 2 条笔记：

```text
┌─────────┬───────────────────┬──────────────────────────────┬──────┐
│ Scope   │ Note File         │ Title                        │ Link │
├─────────┼───────────────────┼──────────────────────────────┼──────┤
│ Global  │ writing-style     │ Keep explanations concise    │ –    │
├─────────┼───────────────────┼──────────────────────────────┼──────┤
│ example │ api-retry-policy  │ Retry external APIs safely   │ ✓    │
│ example │ release-checklist │ Release verification steps   │ ✓    │
└─────────┴───────────────────┴──────────────────────────────┴──────┘
```

全局笔记保存跨项目知识，项目笔记保存局部决策。完整操作见
[Memory 使用指南（英文）](docs/durable-memory.md#list-memory)。

发现缺失链接、冲突或漂移时，请参阅[同步排查指南（英文）](docs/troubleshooting.md)。
如果偏好浏览器界面，可运行 [`aikito web`](docs/cli-reference.md#aikito-web) 打开本地只读 Console。

## 设计边界

Aikito 基于普通文件与 Git，无需后台服务。

<details>
<summary>Aikito 不做什么</summary>

- 自动捕获每一个 Agent 操作或对话
- 运行向量数据库、embedding 管线或记忆服务
- 通过后台守护进程向每个 prompt 注入上下文
- 编排 supervisor 与 worker agent
- 替代你所使用的 Coding Agent 的原生运行时

</details>

## 迁移与安全

已有 Agent 配置时，先运行 `aikito adopt` 查看只读导入预览，审阅后再应用。
详见[接管与备份（英文）](docs/safety.md#adoption)。

工作区是本地 Git 仓库。发布前应检查秘密和私人数据；在后续提交中删除秘密不会清除历史记录。
请阅读[安全模型（英文）](docs/safety.md)，并按[安全策略](SECURITY.md)私下报告漏洞。

## 文档

详细文档以英文为规范来源：

- [入门教程](docs/installation.md)：从安装到第一条指令生效。
- [工作区与同步](docs/architecture.md)：源文件、作用域和资源归属。
- [接入另一台机器](docs/workspace-portability.md)：已有工作区与自定义路径。
- [CLI 参考](docs/cli-reference.md)：命令与 Shell 补全。
- [设计对比](docs/comparison.md)与[常见问题](docs/faq.md)：设计取舍与常见疑问。

配套工具 [Chat Distiller](https://github.com/lsaint/chat-distiller) 可将浏览器 AI 对话
提炼为 Markdown，存入 Aikito Inbox。详见[捕捉与整理流程（英文）](docs/chat-distiller.md)。

更多内容见[文档网站](https://lsaint.github.io/aikito/)。

## 关注作者

作者也在微信公众号分享关于 AI、编程、阅读与长期知识积累的实践和思考。欢迎在微信中搜索
「不是很南」关注。

## 支持

如果你觉得 Aikito 对你有帮助，可以[支持它的开发](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito)。
