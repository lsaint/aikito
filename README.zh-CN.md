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
  <a href="https://github.com/lsaint/aikito/actions/workflows/ci.yml"><img src="https://img.shields.io/github/actions/workflow/status/lsaint/aikito/ci.yml?branch=main&label=%EF%A3%BF%20%F0%9F%90%A7%20%E2%8A%9E%20CI" alt="CI"></a>
  <a href="https://github.com/lsaint/aikito/blob/main/LICENSE"><img src="https://img.shields.io/github/license/lsaint/aikito" alt="License"></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12%20%7C%203.13%20%7C%203.14-blue.svg" alt="Python 3.12 | 3.13 | 3.14"></a>
  <img src="https://img.shields.io/badge/dependencies-stdlib%20only-brightgreen.svg" alt="Dependencies: stdlib only">
</p>

<p align="center">
  <a href="README.md">English</a> · <a href="https://lsaint.github.io/aikito/">项目主页</a> · <a href="https://lsaint.github.io/aikito/guide/">详细文档（英文）</a>
</p>

Aikito 为你提供一个统一的地方，治理 Coding Agent 所共享的上下文。

跨 Agent、项目与机器，统一管理指令、Skills、MCP 配置、Subagents 和长期记忆。

<p align="center">
  <img src="https://raw.githubusercontent.com/lsaint/aikito/main/docs/assets/aikito-overview.png" alt="Aikito 概览图">
</p>

## 快速开始

### 1. 安装

跨平台推荐使用 [uv](https://docs.astral.sh/uv/)，macOS / Linux 也可使用 Homebrew：

```bash
uv tool install aikito
# 或: brew install lsaint/tap/aikito
```

### 2. 初始化工作区并接管已有配置

30 秒内将现有 Agent 配置纳入 Aikito 统一管理：

```bash
aikito init workspace ~/aikito
aikito adopt  # use --dry-run to preview, --verbose for full paths
aikito sync
aikito status
```

`adopt` 和 `sync` 都会在写入前进行完整预检。如果是全新开始，直接跳过 `aikito adopt` 即可。

> Windows 用户请开启 Developer Mode，并参考 [PowerShell 安装指南（英文）](docs/installation.md#install-manually)。

<details>
<summary><b>让 Coding Agent 自主完成配置？</b>（点击展开提示词）</summary>

将以下提示词直接发送给你的 Coding Agent：

> 请从 https://github.com/lsaint/aikito 安装并配置 Aikito。阅读 README、
> `src/aikito/templates/skills/aikito/SKILL.md` 及其中与本次配置相关的链接文档。检查我正在使用的
> Agent 配置，初始化 Aikito workspace，接管其中支持的已有资源，通过 Aikito 同步，并使用
> `aikito status` 验证结果。`adopt` 和 `sync` 都会在写入前完整预检；如果命令停止，请解释诊断
> 结果，并在使用 `--skip`、`--force`、`--prune` 或手工解决冲突前询问我。保留我现有的配置，
> 未经确认不要注册项目。如果没有可接管的内容，跳过该步骤并告诉我。

</details>

## 查看运行结果

运行 `aikito status`，一屏掌控所有 Agent 的上下文与多项目长期 Memory 同步状态：

```text
┌──────────┬───────┬────────┬────────┬───────┬──────┬────────┐
│ Project  │ Instr │ Skills │ Memory │ Paths │ Mode │ Status │
├──────────┼───────┼────────┼────────┼───────┼──────┼────────┤
│ aikito   │ 24L   │ 1      │ 12     │ 2/3   │ link │ ✓      │
│ payments │ 128L  │ 5      │ 14     │ 1/1   │ link │ ✓      │
│ infra    │ 210L  │ 6      │ 21     │ 1/1   │ copy │ ✓      │
│ blog     │ –     │ 2      │ 8      │ 0/1   │ link │ –      │
└──────────┴───────┴────────┴────────┴───────┴──────┴────────┘

Global: ✓ · Instr 24L · Skills 12 · Memory 6 · MCP 1 · Sub 3
Consumers (8): agy · claude · codex · copilot · dsh · grok · opencode · pi
All in one workspace: ~/aikito (AIKITO_DIR)
```

偏好浏览器查看？运行 [`aikito web`](docs/cli-reference.md#aikito-web) 打开本地只读 Console。

## 为什么需要 Aikito？

AI Agent 资源会在三个方向上碎片化：

- **跨工具**：每种 Agent 都有各自的文件夹、配置格式与记忆规范。
- **跨项目**：可复用的知识、Skills 与指令在各个仓库间拷贝或漂移失步。
- **跨时间**：有价值的架构决策与踩坑经验在临时的会话窗口中丢失。

Aikito 将源文件保存在个人的 Git 工作区，按需连接到各个 Agent 和项目。无需后台守护进程、向量数据库或托管云服务。

### 长期 Memory

内置的 [durable-memory skill](src/aikito/templates/skills/durable-memory/SKILL.md)
引导 Agent 检索有用笔记、沉淀验证过的结论并更新过时知识。笔记是受 Git 历史保护的纯 Markdown 文件：

- **Global 作用域**：跨项目的个人偏好、工程规范与工具习惯。
- **Project 作用域**：项目专属的架构决策、API 策略与本地流程。

详见 [Memory 使用与配置（英文）](docs/durable-memory.md) 与 [Memory 也需要维护者](docs/programming-agent-memory.zh-CN.md)。

## 安全与设计边界

Aikito 遵循 Local-first 原则与透明设计：

- **写入前预检**：`adopt` 与 `sync` 永远在模拟检查后再执行写入。
- **纯文本与 Git**：无后台守护进程，无私有数据库，不在 prompt 中隐式注入上下文。
- **自主掌控 Secrets**：工作区是本地 Git 仓库，推送到远端前请审查敏感信息。

深入了解安全与边界设计：
- [安全模型与备份机制（英文）](docs/safety.md#adoption)
- [设计边界与方案对比（英文）](docs/comparison.md)

## 文档导航

访问完整的[文档网站](https://lsaint.github.io/aikito/guide/)（英文），或查阅：

- [入门教程](docs/guide.md)：从安装到接管并同步现有 Agent 配置。
- [工作区与架构](docs/architecture.md)：目录结构、作用域划分与软链模型。
- [多机器连接](docs/workspace-portability.md)：多端同步与自定义路径。
- [CLI 命令参考](docs/cli-reference.md)：命令、参数与 Shell 补全。
- [Chat Distiller](docs/chat-distiller.md)：将浏览器 AI 对话沉淀为 Inbox 笔记。
- [故障排查](docs/troubleshooting.md)：解决冲突与同步漂移。

## 关注作者

作者在微信公众号分享关于 AI、编程、阅读与长期知识积累的实践和思考。欢迎在微信中搜索「不是很南」关注。

## 支持

如果你觉得 Aikito 对你有帮助，可以[支持它的开发](https://lsaint.github.io/donation/?utm_source=github&utm_medium=readme&utm_campaign=aikito)。
