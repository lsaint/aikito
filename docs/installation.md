# 1. Install Aikito

By the end of this step, `aikito --version` will work in your terminal.
Have a supported coding agent installed to follow the agent-assisted path.
The tutorial uses `~/aikito` for your workspace and `~/code/example` for an
existing code repository. Substitute your own paths throughout.

## Ask your agent

> Install Aikito for this machine. Inspect what is already installed and verify
> the CLI version. Stop before creating a workspace or adopting configuration.

If your agent needs setup instructions, point it to the
[Aikito skill](https://github.com/lsaint/aikito/blob/main/src/aikito/templates/skills/aikito/SKILL.md).

## Install manually

### Cross-platform (recommended)

With [uv](https://docs.astral.sh/uv/):

```bash
uv tool install aikito
```

With [pipx](https://pypa.github.io/pipx/):

```bash
pipx install aikito
```

### macOS / Linux with Homebrew

```bash
brew install lsaint/tap/aikito
```

### Windows

Enable Developer Mode in Settings so Aikito can create symbolic links without administrator privileges:
**Settings → System → For developers → Developer Mode**.

Then install via `uv tool` or `pipx`:

```powershell
uv tool install aikito
```

Or run the automated PowerShell installer:

```powershell
irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1 | iex
```

Open a new terminal afterward to load the PATH update. Use native Windows
paths such as `D:/code/example` in subsequent steps.
See [platform constraints](safety.md#platform-support-and-constraints) for details.

### Windows installer details

The installer checks for Windows Developer Mode (symlink support) and Python 3.12+,
and installs Aikito using `uv tool` (if available) or an isolated virtual environment at
`%LOCALAPPDATA%\Programs\aikito`, adding its command directory to your User PATH.
No administrator privileges are required when Developer Mode is enabled.

To choose a different installation directory when installing to a virtual environment:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1))) -InstallDir "D:\aikito"
```

The CLI installation directory is separate from your personal workspace.
For example, after opening a new terminal, create the workspace with
`aikito init workspace $env:USERPROFILE\aikito`, then follow
[workspace synchronization](workspace-setup.md).

### Shell completion

Homebrew installs Zsh, Bash, and Fish completions automatically. For manual
installs, follow the [shell completion reference](cli-reference.md#shell-completion),
which includes PowerShell profile setup.

## Verify

```bash
aikito --version
aikito --help
```

You should see a version and the command list. If the shell cannot find
`aikito`, resolve the installation or PATH issue before continuing.

Next: [Create a workspace](workspace-setup.md).
