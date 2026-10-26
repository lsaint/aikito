# Aikito Cloud

[English](remote-sync.md)

!!! note "同步方式"
    Aikito 默认通过 Git 进行多机器同步，无需专用服务器。
    你也可以根据 [Remote Protocol](architecture/remote-protocol.md) 开发自己的专用同步服务器，
    或使用 Aikito Cloud。本文介绍如何连接 Aikito Cloud。

    Aikito Cloud 同步还需要安装[加密运行时](installation.md#macos-linux-with-homebrew)。

将 Aikito 工作区连接到 Aikito Cloud，让多台机器上的资源保持更新。
你需要一个 Aikito Cloud 账号，以及账号页面提供的连接命令。

## 让 Agent 来做

将下面的请求交给可以使用本机终端的编程 Agent，例如 Codex 或 Claude Code。
Agent 可以检查工作区、执行配置命令并验证同步；账号登录、恢复码输入和授权确认由你亲自完成。
如果 Agent 无法保持交互式终端运行，让它提供连接命令，待你执行完成后继续。
不要将密码或恢复码粘贴到对话中。

### 配置 Cloud 同步

> 帮我将这台机器的 Aikito 工作区连接到 Aikito Cloud，服务地址使用账号页提供的值。
> 按照 [Cloud 指南](remote-sync.zh-CN.md)检查当前工作区和加密运行时，复用已有工作区。
> 引导我完成登录和设备连接确认。预览首次同步，有冲突先说明，再执行同步。
> 然后开启后台同步并验证远程同步状态。告诉我恢复码的本机备份路径，不要读取或输出恢复码。

### 接入另一台机器

> 帮我在这台新机器上接入已有的 Aikito Cloud 账号和服务。
> 按照 [Cloud 指南](remote-sync.zh-CN.md)检查安装和工作区，让我在本机终端完成登录和恢复码输入。
> 下载已同步的资源，开启后台同步，再同步并验证本机 Agent 配置。
> 需要项目的本机路径时向我询问，遇到冲突先说明再处理。

### 让 ChatGPT 访问资源

> 按照[下方的 MCP 接入说明](#ai-mcp)帮我将 ChatGPT 连接到 Aikito Cloud。
> 先验证 Cloud 同步正常并打开本机控制台，询问我要共享哪些资源、各自需要只读还是可写权限。
> 引导我完成资源授权、本机确认和 ChatGPT 连接，这些授权操作由我确认。
> 最后通过一次只读查询，帮助我验证选中的资源可以访问。不要索取或展示恢复码。

下文提供各项任务的手动步骤和完成检查。更多请求示例见 [Agent Request Examples](agent-workflow.md)。

## 连接第一台机器

如果收到了邀请，打开邀请链接并设置账号密码；已有账号则直接登录。

使用现有的 Aikito 工作区。如果尚未创建，运行：

```bash
aikito init workspace ~/aikito
```

从账号页复制连接命令，在交互式终端中运行：

```bash
aikito remote bind https://aikito.notsonan.com
```

1. 如果浏览器没有自动打开，打开终端打印的链接。
2. 登录账号，核对浏览器与终端的连接码（例如 `dc9b-4280`），确认连接自己刚刚操作的设备。
3. 回到终端。首次连接时，Aikito 会初始化加密并自动将恢复码保存到本机，打印保存路径，
   无需手动确认已经保存。

执行首次同步：

```bash
aikito remote sync
```

这个命令会实际应用变更。如果想先查看差异，可在同步前运行
`aikito remote sync --dry-run`。连接成功本身不会传输资源，也不会开启后台同步。

这些命令默认使用当前工作区。如果管理多个工作区，可在每条 `aikito remote` 命令后加上
`--workspace /path/to/workspace`，指定要操作的工作区。

## 开启自动同步

在每台已连接的机器上启动后台同步：

```bash
aikito remote start
aikito remote status
```

确认状态显示后台服务正在运行。服务运行且机器可以访问同步服务时，变更会自动同步。
离线时仍可使用本地资源，网络恢复后继续同步。

暂停后台同步：

```bash
aikito remote stop
```

暂停后，仍可运行 `aikito remote sync` 手动同步。

## 连接另一台机器

在新机器上安装兼容的 Aikito 客户端，然后初始化工作区并连接：

```bash
aikito init workspace ~/aikito
aikito remote bind https://aikito.notsonan.com
```

使用相同的服务地址和账号。在浏览器中批准连接后，按终端提示输入恢复码。
Aikito 会验证恢复码、保存这台设备的解密密钥，并在本机保存一份恢复码。

同步已有内容并开启后台同步：

```bash
aikito remote sync
aikito remote start
```

下载完成后，按照[同步并验证本机 Agent 配置](sync-existing-setup.md)的说明，
将资源连接到这台机器上安装的 Agent。

同步的内容是 Aikito 管理的资源，不包含项目源码仓库。请单独克隆项目源码，
再[连接项目的本机路径](project-setup.md)。无需从另一台机器复制隐藏的本地状态。

## 连接 AI 客户端（MCP）

Aikito Cloud 通过远程 MCP 接口提供已经同步的内容，请先完成前面的首次同步。
仅开启同步不会自动允许任何 AI 客户端读取或修改资源。

!!! note "支持的客户端"
    目前仅支持 ChatGPT 接入。其他 MCP 客户端暂不可直接连接，后续将陆续开放。

### 选择允许访问的资源

在已经连接、持有解密密钥的机器上，打开本机控制台。
浏览器连接流程已自动保存服务公钥，无需另外获取公钥或管理公钥文件。

```bash
aikito web
```

使用之前连接的同一当前工作区，保持控制台运行，打开 **Resource Access**，然后：

1. 打开 [Aikito Cloud 账号页](https://aikito.notsonan.com/account)。
2. 在 **Authorize collections** 中勾选要共享的资源。只需查阅时选择 **Read**；
   需要 AI 客户端修改的资源再选择 **Write**。选择授权期限，点击 **Send for local confirmation**。
3. 回到本机控制台，核对账号、服务、资源范围、读写权限和期限，确认这次授权请求。
4. 在账号页确认这些资源已经出现在 **Authorized collections** 中。

如果账号页资源列表为空，先在本机打开 Resource Access 发布当前资源目录，再刷新账号页。
授权后，Aikito Cloud 可以为获准的 AI 客户端处理所选资源；未选择的资源不会开放。

### 在 ChatGPT 中添加连接

在 ChatGPT 网页版打开 **Plugins**，点击加号，添加自定义 MCP 服务器。
填写以下 Aikito Cloud 连接设置：

| 设置 | 内容 |
| --- | --- |
| 名称 | `Aikito Cloud` |
| Server URL | `https://aikito.notsonan.com/mcp` |
| 认证方式 | OAuth |
| Client ID | `aikito-chatgpt` |
| Client secret | 留空；此连接使用公开客户端，无需客户端密钥 |

如果服务提供的连接设置不同，以服务提供的值为准。阅读 ChatGPT 的连接提示，创建插件，
然后完成账号连接：登录同一个 Aikito Cloud 账号，选择允许此 ChatGPT 连接使用的资源
和权限，再确认授权。如果提示安装插件，请完成安装。
这里仅能选择已在 Aikito 中授权的资源，不能扩大之前批准的访问范围。

这个入口可能受 ChatGPT 账号或工作区策略限制。最新界面和安装步骤见
[OpenAI 自定义 MCP 服务器指南](https://developers.openai.com/api/docs/guides/custom-mcp-server)。
不要在 ChatGPT 中输入 Aikito 恢复码。

### 使用和撤销访问

在 ChatGPT 中打开对话，输入 `@` 并选择 Aikito Cloud 插件。例如：

> 使用 Aikito Cloud 查找我关于这个项目的笔记，并做一个摘要。

对于允许 AI 客户端修改的资源，可以先让它展示拟修改的内容，确认后再提交。
保持各台机器的后台同步运行，即可同步 AI 客户端提交的变更。

Aikito Cloud 账号页的 **Connected AI clients** 会显示这次连接。
在这里撤销即可停止该 AI 客户端连接的访问，不影响工作区同步。
在 **Authorized collections** 撤销某项资源授权，则所有依赖该授权的 AI 客户端都将失去访问权限。
如果找不到资源或授权到期，先检查资源授权和对应连接的权限，必要时重新授权连接。

## 本地文件与恢复码

### 恢复码备份

macOS 和 Linux 上的默认保存路径是：

```text
~/.config/aikito/encrypted-remote/recovery.txt
```

如果设置了 `XDG_CONFIG_HOME`，文件位于
`$XDG_CONFIG_HOME/aikito/encrypted-remote/recovery.txt`。
Windows 上位于 `%APPDATA%\aikito\encrypted-remote\recovery.txt`；
未设置 `APPDATA` 时，使用 `~/.config` 作为配置目录。

需要连接另一台机器时，在本机打开这个文件。每条记录除了恢复码，还包含保存时的服务地址
和工作区路径。根据这些信息找到对应记录，使用其 `code:` 后面的值。
同一个同步空间会更新原记录，不会重复添加。文件仅限当前用户访问，保存在工作区之外，
不参与同步。

建议另外保存一份到密码管理器或其他安全位置：如果整台机器丢失，本机副本也会一起丢失。
账号密码用于登录，不能替代丢失的恢复码，也不能用于解密内容。

恢复码会在首次生成或输入并验证成功时保存。旧设备如果只持有解密密钥，
重新连接时无法反推出之前未备份的恢复码。

### 服务公钥

浏览器连接成功后，程序会自动保存服务公钥和身份信任记录。
macOS 和 Linux 上的默认位置是：

```text
~/.config/aikito/remote-recipients/<服务地址哈希>.json
```

这里同样遵循前面介绍的 `XDG_CONFIG_HOME` 和 Windows `APPDATA` 配置目录规则。
文件名由服务地址生成，由 Aikito 自动维护，无需手动获取公钥或选择文件。
它记录的是公开的服务身份信息，与需要额外备份的秘密恢复码不同。
服务公钥变化时，程序会拒绝静默替换已有记录。保存公钥不会代替前面的资源访问授权。

## 重新连接或断开连接

如果重置账号密码后，设备连接失效，可重新连接：

```bash
aikito remote bind https://aikito.notsonan.com --rebind
```

Aikito 会复用账号原有的同步内容。如果这台机器仍有解密密钥，通常无需再次输入恢复码。

断开当前工作区的连接，并停止其后台同步：

```bash
aikito remote unbind
```

断开连接会保留本地资源和账号中的同步内容。
完整命令选项见 [CLI 参考](cli-reference.md#remote-store-synchronization)。
如果希望通过 Git 迁移工作区，参阅[在另一台机器上连接工作区](workspace-portability.md)。
