# Aikito Cloud

[English](remote-sync.md)

!!! note "同步方式"
    Aikito 默认通过 Git 进行多机器同步，无需专用服务器。
    你也可以根据 [Remote Protocol](architecture/remote-protocol.md) 开发自己的专用同步服务器，
    或使用 Aikito Cloud。本文介绍如何连接 Aikito Cloud。

    Aikito Cloud 同步还需要安装[加密运行时](installation.md#macos-linux-with-homebrew)。

将 Aikito 工作区连接到 Aikito Cloud，让多台机器上的资源保持更新。
你需要一个 Aikito Cloud 账号，以及账号页面提供的连接命令。

## 连接第一台机器

如果收到了邀请，打开邀请链接并设置账号密码；已有账号则直接登录。

使用现有的 Aikito 工作区。如果尚未创建，运行：

```bash
aikito init workspace ~/aikito
```

从账号页复制连接命令，在交互式终端中运行。下面是示例，请将示例 URL 替换为实际服务地址：

```bash
aikito remote bind https://sync.example.com
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
aikito remote bind https://sync.example.com
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

## 查找和备份恢复码

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

## 重新连接或断开连接

如果重置账号密码后，设备连接失效，可重新连接：

```bash
aikito remote bind https://sync.example.com --rebind
```

Aikito 会复用账号原有的同步内容。如果这台机器仍有解密密钥，通常无需再次输入恢复码。

断开当前工作区的连接，并停止其后台同步：

```bash
aikito remote unbind
```

断开连接会保留本地资源和账号中的同步内容。
完整命令选项见 [CLI 参考](cli-reference.md#remote-store-synchronization)。
如果希望通过 Git 迁移工作区，参阅[在另一台机器上连接工作区](workspace-portability.md)。
