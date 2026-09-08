# 1. Install Aikito

By the end of this step, `aikito --version` will work in your terminal.
Have a supported coding agent installed to follow the agent-assisted path.
The tutorial uses `~/aikito` for your workspace and `~/code/example` for an
existing code repository. Substitute your own paths throughout.

## Ask your agent

> Install Aikito for this machine. Inspect what is already installed and verify
> the CLI version. Stop before creating a workspace or adopting configuration.

If your agent needs setup instructions, point it to the
[Aikito skill](https://github.com/lsaint/aikito/blob/main/templates/skills/aikito/SKILL.md).

## Install manually

On macOS or Linux with Homebrew:

```bash
brew install lsaint/tap/aikito
```

On Windows, enable Developer Mode in Settings so Aikito can create symbolic
links, then run the release installer in PowerShell:

```powershell
irm https://raw.githubusercontent.com/lsaint/aikito/main/install.ps1 | iex
```

Open a new terminal afterward to load the PATH update. Use native Windows
paths such as `D:/code/example` in subsequent steps.
See [platform constraints](safety.md#platform-support-and-constraints) for details.

## Verify

```bash
aikito --version
aikito --help
```

You should see a version and the command list. If the shell cannot find
`aikito`, resolve the installation or PATH issue before continuing.

Next: [Create a workspace](workspace-setup.md).
